"""Probability Modeler — Phase 4 teammate C.

Computes settlement-accurate probabilities, lock-in metrics,
time-remaining fields, and knife-edge risk for each candidate.
"""

from __future__ import annotations

import logging
import math
from datetime import datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

from astral import LocationInfo
from astral.sun import sun

from kalshi_weather.config import DEFAULT_CONFIG, Config
from kalshi_weather.rules import get_cli_day_window, get_station_timezone, lookup_station
from kalshi_weather.schemas import (
    CandidateRaw,
    KnifeEdgeRisk,
    LockInFlag,
    MarketType,
    ModelOutput,
    UncertaintyLevel,
)
from kalshi_weather.weather_api import StationForecast

logger = logging.getLogger(__name__)

# ── Sunrise / peak-time helpers ────────────────────────────────────────

# Approximate coordinates for stations (lat, lon). Used for sunrise calc.
_STATION_COORDS: dict[str, tuple[float, float]] = {
    "KNYC": (40.783, -73.967),
    "KMDW": (41.786, -87.752),
    "KMIA": (25.793, -80.290),
    "KAUS": (30.195, -97.670),
    "KLAX": (33.938, -118.389),
    "KDEN": (39.856, -104.673),
    "KLAS": (36.080, -115.152),
    "KSEA": (47.449, -122.309),
    "KATL": (33.640, -84.427),
    "KBOS": (42.361, -71.011),
    "KCLT": (35.214, -80.943),
    "KDFW": (32.898, -97.040),
    "KDTW": (42.212, -83.349),
    "KHOU": (29.645, -95.279),
    "KJAX": (30.494, -81.688),
    "KMSP": (44.883, -93.229),
    "KBNA": (36.124, -86.678),
    "KMSY": (29.993, -90.258),
    "KOKC": (35.393, -97.601),
    "KPHL": (39.872, -75.241),
    "KPHX": (33.434, -112.012),
    "KSAT": (29.534, -98.470),
    "KSFO": (37.619, -122.375),
    "KTPA": (27.963, -82.537),
    "KDCA": (38.852, -77.034),
    "KLGA": (40.777, -73.873),
    "KORD": (41.979, -87.906),
}

# Typical peak temperature hour (local) — generally 2-5 PM.
_DEFAULT_PEAK_HOUR = 15  # 3 PM local


def _get_sunrise(station_icao: str, date: datetime, tz_str: str) -> Optional[datetime]:
    """Compute sunrise for a station on a given date."""
    coords = _STATION_COORDS.get(station_icao)
    if coords is None:
        return None
    lat, lon = coords
    tz = ZoneInfo(tz_str)
    loc = LocationInfo(latitude=lat, longitude=lon, timezone=tz_str)
    try:
        s = sun(loc.observer, date=date, tzinfo=tz)
        return s["sunrise"]
    except Exception:
        logger.warning("Could not compute sunrise for %s", station_icao, exc_info=True)
        return None


def _get_peak_time(date: datetime, tz_str: str) -> datetime:
    """Return the typical peak temperature time (3 PM local)."""
    tz = ZoneInfo(tz_str)
    return datetime(date.year, date.month, date.day, _DEFAULT_PEAK_HOUR, 0, 0, tzinfo=tz)


# ── Probability estimation ────────────────────────────────────────────

def _parse_bracket_threshold(bracket_def: str) -> Optional[float]:
    """Extract the numeric threshold from a bracket definition.

    Examples:
        "40°F or above" -> 40.0
        "50°F or below" -> 50.0
        "Between 45°F and 49°F" -> 47.0 (midpoint)
        "45 to 49" -> 47.0
    """
    import re

    # Pattern: "X°F or above/below" or "X or above/below"
    m = re.search(r"(\d+)°?\s*F?\s+or\s+(above|below)", bracket_def, re.IGNORECASE)
    if m:
        return float(m.group(1))

    # Pattern: "Between X and Y" or "X to Y"
    m = re.search(r"(?:between\s+)?(\d+)°?\s*F?\s+(?:and|to)\s+(\d+)", bracket_def, re.IGNORECASE)
    if m:
        return (float(m.group(1)) + float(m.group(2))) / 2

    # Last resort: find any number
    m = re.search(r"(\d+)", bracket_def)
    if m:
        return float(m.group(1))
    return None


