"""Rules & Settlement Specialist — Phase 3.

Maps Kalshi cities to NWS CLI stations, defines settlement day windows,
computes mapping confidence, and flags special risks.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

from kalshi_weather.schemas import MappingConfidence, MarketType, SettlementSpec

logger = logging.getLogger(__name__)

# ── NWS CLI URL template ──────────────────────────────────────────────

_CLI_URL = (
    "https://forecast.weather.gov/product.php"
    "?site=NWS&product=CLI&issuedby={issuedby}"
)

# ── Station database ──────────────────────────────────────────────────
# Source: wethr.net/edu/city-resources + Kalshi help center + NWS CLI pages.
#
# Fields:
#   kalshi_city     — city name as it appears in Kalshi event titles
#   station_icao    — ICAO station code used for METAR / observation
#   cli_issuedby    — 3-letter code for NWS CLI product (?issuedby=XXX)
#   timezone        — IANA timezone (for local standard time logic)
#   cli_field_high  — field label in CLI for daily max temperature
#   cli_field_low   — field label in CLI for daily min temperature
#   confidence      — mapping confidence (HIGH if confirmed via Kalshi rules + CLI)
#   notes           — any special considerations

_STATION_DB: list[dict] = [
    {
        "kalshi_city": "New York",
        "aliases": ["NYC", "New York City"],
        "station_icao": "KNYC",
        "cli_issuedby": "NYC",
        "timezone": "America/New_York",
        "cli_field_high": "MAXIMUM TEMPERATURE",
        "cli_field_low": "MINIMUM TEMPERATURE",
        "confidence": MappingConfidence.HIGH,
        "notes": ["Central Park observation site"],
    },
    {
        "kalshi_city": "Chicago",
        "aliases": [],
        "station_icao": "KMDW",
        "cli_issuedby": "MDW",
        "timezone": "America/Chicago",
        "cli_field_high": "MAXIMUM TEMPERATURE",
        "cli_field_low": "MINIMUM TEMPERATURE",
        "confidence": MappingConfidence.HIGH,
        "notes": ["Midway Airport; some Kalshi markets may use KORD (O'Hare)"],
    },
    {
        "kalshi_city": "Miami",
        "aliases": [],
        "station_icao": "KMIA",
        "cli_issuedby": "MIA",
        "timezone": "America/New_York",
        "cli_field_high": "MAXIMUM TEMPERATURE",
        "cli_field_low": "MINIMUM TEMPERATURE",
        "confidence": MappingConfidence.HIGH,
        "notes": ["Miami International Airport"],
    },
    {
        "kalshi_city": "Austin",
        "aliases": [],
        "station_icao": "KAUS",
        "cli_issuedby": "AUS",
        "timezone": "America/Chicago",
        "cli_field_high": "MAXIMUM TEMPERATURE",
        "cli_field_low": "MINIMUM TEMPERATURE",
        "confidence": MappingConfidence.HIGH,
        "notes": ["Austin-Bergstrom International Airport"],
    },
    {
        "kalshi_city": "Los Angeles",
        "aliases": ["LA"],
        "station_icao": "KLAX",
        "cli_issuedby": "LAX",
        "timezone": "America/Los_Angeles",
        "cli_field_high": "MAXIMUM TEMPERATURE",
        "cli_field_low": "MINIMUM TEMPERATURE",
        "confidence": MappingConfidence.HIGH,
        "notes": ["LAX airport observation"],
    },
    {
        "kalshi_city": "Denver",
        "aliases": [],
        "station_icao": "KDEN",
        "cli_issuedby": "DEN",
        "timezone": "America/Denver",
        "cli_field_high": "MAXIMUM TEMPERATURE",
        "cli_field_low": "MINIMUM TEMPERATURE",
        "confidence": MappingConfidence.HIGH,
        "notes": ["Denver International Airport"],
    },
    {
        "kalshi_city": "Las Vegas",
        "aliases": [],
        "station_icao": "KLAS",
        "cli_issuedby": "LAS",
        "timezone": "America/Los_Angeles",
        "cli_field_high": "MAXIMUM TEMPERATURE",
        "cli_field_low": "MINIMUM TEMPERATURE",
        "confidence": MappingConfidence.HIGH,
        "notes": ["Harry Reid International Airport"],
    },
    {
        "kalshi_city": "Seattle",
        "aliases": [],
        "station_icao": "KSEA",
        "cli_issuedby": "SEA",
        "timezone": "America/Los_Angeles",
        "cli_field_high": "MAXIMUM TEMPERATURE",
        "cli_field_low": "MINIMUM TEMPERATURE",
        "confidence": MappingConfidence.HIGH,
        "notes": ["Seattle-Tacoma International Airport"],
    },
    {
        "kalshi_city": "Atlanta",
        "aliases": [],
        "station_icao": "KATL",
        "cli_issuedby": "ATL",
        "timezone": "America/New_York",
        "cli_field_high": "MAXIMUM TEMPERATURE",
        "cli_field_low": "MINIMUM TEMPERATURE",
        "confidence": MappingConfidence.HIGH,
        "notes": ["Hartsfield-Jackson Atlanta International Airport"],
    },
    {
        "kalshi_city": "Boston",
        "aliases": [],
        "station_icao": "KBOS",
        "cli_issuedby": "BOS",
        "timezone": "America/New_York",
        "cli_field_high": "MAXIMUM TEMPERATURE",
        "cli_field_low": "MINIMUM TEMPERATURE",
        "confidence": MappingConfidence.HIGH,
        "notes": ["Logan International Airport"],
    },
    {
        "kalshi_city": "Charlotte",
        "aliases": [],
        "station_icao": "KCLT",
        "cli_issuedby": "CLT",
        "timezone": "America/New_York",
        "cli_field_high": "MAXIMUM TEMPERATURE",
        "cli_field_low": "MINIMUM TEMPERATURE",
        "confidence": MappingConfidence.HIGH,
        "notes": ["Charlotte Douglas International Airport"],
    },
    {
        "kalshi_city": "Dallas",
        "aliases": ["Dallas-Fort Worth", "DFW"],
        "station_icao": "KDFW",
        "cli_issuedby": "DFW",
        "timezone": "America/Chicago",
        "cli_field_high": "MAXIMUM TEMPERATURE",
        "cli_field_low": "MINIMUM TEMPERATURE",
        "confidence": MappingConfidence.HIGH,
        "notes": ["Dallas/Fort Worth International Airport"],
    },
    {
        "kalshi_city": "Detroit",
        "aliases": [],
        "station_icao": "KDTW",
        "cli_issuedby": "DTW",
        "timezone": "America/Detroit",
        "cli_field_high": "MAXIMUM TEMPERATURE",
        "cli_field_low": "MINIMUM TEMPERATURE",
        "confidence": MappingConfidence.HIGH,
        "notes": ["Detroit Metropolitan Airport"],
    },
    {
        "kalshi_city": "Houston",
        "aliases": [],
        "station_icao": "KHOU",
        "cli_issuedby": "HOU",
        "timezone": "America/Chicago",
        "cli_field_high": "MAXIMUM TEMPERATURE",
        "cli_field_low": "MINIMUM TEMPERATURE",
        "confidence": MappingConfidence.HIGH,
        "notes": ["William P. Hobby Airport"],
    },
    {
        "kalshi_city": "Jacksonville",
        "aliases": [],
        "station_icao": "KJAX",
        "cli_issuedby": "JAX",
        "timezone": "America/New_York",
        "cli_field_high": "MAXIMUM TEMPERATURE",
        "cli_field_low": "MINIMUM TEMPERATURE",
        "confidence": MappingConfidence.HIGH,
        "notes": ["Jacksonville International Airport"],
    },
    {
        "kalshi_city": "Minneapolis",
        "aliases": [],
        "station_icao": "KMSP",
        "cli_issuedby": "MSP",
        "timezone": "America/Chicago",
        "cli_field_high": "MAXIMUM TEMPERATURE",
        "cli_field_low": "MINIMUM TEMPERATURE",
        "confidence": MappingConfidence.HIGH,
        "notes": ["Minneapolis-Saint Paul International Airport"],
    },
    {
        "kalshi_city": "Nashville",
        "aliases": [],
        "station_icao": "KBNA",
        "cli_issuedby": "BNA",
        "timezone": "America/Chicago",
        "cli_field_high": "MAXIMUM TEMPERATURE",
        "cli_field_low": "MINIMUM TEMPERATURE",
        "confidence": MappingConfidence.HIGH,
        "notes": ["Nashville International Airport"],
    },
    {
        "kalshi_city": "New Orleans",
        "aliases": [],
        "station_icao": "KMSY",
        "cli_issuedby": "MSY",
        "timezone": "America/Chicago",
        "cli_field_high": "MAXIMUM TEMPERATURE",
        "cli_field_low": "MINIMUM TEMPERATURE",
        "confidence": MappingConfidence.HIGH,
        "notes": ["Louis Armstrong New Orleans International Airport"],
    },
    {
        "kalshi_city": "Oklahoma City",
        "aliases": ["OKC"],
        "station_icao": "KOKC",
        "cli_issuedby": "OKC",
        "timezone": "America/Chicago",
        "cli_field_high": "MAXIMUM TEMPERATURE",
        "cli_field_low": "MINIMUM TEMPERATURE",
        "confidence": MappingConfidence.HIGH,
        "notes": ["Will Rogers World Airport"],
    },
    {
        "kalshi_city": "Philadelphia",
        "aliases": ["Philly"],
        "station_icao": "KPHL",
        "cli_issuedby": "PHL",
        "timezone": "America/New_York",
        "cli_field_high": "MAXIMUM TEMPERATURE",
        "cli_field_low": "MINIMUM TEMPERATURE",
        "confidence": MappingConfidence.HIGH,
        "notes": ["Philadelphia International Airport"],
    },
    {
        "kalshi_city": "Phoenix",
        "aliases": [],
        "station_icao": "KPHX",
        "cli_issuedby": "PHX",
        "timezone": "America/Phoenix",
        "cli_field_high": "MAXIMUM TEMPERATURE",
        "cli_field_low": "MINIMUM TEMPERATURE",
        "confidence": MappingConfidence.HIGH,
        "notes": ["Phoenix Sky Harbor; Arizona does not observe DST"],
    },
    {
        "kalshi_city": "San Antonio",
        "aliases": [],
        "station_icao": "KSAT",
        "cli_issuedby": "SAT",
        "timezone": "America/Chicago",
        "cli_field_high": "MAXIMUM TEMPERATURE",
        "cli_field_low": "MINIMUM TEMPERATURE",
        "confidence": MappingConfidence.HIGH,
        "notes": ["San Antonio International Airport"],
    },
    {
        "kalshi_city": "San Francisco",
        "aliases": ["SF"],
        "station_icao": "KSFO",
        "cli_issuedby": "SFO",
        "timezone": "America/Los_Angeles",
        "cli_field_high": "MAXIMUM TEMPERATURE",
        "cli_field_low": "MINIMUM TEMPERATURE",
        "confidence": MappingConfidence.HIGH,
        "notes": ["San Francisco International Airport"],
    },
    {
        "kalshi_city": "Tampa",
        "aliases": [],
        "station_icao": "KTPA",
        "cli_issuedby": "TPA",
        "timezone": "America/New_York",
        "cli_field_high": "MAXIMUM TEMPERATURE",
        "cli_field_low": "MINIMUM TEMPERATURE",
        "confidence": MappingConfidence.HIGH,
        "notes": ["Tampa International Airport"],
    },
    {
        "kalshi_city": "Washington",
        "aliases": ["Washington D.C.", "DC", "Washington DC"],
        "station_icao": "KDCA",
        "cli_issuedby": "DCA",
        "timezone": "America/New_York",
        "cli_field_high": "MAXIMUM TEMPERATURE",
        "cli_field_low": "MINIMUM TEMPERATURE",
        "confidence": MappingConfidence.HIGH,
        "notes": ["Reagan National Airport"],
    },
    {
        "kalshi_city": "LaGuardia",
        "aliases": ["LGA"],
        "station_icao": "KLGA",
        "cli_issuedby": "LGA",
        "timezone": "America/New_York",
        "cli_field_high": "MAXIMUM TEMPERATURE",
        "cli_field_low": "MINIMUM TEMPERATURE",
        "confidence": MappingConfidence.MED,
        "notes": [
            "LaGuardia Airport; less common Kalshi market",
            "Verify ticker mapping before trading",
        ],
    },
]

# Build lookup indices.
_CITY_INDEX: dict[str, dict] = {}
for _entry in _STATION_DB:
    _key = _entry["kalshi_city"].lower()
    _CITY_INDEX[_key] = _entry
    for _alias in _entry.get("aliases", []):
        _CITY_INDEX[_alias.lower()] = _entry


# ── Public API ─────────────────────────────────────────────────────────

def lookup_station(city: str) -> Optional[dict]:
    """Look up a station entry by city name (case-insensitive, alias-aware)."""
    key = city.strip().lower()
    if key in _CITY_INDEX:
        return _CITY_INDEX[key]
    # Fuzzy: try substring match.
    for idx_key, entry in _CITY_INDEX.items():
        if idx_key in key or key in idx_key:
            return entry
    return None


def get_cli_day_window(
    target_date: datetime,
    timezone_str: str,
) -> tuple[datetime, datetime]:
    """Compute the NWS CLI day window for a given date and timezone.

    The CLI climate day runs from midnight to midnight LOCAL STANDARD TIME.
    During DST, the actual UTC window shifts but the LST window stays fixed.

    Returns (start_utc, end_utc) — the UTC boundaries of the CLI day.
    """
    tz = ZoneInfo(timezone_str)

    # Get the standard UTC offset (January 1 is always standard time in the US).
    jan1 = datetime(target_date.year, 1, 1, tzinfo=tz)
    std_offset = jan1.utcoffset()

    # CLI day: midnight-to-midnight in local standard time.
    # Convert to UTC using the standard offset (NOT the DST-aware offset).
    start_lst = datetime(
        target_date.year, target_date.month, target_date.day, 0, 0, 0
    )
    end_lst = start_lst + timedelta(days=1)

    start_utc = (start_lst - std_offset).replace(tzinfo=ZoneInfo("UTC"))
    end_utc = (end_lst - std_offset).replace(tzinfo=ZoneInfo("UTC"))

    return start_utc, end_utc


def build_settlement_spec(
    city: str,
    market_type: MarketType,
    target_date: Optional[datetime] = None,
) -> SettlementSpec:
    """Build a SettlementSpec for a city + market type.

    If the city cannot be mapped, returns a spec with LOW confidence.
    """
    entry = lookup_station(city)

    if entry is None:
        return SettlementSpec(
            city=city,
            market_type=market_type,
            issuedby="UNKNOWN",
            cli_url="",
            what_to_read_in_cli="UNKNOWN",
            day_window_note="Cannot determine — city not in station database",
            special_risks=["UNMAPPED CITY — cannot determine settlement source"],
            mapping_confidence=MappingConfidence.LOW,
            mapping_notes=[f"City '{city}' not found in station database"],
        )

    issuedby = entry["cli_issuedby"]
    cli_url = _CLI_URL.format(issuedby=issuedby)

    if market_type == MarketType.HIGH_TEMP:
        cli_field = entry["cli_field_high"]
    else:
        cli_field = entry["cli_field_low"]

    # Day window note.
    tz_str = entry["timezone"]
    if target_date is not None:
        start_utc, end_utc = get_cli_day_window(target_date, tz_str)
        day_note = (
            f"CLI day = midnight-midnight LST ({tz_str}); "
            f"UTC window: {start_utc.strftime('%H:%M')}Z — {end_utc.strftime('%H:%M')}Z"
        )
    else:
        day_note = f"CLI day = midnight-midnight LST ({tz_str})"

    special_risks = list(entry.get("notes", []))

    # Phoenix DST exception.
    if tz_str == "America/Phoenix":
        special_risks.append("Arizona does not observe DST — no LST/LDT shift")

    return SettlementSpec(
        city=city,
        market_type=market_type,
        issuedby=issuedby,
        cli_url=cli_url,
        what_to_read_in_cli=cli_field,
        day_window_note=day_note,
        special_risks=special_risks,
        mapping_confidence=entry["confidence"],
        mapping_notes=[f"Station: {entry['station_icao']} ({entry['kalshi_city']})"],
    )


def build_all_settlement_specs(
    cities_and_types: list[tuple[str, MarketType]],
    target_date: Optional[datetime] = None,
) -> list[SettlementSpec]:
    """Build SettlementSpecs for a batch of (city, market_type) pairs."""
    return [
        build_settlement_spec(city, mt, target_date)
        for city, mt in cities_and_types
    ]


def get_station_timezone(city: str) -> Optional[str]:
    """Return the IANA timezone string for a city, or None if unmapped."""
    entry = lookup_station(city)
    return entry["timezone"] if entry else None


def get_station_icao(city: str) -> Optional[str]:
    """Return the ICAO station code for a city, or None if unmapped."""
    entry = lookup_station(city)
    return entry["station_icao"] if entry else None
