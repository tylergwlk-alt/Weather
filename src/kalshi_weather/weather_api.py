"""NWS Weather API client — fetches observations and forecasts.

Uses api.weather.gov (no API key required, just User-Agent header).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

import httpx

from kalshi_weather.config import DEFAULT_CONFIG, Config
from kalshi_weather.rate_limiter import RateLimiter, request_with_retry

logger = logging.getLogger(__name__)

_BASE = "https://api.weather.gov"
_HEADERS = {
    "User-Agent": "(kalshi-weather-scanner, contact@example.com)",
    "Accept": "application/geo+json",
}


@dataclass
class CurrentObs:
    """Parsed current observation from a station."""

    station_icao: str
    timestamp: str
    temp_c: Optional[float] = None
    temp_f: Optional[float] = None
    text_description: str = ""
    raw_json: dict = field(default_factory=dict, repr=False)


@dataclass
class HourlyForecastPeriod:
    """A single hourly forecast period."""

    start_time: str
    end_time: str
    temp_f: Optional[float] = None
    short_forecast: str = ""


@dataclass
class StationForecast:
    """Hourly forecast for a station's grid point."""

    station_icao: str
    periods: list[HourlyForecastPeriod] = field(default_factory=list)
    forecast_high_f: Optional[float] = None
    forecast_low_f: Optional[float] = None


def _c_to_f(c: Optional[float]) -> Optional[float]:
    if c is None:
        return None
    return round(c * 9 / 5 + 32, 1)


class WeatherAPI:
    """Client for the NWS api.weather.gov."""

    def __init__(
        self, timeout: float = 15.0, config: Config = DEFAULT_CONFIG,
    ) -> None:
        self._client = httpx.Client(timeout=timeout, headers=_HEADERS)
        self._config = config
        self._rate_limiter = RateLimiter(config.rate_limit.nws_requests_per_second)
        logger.info("WeatherAPI initialized")

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

    def get_current_obs(self, station_icao: str) -> Optional[CurrentObs]:
        """Fetch latest observation for a station (e.g. 'KORD')."""
        url = f"{_BASE}/stations/{station_icao}/observations/latest"
        try:
            resp = self._get(url)
            data = resp.json()
        except Exception:
            logger.warning("Failed to fetch obs for %s", station_icao, exc_info=True)
            return None

        props = data.get("properties", {})
        temp_c = None
        temp_val = props.get("temperature", {})
        if isinstance(temp_val, dict):
            temp_c = temp_val.get("value")

        return CurrentObs(
            station_icao=station_icao,
            timestamp=props.get("timestamp", ""),
            temp_c=temp_c,
            temp_f=_c_to_f(temp_c),
            text_description=props.get("textDescription", ""),
            raw_json=data,
        )

    def _get_gridpoint_url(self, station_icao: str) -> Optional[str]:
        """Resolve a station ICAO to its gridpoint forecast URL."""
        # Step 1: Get station metadata for coordinates.
        url = f"{_BASE}/stations/{station_icao}"
        try:
            resp = self._get(url)
            data = resp.json()
        except Exception:
            logger.warning("Failed to get station metadata for %s", station_icao, exc_info=True)
            return None

        coords = data.get("geometry", {}).get("coordinates", [])
        if len(coords) < 2:
            return None
        lon, lat = coords[0], coords[1]

        # Step 2: Get gridpoint from /points.
        points_url = f"{_BASE}/points/{lat},{lon}"
        try:
            resp = self._get(points_url)
            points_data = resp.json()
        except Exception:
            logger.warning("Failed to get points for %s", station_icao, exc_info=True)
            return None

        return points_data.get("properties", {}).get("forecastHourly")

    def get_hourly_forecast(self, station_icao: str) -> Optional[StationForecast]:
        """Fetch hourly forecast for the grid point nearest a station."""
        forecast_url = self._get_gridpoint_url(station_icao)
        if not forecast_url:
            return None

        try:
            resp = self._get(forecast_url)
            data = resp.json()
        except Exception:
            logger.warning(
                "Failed to fetch hourly forecast for %s", station_icao, exc_info=True
            )
            return None

        periods_raw = data.get("properties", {}).get("periods", [])
        periods: list[HourlyForecastPeriod] = []
        temps: list[float] = []

        for p in periods_raw:
            temp = p.get("temperature")
            temp_f = float(temp) if temp is not None else None
            period = HourlyForecastPeriod(
                start_time=p.get("startTime", ""),
                end_time=p.get("endTime", ""),
                temp_f=temp_f,
                short_forecast=p.get("shortForecast", ""),
            )
            periods.append(period)
            if temp_f is not None:
                temps.append(temp_f)

        forecast_high = max(temps) if temps else None
        forecast_low = min(temps) if temps else None

        return StationForecast(
            station_icao=station_icao,
            periods=periods,
            forecast_high_f=forecast_high,
            forecast_low_f=forecast_low,
        )
