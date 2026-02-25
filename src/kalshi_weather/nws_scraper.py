"""NWS scraper — fetches precise temperature data from 4 NWS sources.

Sources:
  1. Raw METAR text (tgftp.nws.noaa.gov) — T-group with tenths °C
  2. Current conditions HTML — decimal °F
  3. Observation history HTML table — all today's observations
  4. Preliminary CLI product — official settlement max/min
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Optional

import httpx

from kalshi_weather.config import DEFAULT_CONFIG, Config
from kalshi_weather.metar_parser import MetarObservation, parse_raw_metar
from kalshi_weather.rate_limiter import RateLimiter, request_with_retry

logger = logging.getLogger(__name__)

_HEADERS = {
    "User-Agent": "(kalshi-weather-edge, contact@example.com)",
    "Accept": "text/html, text/plain, */*",
}


# ── Dataclasses for parsed results ───────────────────────────────────


@dataclass
class CurrentConditions:
    """Parsed from the NWS current conditions HTML page."""

    station_icao: str
    temp_f: Optional[float] = None       # decimal °F, e.g. 39.9
    temp_c: Optional[float] = None       # decimal °C
    humidity: Optional[str] = None
    wind: Optional[str] = None
    observation_time: Optional[str] = None
    six_hr_max_f: Optional[float] = None
    six_hr_min_f: Optional[float] = None
    twenty_four_hr_max_f: Optional[float] = None
    twenty_four_hr_min_f: Optional[float] = None


@dataclass
class ObsHistoryEntry:
    """A single row from the observation history table."""

    date_str: str              # e.g. "02/24"
    time_str: str              # e.g. "19:53"
    temp_f: Optional[float] = None
    dewpoint_f: Optional[float] = None
    wind_dir: Optional[str] = None
    wind_speed: Optional[str] = None


@dataclass
class ObservationHistory:
    """All observations from the history table, filtered to today."""

    station_icao: str
    entries: list[ObsHistoryEntry] = field(default_factory=list)
    max_temp_f: Optional[float] = None


@dataclass
class CliReport:
    """Parsed preliminary CLI (Climate Report)."""

    cli_code: str
    max_temp_f: Optional[int] = None
    max_temp_time: Optional[str] = None
    min_temp_f: Optional[int] = None
    min_temp_time: Optional[str] = None
    valid_as_of: Optional[str] = None
    is_preliminary: bool = True
    raw_text: str = ""


# ── Parsing helpers ──────────────────────────────────────────────────

# Current conditions: "Temperature: 39.9 F (4.4 C)"
_TEMP_RE = re.compile(
    r"Temperature[:\s]+([-\d.]+)\s*(?:&deg;|°)?\s*F\s*\(\s*([-\d.]+)\s*(?:&deg;|°)?\s*C\s*\)",
    re.IGNORECASE,
)

# Also try a simpler pattern for variant HTML formats
_TEMP_SIMPLE_RE = re.compile(
    r"([-\d.]+)\s*(?:&deg;|°)\s*F\s*\(\s*([-\d.]+)\s*(?:&deg;|°)\s*C\s*\)",
)

# 6-hour max/min from current conditions page
_6HR_MAX_RE = re.compile(
    r"(?:6[- ]?(?:hour|hr)\s+max(?:imum)?)[:\s]+([-\d.]+)\s*(?:&deg;|°)?\s*F",
    re.IGNORECASE,
)
_6HR_MIN_RE = re.compile(
    r"(?:6[- ]?(?:hour|hr)\s+min(?:imum)?)[:\s]+([-\d.]+)\s*(?:&deg;|°)?\s*F",
    re.IGNORECASE,
)

# 24-hour extremes
_24HR_MAX_RE = re.compile(
    r"(?:24[- ]?(?:hour|hr)\s+max(?:imum)?)[:\s]+([-\d.]+)\s*(?:&deg;|°)?\s*F",
    re.IGNORECASE,
)

# Observation history table row — flexible parsing
# Typical format: date, time, wind, vis, weather, sky, temp, dewpt, ...
_OBS_ROW_RE = re.compile(
    r"<tr[^>]*>\s*"
    r"<td[^>]*>([^<]*)</td>\s*"   # date or day
    r"<td[^>]*>([^<]*)</td>\s*"   # time
    r"(?:<td[^>]*>[^<]*</td>\s*){2,5}"  # wind, vis, weather, sky (variable cols)
    r"<td[^>]*>\s*([-\d.]+)\s*</td>\s*"  # temperature
    r"<td[^>]*>\s*([-\d.]+)\s*</td>",     # dewpoint
    re.IGNORECASE | re.DOTALL,
)

# CLI product parsing
_CLI_MAX_RE = re.compile(
    r"MAXIMUM\s+TEMPERATURE[^\n]*\n\s*(?:TODAY|YESTERDAY)?\s*(\d+)",
    re.IGNORECASE,
)
_CLI_MAX_TIME_RE = re.compile(
    r"MAXIMUM\s+TEMPERATURE[^\n]*\n[^\n]*?(\d{1,2}:\d{2}\s*[AP]M)",
    re.IGNORECASE,
)
_CLI_MIN_RE = re.compile(
    r"MINIMUM\s+TEMPERATURE[^\n]*\n\s*(?:TODAY|YESTERDAY)?\s*(\d+)",
    re.IGNORECASE,
)
_CLI_VALID_RE = re.compile(
    r"(?:VALID|AS\s+OF)[:\s]+([^\n]+)",
    re.IGNORECASE,
)
_CLI_PRELIMINARY_RE = re.compile(r"PRELIMINARY", re.IGNORECASE)


# ── Scraper class ────────────────────────────────────────────────────


class NWSScraper:
    """Fetches and parses data from 4 NWS sources.

    Follows the WeatherAPI pattern: httpx.Client + RateLimiter.
    """

    def __init__(
        self, timeout: float = 15.0, config: Config = DEFAULT_CONFIG,
    ) -> None:
        self._client = httpx.Client(timeout=timeout, headers=_HEADERS)
        self._config = config
        self._rate_limiter = RateLimiter(config.rate_limit.nws_requests_per_second)
        logger.info("NWSScraper initialized")

    def close(self) -> None:
        self._client.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def _get(self, url: str) -> httpx.Response:
        """Rate-limited GET with retry."""
        return request_with_retry(
            self._client, "GET", url,
            rate_limiter=self._rate_limiter,
            config=self._config.rate_limit,
        )

    # ── Source 1: Raw METAR ──────────────────────────────────────────

    def get_raw_metar(self, icao: str) -> Optional[MetarObservation]:
        """Fetch raw METAR text and parse with metar_parser.

        URL: tgftp.nws.noaa.gov/data/observations/metar/stations/{ICAO}.TXT
        """
        url = f"https://tgftp.nws.noaa.gov/data/observations/metar/stations/{icao}.TXT"
        try:
            resp = self._get(url)
            raw_text = resp.text
        except Exception:
            logger.warning("Failed to fetch raw METAR for %s", icao, exc_info=True)
            return None

        return parse_raw_metar(raw_text, icao)

    # ── Source 2: Current Conditions ─────────────────────────────────

    def get_current_conditions(self, icao: str) -> Optional[CurrentConditions]:
        """Fetch current conditions HTML page with decimal °F.

        URL: tgftp.nws.noaa.gov/weather/current/{ICAO}.html
        """
        url = f"https://tgftp.nws.noaa.gov/weather/current/{icao}.html"
        try:
            resp = self._get(url)
            html = resp.text
        except Exception:
            logger.warning(
                "Failed to fetch current conditions for %s", icao, exc_info=True
            )
            return None

        return self._parse_current_conditions(html, icao)

    @staticmethod
    def _parse_current_conditions(html: str, icao: str) -> CurrentConditions:
        """Parse temperature and extremes from current conditions HTML."""
        cc = CurrentConditions(station_icao=icao)

        # Primary temperature
        m = _TEMP_RE.search(html)
        if not m:
            m = _TEMP_SIMPLE_RE.search(html)
        if m:
            try:
                cc.temp_f = float(m.group(1))
                cc.temp_c = float(m.group(2))
            except (ValueError, IndexError):
                pass

        # 6-hour extremes
        m6max = _6HR_MAX_RE.search(html)
        if m6max:
            try:
                cc.six_hr_max_f = float(m6max.group(1))
            except ValueError:
                pass

        m6min = _6HR_MIN_RE.search(html)
        if m6min:
            try:
                cc.six_hr_min_f = float(m6min.group(1))
            except ValueError:
                pass

        # 24-hour max
        m24 = _24HR_MAX_RE.search(html)
        if m24:
            try:
                cc.twenty_four_hr_max_f = float(m24.group(1))
            except ValueError:
                pass

        return cc

    # ── Source 3: Observation History ────────────────────────────────

    def get_observation_history(self, icao: str) -> Optional[ObservationHistory]:
        """Fetch observation history HTML table.

        URL: forecast.weather.gov/data/obhistory/{ICAO}.html
        """
        url = f"https://forecast.weather.gov/data/obhistory/{icao}.html"
        try:
            resp = self._get(url)
            html = resp.text
        except Exception:
            logger.warning(
                "Failed to fetch observation history for %s", icao, exc_info=True
            )
            return None

        return self._parse_observation_history(html, icao)

    @staticmethod
    def _parse_observation_history(html: str, icao: str) -> ObservationHistory:
        """Parse observation history rows from HTML table."""
        entries: list[ObsHistoryEntry] = []
        temps: list[float] = []

        for m in _OBS_ROW_RE.finditer(html):
            date_str = m.group(1).strip()
            time_str = m.group(2).strip()
            try:
                temp_f = float(m.group(3))
                dew_f = float(m.group(4))
            except (ValueError, IndexError):
                continue

            entry = ObsHistoryEntry(
                date_str=date_str,
                time_str=time_str,
                temp_f=temp_f,
                dewpoint_f=dew_f,
            )
            entries.append(entry)
            temps.append(temp_f)

        max_temp = max(temps) if temps else None

        return ObservationHistory(
            station_icao=icao,
            entries=entries,
            max_temp_f=max_temp,
        )

    # ── Source 4: Preliminary CLI ────────────────────────────────────

    def get_preliminary_cli(self, cli_code: str) -> Optional[CliReport]:
        """Fetch the latest CLI product for a station.

        URL: forecast.weather.gov/product.php?site=NWS&product=CLI&issuedby={CODE}
        """
        url = (
            "https://forecast.weather.gov/product.php"
            f"?site=NWS&product=CLI&issuedby={cli_code}"
        )
        try:
            resp = self._get(url)
            text = resp.text
        except Exception:
            logger.warning(
                "Failed to fetch CLI for %s", cli_code, exc_info=True
            )
            return None

        return self._parse_cli_product(text, cli_code)

    @staticmethod
    def _parse_cli_product(text: str, cli_code: str) -> CliReport:
        """Parse max/min temperatures from CLI product text."""
        report = CliReport(cli_code=cli_code, raw_text=text)

        # Maximum temperature
        m_max = _CLI_MAX_RE.search(text)
        if m_max:
            try:
                report.max_temp_f = int(m_max.group(1))
            except ValueError:
                pass

        # Max temperature time
        m_time = _CLI_MAX_TIME_RE.search(text)
        if m_time:
            report.max_temp_time = m_time.group(1).strip()

        # Minimum temperature
        m_min = _CLI_MIN_RE.search(text)
        if m_min:
            try:
                report.min_temp_f = int(m_min.group(1))
            except ValueError:
                pass

        # Valid-as-of timestamp
        m_valid = _CLI_VALID_RE.search(text)
        if m_valid:
            report.valid_as_of = m_valid.group(1).strip()

        # Preliminary flag
        report.is_preliminary = bool(_CLI_PRELIMINARY_RE.search(text))

        return report