def _normal_cdf(x: float, mu: float, sigma: float) -> float:
    """Standard normal CDF approximation."""
    if sigma <= 0:
        return 1.0 if x >= mu else 0.0
    z = (x - mu) / sigma
    return 0.5 * (1 + math.erf(z / math.sqrt(2)))


def _estimate_p_bracket(
    bracket_def: str,
    forecast_temp_f: float,
    uncertainty_sigma: float = 3.0,
) -> tuple[float, float]:
    """Estimate P(YES) and P(NO) for a bracket using normal distribution.

    Models the actual settlement temperature as N(forecast, sigma^2).
    Returns (p_yes, p_no).
    """
    threshold = _parse_bracket_threshold(bracket_def)
    if threshold is None:
        return 0.5, 0.5  # Can't parse — maximum uncertainty

    bracket_lower = bracket_def.lower()

    if "above" in bracket_lower or ">=" in bracket_lower:
        # YES = temp >= threshold
        p_yes = 1 - _normal_cdf(threshold - 0.5, forecast_temp_f, uncertainty_sigma)
        return p_yes, 1 - p_yes
    elif "below" in bracket_lower or "<=" in bracket_lower:
        # YES = temp <= threshold
        p_yes = _normal_cdf(threshold + 0.5, forecast_temp_f, uncertainty_sigma)
        return p_yes, 1 - p_yes
    elif "between" in bracket_lower or "to" in bracket_lower:
        import re
        m = re.search(
            r"(?:between\s+)?(\d+)°?\s*F?\s+(?:and|to)\s+(\d+)",
            bracket_def, re.IGNORECASE,
        )
        if m:
            lo, hi = float(m.group(1)), float(m.group(2))
            p_yes = _normal_cdf(hi + 0.5, forecast_temp_f, uncertainty_sigma) - \
                    _normal_cdf(lo - 0.5, forecast_temp_f, uncertainty_sigma)
            return max(p_yes, 0.001), 1 - max(p_yes, 0.001)
        return 0.5, 0.5
    else:
        # Assume "at or above" by default for ambiguous brackets
        p_yes = 1 - _normal_cdf(threshold - 0.5, forecast_temp_f, uncertainty_sigma)
        return p_yes, 1 - p_yes


def _estimate_p_new_extreme(
    current_extreme_f: float,
    forecast_extreme_f: float,
    hours_remaining: float,
    is_low: bool,
) -> float:
    """Estimate probability of a new lower-low or higher-high after now.

    Simple model: as time remaining shrinks and the current extreme
    is already near/past the forecast, probability drops.
    """
    if hours_remaining <= 0:
        return 0.0

    # How much "room" is there for a new extreme?
    if is_low:
        room = current_extreme_f - forecast_extreme_f  # positive if forecast is lower
    else:
        room = forecast_extreme_f - current_extreme_f  # positive if forecast is higher

    if room <= 0:
        # Already past forecast extreme — new extreme unlikely but not impossible.
        base_p = 0.15
    elif room >= 5:
        base_p = 0.85
    else:
        base_p = 0.15 + (room / 5) * 0.70

    # Time decay: less time = less chance for change.
    time_factor = min(1.0, hours_remaining / 6.0)
    return round(min(base_p * time_factor, 0.99), 4)


def _compute_knife_edge(
    bracket_def: str,
    forecast_temp_f: float,
    sigma: float = 3.0,
) -> KnifeEdgeRisk:
    """Score knife-edge risk: how much distribution mass is near the boundary."""
    threshold = _parse_bracket_threshold(bracket_def)
    if threshold is None:
        return KnifeEdgeRisk.HIGH

    distance = abs(forecast_temp_f - threshold)
    if distance <= 1.0:
        return KnifeEdgeRisk.HIGH
    elif distance <= sigma:
        return KnifeEdgeRisk.MED
    else:
        return KnifeEdgeRisk.LOW


