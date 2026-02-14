"""Phase 3 tests — Rules & Settlement Specialist."""

from __future__ import annotations

from datetime import datetime

from kalshi_weather.rules import (
    build_settlement_spec,
    get_cli_day_window,
    get_station_icao,
    get_station_timezone,
    lookup_station,
)
from kalshi_weather.schemas import MappingConfidence, MarketType

# ── Station lookup ─────────────────────────────────────────────────────

class TestLookupStation:
    def test_exact_match(self):
        entry = lookup_station("Chicago")
        assert entry is not None
        assert entry["station_icao"] == "KMDW"

    def test_case_insensitive(self):
        entry = lookup_station("CHICAGO")
        assert entry is not None
        assert entry["station_icao"] == "KMDW"

    def test_alias_match(self):
        entry = lookup_station("NYC")
        assert entry is not None
        assert entry["station_icao"] == "KNYC"

    def test_alias_full_name(self):
        entry = lookup_station("New York City")
        assert entry is not None
        assert entry["station_icao"] == "KNYC"

    def test_substring_match(self):
        # "Highest temperature in Chicago" contains "chicago"
        entry = lookup_station("chicago")
        assert entry is not None

    def test_unknown_city(self):
        entry = lookup_station("Timbuktu")
        assert entry is None

    def test_all_major_cities_mapped(self):
        cities = [
            "New York", "Chicago", "Miami", "Austin",
            "Los Angeles", "Denver", "Las Vegas", "Seattle",
            "Atlanta", "Boston", "Dallas", "Houston",
            "Phoenix", "San Francisco", "Washington",
        ]
        for city in cities:
            entry = lookup_station(city)
            assert entry is not None, f"City '{city}' not found in station DB"
            assert entry["confidence"] == MappingConfidence.HIGH


class TestStationHelpers:
    def test_get_timezone(self):
        assert get_station_timezone("Denver") == "America/Denver"
        assert get_station_timezone("Phoenix") == "America/Phoenix"
        assert get_station_timezone("Unknown") is None

    def test_get_icao(self):
        assert get_station_icao("Miami") == "KMIA"
        assert get_station_icao("Austin") == "KAUS"
        assert get_station_icao("Unknown") is None


# ── CLI day window ─────────────────────────────────────────────────────

class TestCliDayWindow:
    def test_eastern_standard_time(self):
        """EST = UTC-5. CLI day midnight-midnight EST = 05:00Z to 05:00Z+1."""
        target = datetime(2026, 1, 15)  # January = standard time
        start, end = get_cli_day_window(target, "America/New_York")
        assert start.hour == 5  # midnight EST = 05:00 UTC
        assert end.hour == 5
        assert (end - start).total_seconds() == 86400

    def test_eastern_during_dst(self):
        """During DST, CLI day still uses LST (EST = UTC-5), NOT EDT."""
        target = datetime(2026, 7, 15)  # July = DST active
        start, end = get_cli_day_window(target, "America/New_York")
        # Should still be UTC-5 (EST), not UTC-4 (EDT)
        assert start.hour == 5
        assert end.hour == 5

    def test_central_standard(self):
        """CST = UTC-6."""
        target = datetime(2026, 1, 15)
        start, end = get_cli_day_window(target, "America/Chicago")
        assert start.hour == 6
        assert end.hour == 6

    def test_mountain_standard(self):
        """MST = UTC-7."""
        target = datetime(2026, 1, 15)
        start, end = get_cli_day_window(target, "America/Denver")
        assert start.hour == 7

    def test_pacific_standard(self):
        """PST = UTC-8."""
        target = datetime(2026, 1, 15)
        start, end = get_cli_day_window(target, "America/Los_Angeles")
        assert start.hour == 8

    def test_phoenix_no_dst(self):
        """Arizona doesn't observe DST — MST year-round = UTC-7."""
        winter = datetime(2026, 1, 15)
        summer = datetime(2026, 7, 15)
        start_w, _ = get_cli_day_window(winter, "America/Phoenix")
        start_s, _ = get_cli_day_window(summer, "America/Phoenix")
        assert start_w.hour == 7
        assert start_s.hour == 7  # Same — no DST shift


# ── Settlement spec construction ───────────────────────────────────────

class TestBuildSettlementSpec:
    def test_known_city_high(self):
        spec = build_settlement_spec("Chicago", MarketType.HIGH_TEMP)
        assert spec.city == "Chicago"
        assert spec.market_type == MarketType.HIGH_TEMP
        assert spec.issuedby == "MDW"
        assert "CLI" in spec.cli_url
        assert spec.what_to_read_in_cli == "MAXIMUM TEMPERATURE"
        assert spec.mapping_confidence == MappingConfidence.HIGH
        assert "KMDW" in spec.mapping_notes[0]

    def test_known_city_low(self):
        spec = build_settlement_spec("Miami", MarketType.LOW_TEMP)
        assert spec.what_to_read_in_cli == "MINIMUM TEMPERATURE"
        assert spec.issuedby == "MIA"
        assert spec.mapping_confidence == MappingConfidence.HIGH

    def test_with_target_date(self):
        target = datetime(2026, 2, 12)
        spec = build_settlement_spec("Denver", MarketType.HIGH_TEMP, target)
        assert "UTC window" in spec.day_window_note
        assert "07:00Z" in spec.day_window_note

    def test_unknown_city(self):
        spec = build_settlement_spec("Timbuktu", MarketType.HIGH_TEMP)
        assert spec.mapping_confidence == MappingConfidence.LOW
        assert spec.issuedby == "UNKNOWN"
        assert "not found" in spec.mapping_notes[0]

    def test_phoenix_special_risk(self):
        spec = build_settlement_spec("Phoenix", MarketType.HIGH_TEMP)
        risks = " ".join(spec.special_risks)
        assert "DST" in risks

    def test_alias_resolution(self):
        spec = build_settlement_spec("Washington D.C.", MarketType.HIGH_TEMP)
        assert spec.issuedby == "DCA"
        assert spec.mapping_confidence == MappingConfidence.HIGH

    def test_cli_url_format(self):
        spec = build_settlement_spec("Seattle", MarketType.HIGH_TEMP)
        assert spec.cli_url == (
            "https://forecast.weather.gov/product.php"
            "?site=NWS&product=CLI&issuedby=SEA"
        )
