"""Temperature Edge Bot — analysis engine.

Aggregates data from 4 NWS sources, tracks running max temperature,
computes rounding edge relative to Kalshi bracket boundaries,
and generates trading signals.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional
from zoneinfo import ZoneInfo

from kalshi_weather.metar_parser import (
    c_to_f_cli_rounded,
    c_to_f_precise,
    get_f_boundary_c,
    nws_round,
)
from kalshi_weather.nws_scraper import NWSScraper
from kalshi_weather.rules import (
    _STATION_DB,
    get_cli_day_window,
    lookup_station,
)

logger = logging.getLogger(__name__)


# ── Enums ────────────────────────────────────────────────────────────


class Confidence(Enum):
    """Confidence level for a temperature reading based on its source."""

    HIGHEST = "HIGHEST"   # CLI preliminary max
    HIGH = "HIGH"         # T-group from METAR (tenths °C)
    MEDIUM_HIGH = "MEDIUM_HIGH"  # Current conditions decimal °F
    MEDIUM = "MEDIUM"     # 6-hr / 24-hr extremes
    LOW = "LOW"           # Observation history (rounded values)


class MarginStatus(Enum):
    """How close the running max is to a rounding boundary."""

    COMFORTABLE = "COMFORTABLE"    # > 0.5°C from boundary
    MODERATE = "MODERATE"          # 0.3 – 0.5°C
    CLOSE = "CLOSE"                # 0.1 – 0.3°C
    RAZOR_THIN = "RAZOR_THIN"      # < 0.1°C


class TimeRisk(Enum):
    """Time-of-day risk for the temperature still changing."""

    STILL_RISING = "STILL_RISING"  # Before 3 PM local
    NEAR_PEAK = "NEAR_PEAK"        # 3 – 5 PM local
    PAST_PEAK = "PAST_PEAK"        # After 5 PM local
    SETTLED = "SETTLED"             # Near CLI close (midnight LST)


class Signal(Enum):
    """Trading signal."""

    STRONG_BUY = "STRONG_BUY"
    BUY = "BUY"
    HOLD = "HOLD"
    CAUTION = "CAUTION"
    NO_EDGE = "NO_EDGE"


# ── Dataclasses ──────────────────────────────────────────────────────


@dataclass
class TemperatureReading:
    """A single temperature reading from any NWS source."""

    source: str               # e.g. "METAR T-group", "Current Conditions"
    time_utc: Optional[datetime]
    temp_c: Optional[float]
    temp_f_precise: Optional[float]
    cli_rounded_f: Optional[int]
    confidence: Confidence
    note: str = ""


@dataclass
class BracketAnalysis:
    """Analysis of how a temperature relates to Kalshi brackets."""

    cli_rounded_f: int                    # Predicted CLI integer °F
    boundary_below_c: float               # °C boundary below (rounds to cli_rounded_f)
    boundary_above_c: float               # °C boundary above (rounds to cli_rounded_f + 1)
    margin_below_c: float                 # Distance above lower boundary (positive = safe)
    margin_above_c: float                 # Distance below upper boundary (positive = room)
    margin_status: MarginStatus


@dataclass
class EdgeReport:
    """Complete analysis for a single city."""

    city: str
    station_icao: str
    cli_code: str
    timezone: str
    analysis_time_utc: datetime
    readings: list[TemperatureReading] = field(default_factory=list)
    running_max_c: Optional[float] = None
    running_max_f_precise: Optional[float] = None
    running_max_cli_f: Optional[int] = None
    running_max_source: Optional[str] = None
    metar_temp_f: Optional[int] = None      # What hourly METAR shows (rounded)
    bracket: Optional[BracketAnalysis] = None
    time_risk: TimeRisk = TimeRisk.STILL_RISING
    hours_to_cli_close: Optional[float] = None
    signal: Signal = Signal.NO_EDGE
    signal_reason: str = ""
    cli_max_f: Optional[int] = None          # From preliminary CLI if available
    cli_is_preliminary: bool = False


# ── Classification helpers ───────────────────────────────────────────


def classify_margin(margin_c: float) -> MarginStatus:
    """Classify margin distance (°C above lower boundary).

    Each integer °F spans 5/9 ≈ 0.556°C, so the max possible margin
    from the nearest boundary is ~0.278°C.  Thresholds are scaled
    to this physical range.
    """
    abs_margin = abs(margin_c)
    if abs_margin >= 0.20:
        return MarginStatus.COMFORTABLE
    if abs_margin >= 0.12:
        return MarginStatus.MODERATE
    if abs_margin >= 0.06:
        return MarginStatus.CLOSE
    return MarginStatus.RAZOR_THIN


def classify_time_risk(local_hour: int) -> TimeRisk:
    """Classify time-of-day risk based on local hour (0-23)."""
    if local_hour < 15:
        return TimeRisk.STILL_RISING
    if local_hour < 17:
        return TimeRisk.NEAR_PEAK
    if local_hour < 22:
        return TimeRisk.PAST_PEAK
    return TimeRisk.SETTLED


def compute_bracket_analysis(temp_c: float) -> BracketAnalysis:
    """Compute bracket analysis for a temperature in °C."""
    cli_f = c_to_f_cli_rounded(temp_c)

    # Boundary below: °C where CLI rounds to cli_f (from cli_f - 1)
    boundary_below_c = get_f_boundary_c(cli_f - 1)
    # Boundary above: °C where CLI rounds to cli_f + 1 (from cli_f)
    boundary_above_c = get_f_boundary_c(cli_f)

    margin_below = temp_c - boundary_below_c   # Positive = safely above lower boundary
    margin_above = boundary_above_c - temp_c   # Positive = still below upper boundary

    margin_status = classify_margin(margin_below)

    return BracketAnalysis(
        cli_rounded_f=cli_f,
        boundary_below_c=boundary_below_c,
        boundary_above_c=boundary_above_c,
        margin_below_c=margin_below,
        margin_above_c=margin_above,
        margin_status=margin_status,
    )


# ── Signal generation ────────────────────────────────────────────────


def _generate_signal(
    report: EdgeReport,
) -> tuple[Signal, str]:
    """Generate trading signal based on collected data.

    Returns (signal, reason).
    """
    if report.running_max_c is None or report.bracket is None:
        return Signal.NO_EDGE, "Insufficient data to analyze."

    bracket = report.bracket
    metar_f = report.metar_temp_f
    cli_f = bracket.cli_rounded_f

    # If preliminary CLI already published and matches, high confidence
    if report.cli_max_f is not None:
        if report.cli_max_f == cli_f:
            return Signal.STRONG_BUY, (
                f"Preliminary CLI confirms {cli_f}F. "
                f"Market should converge to this bracket."
            )
        if report.cli_max_f > cli_f:
            return Signal.CAUTION, (
                f"Preliminary CLI shows {report.cli_max_f}F, higher than "
                f"current running max predicts ({cli_f}F). CLI may be stale."
            )

    # Check if precise reading disagrees with rounded METAR
    metar_disagrees = metar_f is not None and metar_f != cli_f

    if metar_disagrees:
        # The core edge: METAR shows one value, precise data shows another
        if bracket.margin_status in (MarginStatus.COMFORTABLE, MarginStatus.MODERATE):
            if report.time_risk in (TimeRisk.PAST_PEAK, TimeRisk.SETTLED):
                return Signal.STRONG_BUY, (
                    f"Precise data shows {cli_f}F with {bracket.margin_status.value} margin. "
                    f"Hourly METARs show {metar_f}F — market likely underpricing. "
                    f"Time risk: {report.time_risk.value}."
                )
            return Signal.BUY, (
                f"Precise data shows {cli_f}F (METAR shows {metar_f}F). "
                f"Margin: {bracket.margin_status.value}. "
                f"Still {report.time_risk.value} — could move further."
            )

        if bracket.margin_status == MarginStatus.CLOSE:
            return Signal.CAUTION, (
                f"Precise data shows {cli_f}F but margin is CLOSE "
                f"({bracket.margin_below_c:+.3f}°C). "
                f"Temperature could drift back across the boundary."
            )

        # RAZOR_THIN
        return Signal.CAUTION, (
            f"Precise data shows {cli_f}F but margin is RAZOR_THIN "
            f"({bracket.margin_below_c:+.3f}°C). Very risky."
        )

    # METAR agrees with precise reading — less edge
    if bracket.margin_status == MarginStatus.COMFORTABLE:
        return Signal.NO_EDGE, (
            f"All sources agree on {cli_f}F with comfortable margin. "
            f"Market likely already priced correctly."
        )

    if bracket.margin_status in (MarginStatus.CLOSE, MarginStatus.RAZOR_THIN):
        return Signal.CAUTION, (
            f"Sources agree on {cli_f}F but margin is {bracket.margin_status.value}. "
            f"Small temperature change could flip the bracket."
        )

    return Signal.HOLD, (
        f"Sources agree on {cli_f}F. Moderate margin. "
        f"No significant edge detected."
    )


# ── Main analysis ────────────────────────────────────────────────────


def analyze_city(city: str, scraper: NWSScraper) -> Optional[EdgeReport]:
    """Run full temperature edge analysis for one city.

    Steps:
      1. Look up station metadata
      2. Fetch all 4 NWS sources
      3. Build temperature readings list
      4. Find running max
      5. Compute rounding edge & bracket analysis
      6. Assess time risk
      7. Generate signal
    """
    # Step 1: Look up station
    entry = lookup_station(city)
    if entry is None:
        logger.warning("City '%s' not found in station database", city)
        return None

    icao = entry["station_icao"]
    cli_code = entry["cli_issuedby"]
    tz_str = entry["timezone"]
    tz = ZoneInfo(tz_str)
    now_utc = datetime.now(ZoneInfo("UTC"))
    now_local = now_utc.astimezone(tz)

    report = EdgeReport(
        city=entry["kalshi_city"],
        station_icao=icao,
        cli_code=cli_code,
        timezone=tz_str,
        analysis_time_utc=now_utc,
    )

    # Step 2: Fetch all 4 sources
    metar_obs = scraper.get_raw_metar(icao)
    current_cond = scraper.get_current_conditions(icao)
    obs_history = scraper.get_observation_history(icao)
    cli_report = scraper.get_preliminary_cli(cli_code)

    readings: list[TemperatureReading] = []

    # Step 3: Build readings from each source

    # Source 1: Raw METAR
    if metar_obs is not None:
        if metar_obs.has_t_group and metar_obs.temp_c_tenths is not None:
            readings.append(TemperatureReading(
                source="METAR T-group",
                time_utc=metar_obs.observation_time_utc,
                temp_c=metar_obs.temp_c_tenths,
                temp_f_precise=metar_obs.temp_f_precise,
                cli_rounded_f=c_to_f_cli_rounded(metar_obs.temp_c_tenths),
                confidence=Confidence.HIGH,
                note=f"Raw METAR from {icao}",
            ))
        if metar_obs.temp_c_rounded is not None:
            report.metar_temp_f = nws_round(
                c_to_f_precise(float(metar_obs.temp_c_rounded))
            )

        # 6-hr max from METAR
        if metar_obs.six_hr_max_c is not None:
            readings.append(TemperatureReading(
                source="METAR 6-hr max",
                time_utc=metar_obs.observation_time_utc,
                temp_c=metar_obs.six_hr_max_c,
                temp_f_precise=c_to_f_precise(metar_obs.six_hr_max_c),
                cli_rounded_f=c_to_f_cli_rounded(metar_obs.six_hr_max_c),
                confidence=Confidence.MEDIUM,
                note="6-hour maximum from METAR remarks",
            ))

    # Source 2: Current Conditions
    if current_cond is not None and current_cond.temp_f is not None:
        temp_c = current_cond.temp_c
        if temp_c is None and current_cond.temp_f is not None:
            temp_c = (current_cond.temp_f - 32.0) * 5.0 / 9.0
        readings.append(TemperatureReading(
            source="Current Conditions",
            time_utc=now_utc,  # Approximate; page doesn't always have exact time
            temp_c=temp_c,
            temp_f_precise=current_cond.temp_f,
            cli_rounded_f=nws_round(current_cond.temp_f) if current_cond.temp_f else None,
            confidence=Confidence.MEDIUM_HIGH,
            note=f"NWS current conditions page for {icao}",
        ))

    # Source 3: Observation History
    if obs_history is not None and obs_history.max_temp_f is not None:
        max_c = (obs_history.max_temp_f - 32.0) * 5.0 / 9.0
        readings.append(TemperatureReading(
            source="Observation History Max",
            time_utc=None,
            temp_c=max_c,
            temp_f_precise=obs_history.max_temp_f,
            cli_rounded_f=nws_round(obs_history.max_temp_f),
            confidence=Confidence.LOW,
            note=f"Max from {len(obs_history.entries)} observations today",
        ))

    # Source 4: Preliminary CLI
    if cli_report is not None and cli_report.max_temp_f is not None:
        report.cli_max_f = cli_report.max_temp_f
        report.cli_is_preliminary = cli_report.is_preliminary
        # Back-convert CLI integer °F to approximate °C for comparison
        approx_c = (cli_report.max_temp_f - 32.0) * 5.0 / 9.0
        readings.append(TemperatureReading(
            source="Preliminary CLI",
            time_utc=None,
            temp_c=approx_c,
            temp_f_precise=float(cli_report.max_temp_f),
            cli_rounded_f=cli_report.max_temp_f,
            confidence=Confidence.HIGHEST,
            note=(
                f"CLI {cli_code}: max {cli_report.max_temp_f}F"
                + (f" at {cli_report.max_temp_time}" if cli_report.max_temp_time else "")
                + (" (preliminary)" if cli_report.is_preliminary else "")
            ),
        ))

    report.readings = readings

    # Step 4: Find running max (highest precise °C reading)
    precise_readings = [
        r for r in readings
        if r.temp_c is not None and r.confidence != Confidence.LOW
    ]
    if precise_readings:
        best = max(precise_readings, key=lambda r: r.temp_c)  # type: ignore[arg-type]
        report.running_max_c = best.temp_c
        report.running_max_f_precise = best.temp_f_precise
        report.running_max_cli_f = best.cli_rounded_f
        report.running_max_source = best.source

    # Step 5: Bracket analysis
    if report.running_max_c is not None:
        report.bracket = compute_bracket_analysis(report.running_max_c)

    # Step 6: Time risk
    report.time_risk = classify_time_risk(now_local.hour)

    # Hours to CLI close (midnight LST)
    cli_start, cli_end = get_cli_day_window(now_local.date(), tz_str)
    remaining = (cli_end - now_utc).total_seconds() / 3600.0
    report.hours_to_cli_close = max(0.0, remaining)

    # Step 7: Signal
    report.signal, report.signal_reason = _generate_signal(report)

    return report


def analyze_all_cities(scraper: NWSScraper) -> list[EdgeReport]:
    """Run edge analysis for all 26 cities in the station database."""
    reports: list[EdgeReport] = []
    for entry in _STATION_DB:
        city = entry["kalshi_city"]
        report = analyze_city(city, scraper)
        if report is not None:
            reports.append(report)
    return reports


# ── Output formatters ────────────────────────────────────────────────


def format_edge_report(report: EdgeReport) -> str:
    """Format a detailed single-city edge report."""
    tz = ZoneInfo(report.timezone)
    local_time = report.analysis_time_utc.astimezone(tz)
    time_str = local_time.strftime("%Y-%m-%d %H:%M %Z")

    hours_str = (
        f"{report.hours_to_cli_close:.1f} hours"
        if report.hours_to_cli_close is not None
        else "unknown"
    )

    lines = [
        f"=== TEMPERATURE EDGE: {report.city} ({report.station_icao}) ===",
        f"Time: {time_str} | CLI closes in {hours_str}",
        "",
        "--- PRECISE READINGS ---",
    ]

    for r in report.readings:
        time_part = ""
        if r.time_utc:
            time_part = f" ({r.time_utc.strftime('%H:%MZ')})"
        temp_parts = []
        if r.temp_c is not None:
            temp_parts.append(f"{r.temp_c:.1f}C")
        if r.temp_f_precise is not None:
            temp_parts.append(f"= {r.temp_f_precise:.2f}F")
        if r.cli_rounded_f is not None:
            temp_parts.append(f"-> CLI: {r.cli_rounded_f}F")
        temp_str = " ".join(temp_parts)

        marker = ""
        if (
            report.running_max_source
            and r.source == report.running_max_source
            and len(report.readings) > 1
        ):
            marker = "  << HIGHEST"

        lines.append(f"  {r.source}{time_part}: {temp_str}{marker}")
        if r.note:
            lines.append(f"    ({r.note})")

    lines.append("")

    if report.running_max_c is not None and report.bracket is not None:
        b = report.bracket
        lines.append(
            f"--- RUNNING MAX: {report.running_max_c:.1f}C "
            f"({report.running_max_f_precise:.2f}F) -> CLI: {b.cli_rounded_f}F ---"
        )
        lines.append(
            f"  Boundary {b.cli_rounded_f - 1}/{b.cli_rounded_f}F: "
            f"{b.boundary_below_c:.3f}C | "
            f"Margin: {b.margin_below_c:+.3f}C above ({b.margin_status.value})"
        )
        lines.append(
            f"  Boundary {b.cli_rounded_f}/{b.cli_rounded_f + 1}F: "
            f"{b.boundary_above_c:.3f}C | "
            f"Gap: {-b.margin_above_c:+.3f}C below "
            f"(needs {b.margin_above_c:+.1f}C more)"
        )
    else:
        lines.append("--- NO PRECISE DATA AVAILABLE ---")

    lines.append("")
    lines.append(f"--- SIGNAL: {report.signal.value} ---")
    lines.append(f"  Time risk: {report.time_risk.value}")
    lines.append(f"  {report.signal_reason}")

    return "\n".join(lines)


def format_edge_summary(reports: list[EdgeReport]) -> str:
    """Format a multi-city summary table."""
    header = (
        f"{'City':<15} | {'METAR':>5} | {'Precise':>7} | "
        f"{'CLI F':>5} | {'Margin C':>8} | {'Signal':<11} | {'Time'}"
    )
    sep = "-" * len(header)

    lines = [
        "=== TEMPERATURE EDGE SUMMARY ===",
        "",
        header,
        sep,
    ]

    for r in reports:
        metar_str = str(r.metar_temp_f) if r.metar_temp_f is not None else "—"
        precise_str = (
            f"{r.running_max_f_precise:.1f}" if r.running_max_f_precise is not None else "—"
        )
        cli_str = str(r.running_max_cli_f) if r.running_max_cli_f is not None else "—"
        margin_str = (
            f"{r.bracket.margin_below_c:+.2f}"
            if r.bracket is not None
            else "—"
        )
        signal_str = r.signal.value
        time_str = r.time_risk.value

        lines.append(
            f"{r.city:<15} | {metar_str:>5} | {precise_str:>7} | "
            f"{cli_str:>5} | {margin_str:>8} | {signal_str:<11} | {time_str}"
        )

    # Count signals
    signals = [r.signal for r in reports]
    strong = signals.count(Signal.STRONG_BUY)
    buy = signals.count(Signal.BUY)
    caution = signals.count(Signal.CAUTION)

    lines.append(sep)
    lines.append(
        f"Signals: {strong} STRONG_BUY, {buy} BUY, {caution} CAUTION, "
        f"{len(reports) - strong - buy - caution} other"
    )

    return "\n".join(lines)