def _classify_uncertainty(
    hours_vol_window: float,
    has_forecast: bool,
    knife_edge: KnifeEdgeRisk,
) -> UncertaintyLevel:
    """Classify overall uncertainty level."""
    if not has_forecast:
        return UncertaintyLevel.HIGH
    if knife_edge == KnifeEdgeRisk.HIGH:
        return UncertaintyLevel.HIGH
    if hours_vol_window > 4:
        return UncertaintyLevel.MED
    if hours_vol_window > 1:
        return UncertaintyLevel.LOW
    return UncertaintyLevel.LOW


# ── Main modeler function ──────────────────────────────────────────────

def model_candidate(
    candidate: CandidateRaw,
    forecast: Optional[StationForecast],
    current_obs_temp_f: Optional[float],
    now_utc: Optional[datetime] = None,
    config: Config = DEFAULT_CONFIG,
) -> ModelOutput:
    """Compute all modeler fields for a single candidate.

    Parameters
    ----------
    candidate : CandidateRaw
    forecast : StationForecast or None
    current_obs_temp_f : current observed temperature in F, or None
    now_utc : current time (UTC)
    config : Config
    """
    if now_utc is None:
        now_utc = datetime.now(ZoneInfo("UTC"))

    city = candidate.city
    market_type = candidate.market_type
    ticker = candidate.market_ticker
    bracket_def = candidate.bracket_definition

    # Timezone + local time.
    tz_str = get_station_timezone(city) or "America/New_York"
    tz = ZoneInfo(tz_str)
    local_now = now_utc.astimezone(tz)
    local_time_str = local_now.strftime("%Y-%m-%d %H:%M %Z")

    # Target date.
    target_date = datetime.strptime(candidate.target_date_local, "%Y-%m-%d")

    # CLI day window.
    cli_start, cli_end = get_cli_day_window(target_date, tz_str)
    hours_to_cli_close = max(0, (cli_end - now_utc).total_seconds() / 3600)

    # Station ICAO.
    entry = lookup_station(city)
    station_icao = entry["station_icao"] if entry else "KNYC"

    # Sunrise + peak time.
    sunrise_local = _get_sunrise(station_icao, target_date, tz_str)
    peak_local = _get_peak_time(target_date, tz_str)

    sunrise_str = sunrise_local.strftime("%H:%M %Z") if sunrise_local else None
    peak_str = peak_local.strftime("%H:%M %Z")

    # Hours remaining in meaningful volatility window.
    if market_type == MarketType.LOW_TEMP:
        if sunrise_local:
            vol_end = sunrise_local + timedelta(
                hours=config.lock_in.sunrise_buffer_hours
            )
        else:
            vol_end = local_now.replace(hour=9, minute=0, second=0)
        hours_vol = max(0, (vol_end - local_now).total_seconds() / 3600)
    else:
        vol_end = peak_local + timedelta(hours=config.lock_in.peak_buffer_hours)
        hours_vol = max(0, (vol_end - local_now).total_seconds() / 3600)

    # Forecast temperature for probability model.
    if forecast and market_type == MarketType.HIGH_TEMP:
        forecast_temp = forecast.forecast_high_f
    elif forecast and market_type == MarketType.LOW_TEMP:
        forecast_temp = forecast.forecast_low_f
    else:
        forecast_temp = None

    # Uncertainty sigma — wider if farther from settlement, narrower if locked in.
    sigma = 3.0
    if hours_vol < 1:
        sigma = 1.0
    elif hours_vol < 3:
        sigma = 2.0

    # Bracket probability.
    if forecast_temp is not None:
        p_yes, p_no = _estimate_p_bracket(bracket_def, forecast_temp, sigma)
    else:
        p_yes, p_no = 0.5, 0.5

    # Knife-edge risk.
    knife_edge = KnifeEdgeRisk.HIGH
    if forecast_temp is not None:
        knife_edge = _compute_knife_edge(bracket_def, forecast_temp, sigma)

    # Lock-in logic.
    p_new_lower = None
    lock_in_low = None
    p_new_higher = None
    lock_in_high = None
    signals: list[str] = []
    assumptions: list[str] = []
    model_notes: list[str] = []

    if market_type == MarketType.LOW_TEMP:
        current_low = current_obs_temp_f  # Best proxy for today's observed low so far.
        forecast_low = forecast.forecast_low_f if forecast else None

        if current_low is not None and forecast_low is not None:
            p_new_lower = _estimate_p_new_extreme(
                current_low, forecast_low, hours_vol, is_low=True
            )
            signals.append(f"current_obs={current_low}F, forecast_low={forecast_low}F")
        else:
            p_new_lower = 0.5 if hours_vol > 0 else 0.0

        # Lock-in gate.
        if sunrise_local and local_now > sunrise_local + timedelta(
            hours=config.lock_in.sunrise_buffer_hours
        ):
            reject_thresh = config.lock_in.p_new_extreme_reject_threshold
            if p_new_lower is not None and p_new_lower < reject_thresh:
                lock_in_low = LockInFlag.LOCKING
                model_notes.append("LOW lock-in: past sunrise+2h, P(new low) < 0.05")
            else:
                lock_in_low = LockInFlag.NOT_LOCKED
        else:
            lock_in_low = LockInFlag.NOT_LOCKED

    elif market_type == MarketType.HIGH_TEMP:
        current_high = current_obs_temp_f  # Proxy for observed high so far.
        forecast_high = forecast.forecast_high_f if forecast else None

        if current_high is not None and forecast_high is not None:
            p_new_higher = _estimate_p_new_extreme(
                current_high, forecast_high, hours_vol, is_low=False
            )
            signals.append(f"current_obs={current_high}F, forecast_high={forecast_high}F")
        else:
            p_new_higher = 0.5 if hours_vol > 0 else 0.0

        # Lock-in gate.
        if local_now > peak_local + timedelta(hours=config.lock_in.peak_buffer_hours):
            reject_thresh = config.lock_in.p_new_extreme_reject_threshold
            if p_new_higher is not None and p_new_higher < reject_thresh:
                lock_in_high = LockInFlag.LOCKING
                model_notes.append("HIGH lock-in: past peak+2h, P(new high) < 0.05")
            else:
                lock_in_high = LockInFlag.NOT_LOCKED
        else:
            lock_in_high = LockInFlag.NOT_LOCKED

    # Method description.
    method = f"Normal CDF (sigma={sigma})"
    if forecast_temp is not None:
        assumptions.append(f"Forecast temp={forecast_temp}F, sigma={sigma}")
    else:
        assumptions.append("No forecast available — using maximum uncertainty")
        method = "No-forecast fallback (p=0.5)"

    uncertainty = _classify_uncertainty(hours_vol, forecast_temp is not None, knife_edge)

    return ModelOutput(
        market_ticker=ticker,
        p_yes=round(p_yes, 4),
        p_no=round(p_no, 4),
        method=method,
        signals_used=signals,
        assumptions=assumptions,
        uncertainty_level=uncertainty,
        local_time_at_station=local_time_str,
        hours_remaining_until_cli_day_close=round(hours_to_cli_close, 2),
        hours_remaining_in_meaningful_volatility_window=round(hours_vol, 2),
        sunrise_estimate_local=sunrise_str,
        p_new_lower_low_after_now=p_new_lower,
        lock_in_flag_if_low=lock_in_low,
        typical_peak_time_estimate_local=peak_str if market_type == MarketType.HIGH_TEMP else None,
        p_new_higher_high_after_now=p_new_higher,
        high_lock_in_flag=lock_in_high,
        knife_edge_risk=knife_edge,
        model_notes=model_notes,
    )
