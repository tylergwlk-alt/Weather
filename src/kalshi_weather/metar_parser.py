"""METAR parser — extracts precise temperatures from raw METAR text.

Parses the T-group (tenths °C), 6-hr/24-hr extremes, and standard
METAR temperature fields.  Implements NWS-style rounding (half-up)
for CLI settlement prediction.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Optional


@dataclass
class MetarObservation:
    """Parsed observation from a raw METAR string."""

    station_icao: str
    observation_time_utc: Optional[datetime]
    raw_text: str
    temp_c_tenths: Optional[float]       # T-group: e.g. 3.9
    dewpoint_c_tenths: Optional[float]    # T-group: e.g. -10.6
    temp_f_precise: Optional[float]       # e.g. 39.02
    temp_c_rounded: Optional[int]         # standard METAR: e.g. 4
    has_t_group: bool
    six_hr_max_c: Optional[float]         # 1xxxx group
    six_hr_min_c: Optional[float]         # 2xxxx group
    twenty_four_hr_max_c: Optional[float]  # 4xxxx group (max)
    twenty_four_hr_min_c: Optional[float]  # 4xxxx group (min)


# ── Conversion helpers ───────────────────────────────────────────────


def nws_round(value: float) -> int:
    """NWS-style rounding: half rounds UP (not banker's rounding).

    >>> nws_round(39.5)
    40
    >>> nws_round(39.4999)
    39
    """
    return math.floor(value + 0.5)


def c_to_f_precise(c: float) -> float:
    """Convert Celsius to Fahrenheit with full precision."""
    return c * 9.0 / 5.0 + 32.0


def c_to_f_cli_rounded(c: float) -> int:
    """Convert Celsius to Fahrenheit using NWS CLI rounding.

    This mirrors the NWS ASOS pipeline: convert tenths-°C to °F,
    then round half-up to the nearest integer.
    """
    return nws_round(c_to_f_precise(c))


def get_f_boundary_c(n_f: int) -> float:
    """Return the °C threshold where CLI rounds UP to (n_f + 1)°F.

    At exactly this temperature, cli_rounded = n_f + 1.
    Below it, cli_rounded = n_f.

    Example: get_f_boundary_c(39) = 4.1667°C — the boundary between 39F and 40F.
    """
    return (n_f + 0.5 - 32.0) * 5.0 / 9.0


# ── METAR field parsers ──────────────────────────────────────────────

# T-group in remarks: T followed by 8 digits.
# Format: T[sign_t][3 digits temp][sign_d][3 digits dewpoint]
# sign: 0 = positive, 1 = negative
_T_GROUP_RE = re.compile(r"\bT(\d)(\d{3})(\d)(\d{3})\b")


def parse_t_group(remarks: str) -> tuple[Optional[float], Optional[float]]:
    """Parse the T-group from METAR remarks.

    Returns (temp_c_tenths, dewpoint_c_tenths) or (None, None).
    """
    m = _T_GROUP_RE.search(remarks)
    if not m:
        return None, None

    sign_t, digits_t, sign_d, digits_d = m.groups()
    temp = int(digits_t) / 10.0
    if sign_t == "1":
        temp = -temp
    dew = int(digits_d) / 10.0
    if sign_d == "1":
        dew = -dew
    return temp, dew


# 6-hour max: 1[sign][3 digits]   (reported at 00Z, 06Z, 12Z, 18Z)
_6HR_MAX_RE = re.compile(r"\b1(\d)(\d{3})\b")

# 6-hour min: 2[sign][3 digits]
_6HR_MIN_RE = re.compile(r"\b2(\d)(\d{3})\b")

# 24-hour max/min: 4[sign_max][3 digits][sign_min][3 digits]
_24HR_RE = re.compile(r"\b4(\d)(\d{3})(\d)(\d{3})\b")


def parse_6hr_extremes(remarks: str) -> tuple[Optional[float], Optional[float]]:
    """Parse 6-hour max (1xxxx) and min (2xxxx) from METAR remarks.

    Returns (six_hr_max_c, six_hr_min_c).
    """
    max_c = None
    min_c = None

    m_max = _6HR_MAX_RE.search(remarks)
    if m_max:
        sign, digits = m_max.groups()
        val = int(digits) / 10.0
        if sign == "1":
            val = -val
        max_c = val

    m_min = _6HR_MIN_RE.search(remarks)
    if m_min:
        sign, digits = m_min.groups()
        val = int(digits) / 10.0
        if sign == "1":
            val = -val
        min_c = val

    return max_c, min_c


def parse_24hr_extremes(remarks: str) -> tuple[Optional[float], Optional[float]]:
    """Parse 24-hour max and min (4xxxxxxxx group) from METAR remarks.

    Returns (twenty_four_hr_max_c, twenty_four_hr_min_c).
    """
    m = _24HR_RE.search(remarks)
    if not m:
        return None, None

    sign_max, digits_max, sign_min, digits_min = m.groups()
    max_c = int(digits_max) / 10.0
    if sign_max == "1":
        max_c = -max_c
    min_c = int(digits_min) / 10.0
    if sign_min == "1":
        min_c = -min_c
    return max_c, min_c


# Standard METAR temperature: TT/DD where M prefix means negative.
# e.g. "04/M11" = temp 4°C, dewpoint -11°C
# Use a space lookbehind to avoid matching date components like "02/24"
# in the timestamp header line "2026/02/24 19:53".
_STANDARD_TEMP_RE = re.compile(r"(?<= )(M?\d{2})/(M?\d{2})(?=\s|$)")


def parse_standard_temp(metar: str) -> tuple[Optional[int], Optional[int]]:
    """Parse standard METAR temperature and dewpoint (whole °C).

    Returns (temp_c, dewpoint_c) as integers, or (None, None).
    """
    m = _STANDARD_TEMP_RE.search(metar)
    if not m:
        return None, None

    raw_t, raw_d = m.groups()

    def _parse_val(s: str) -> int:
        if s.startswith("M"):
            return -int(s[1:])
        return int(s)

    return _parse_val(raw_t), _parse_val(raw_d)


# Observation time from the METAR header line.
# e.g. "2026/02/24 20:53" on the first line of NWS METAR text files.
_OBS_TIME_RE = re.compile(r"(\d{4})/(\d{2})/(\d{2})\s+(\d{2}):(\d{2})")


def _parse_obs_time(raw_text: str) -> Optional[datetime]:
    """Extract observation time from the NWS METAR text file header."""
    m = _OBS_TIME_RE.search(raw_text)
    if not m:
        return None
    year, month, day, hour, minute = (int(x) for x in m.groups())
    return datetime(year, month, day, hour, minute)


# ── Main entry point ─────────────────────────────────────────────────


def parse_raw_metar(raw_text: str, station_icao: str) -> MetarObservation:
    """Parse a raw METAR text block into a MetarObservation.

    The raw_text may include a timestamp header line (from NWS METAR files)
    followed by the actual METAR string.
    """
    obs_time = _parse_obs_time(raw_text)

    # T-group from remarks
    temp_c_tenths, dewpoint_c_tenths = parse_t_group(raw_text)
    has_t_group = temp_c_tenths is not None

    # Precise °F from T-group
    temp_f_precise = c_to_f_precise(temp_c_tenths) if temp_c_tenths is not None else None

    # Standard whole-°C temp
    temp_c_rounded, _ = parse_standard_temp(raw_text)

    # 6-hour extremes
    six_hr_max_c, six_hr_min_c = parse_6hr_extremes(raw_text)

    # 24-hour extremes
    twenty_four_hr_max_c, twenty_four_hr_min_c = parse_24hr_extremes(raw_text)

    return MetarObservation(
        station_icao=station_icao,
        observation_time_utc=obs_time,
        raw_text=raw_text,
        temp_c_tenths=temp_c_tenths,
        dewpoint_c_tenths=dewpoint_c_tenths,
        temp_f_precise=temp_f_precise,
        temp_c_rounded=temp_c_rounded,
        has_t_group=has_t_group,
        six_hr_max_c=six_hr_max_c,
        six_hr_min_c=six_hr_min_c,
        twenty_four_hr_max_c=twenty_four_hr_max_c,
        twenty_four_hr_min_c=twenty_four_hr_min_c,
    )
