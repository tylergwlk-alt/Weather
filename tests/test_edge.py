"""Tests for the Temperature Edge Bot — metar_parser, nws_scraper, edge."""

from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

from kalshi_weather.metar_parser import (
    MetarObservation,
    c_to_f_cli_rounded,
    c_to_f_precise,
    get_f_boundary_c,
    nws_round,
    parse_24hr_extremes,
    parse_6hr_extremes,
    parse_raw_metar,
    parse_standard_temp,
    parse_t_group,
)
from kalshi_weather.nws_scraper import NWSScraper
from kalshi_weather.edge import (
    Confidence,
    EdgeReport,
    MarginStatus,
    Signal,
    TemperatureReading,
    TimeRisk,
    classify_margin,
    classify_time_risk,
    compute_bracket_analysis,
    format_edge_report,
    format_edge_summary,
)


# ═══════════════════════════════════════════════════════════════════════
# METAR Parser Tests
# ═══════════════════════════════════════════════════════════════════════


class TestNwsRound:
    """NWS uses standard rounding (half rounds UP), not Python banker's rounding."""

    def test_half_rounds_up(self):
        assert nws_round(39.5) == 40

    def test_just_below_half(self):
        assert nws_round(39.4999) == 39

    def test_exact_integer(self):
        assert nws_round(39.0) == 39

    def test_half_at_zero(self):
        assert nws_round(0.5) == 1

    def test_negative_half(self):
        # -0.5 → floor(-0.5 + 0.5) = floor(0.0) = 0
        assert nws_round(-0.5) == 0

    def test_negative_below_half(self):
        # -0.6 → floor(-0.6 + 0.5) = floor(-0.1) = -1
        assert nws_round(-0.6) == -1

    def test_differs_from_python_round(self):
        # Python's round(0.5) = 0 (banker's rounding), NWS = 1
        assert nws_round(0.5) == 1
        assert round(0.5) == 0  # banker's rounding

    def test_differs_from_python_round_at_2_5(self):
        assert nws_round(2.5) == 3
        assert round(2.5) == 2  # banker's rounding


class TestCtoF:
    """Celsius to Fahrenheit conversion precision."""

    def test_freezing_point(self):
        assert c_to_f_precise(0.0) == 32.0

    def test_boiling_point(self):
        assert c_to_f_precise(100.0) == 212.0

    def test_3_9c_gives_39f(self):
        # 3.9 * 9/5 + 32 = 39.02
        f = c_to_f_precise(3.9)
        assert abs(f - 39.02) < 0.01
        assert c_to_f_cli_rounded(3.9) == 39

    def test_4_2c_gives_40f(self):
        # 4.2 * 9/5 + 32 = 39.56 → rounds to 40
        f = c_to_f_precise(4.2)
        assert abs(f - 39.56) < 0.01
        assert c_to_f_cli_rounded(4.2) == 40

    def test_4_1c_gives_39f(self):
        # 4.1 * 9/5 + 32 = 39.38 → rounds to 39
        assert c_to_f_cli_rounded(4.1) == 39

    def test_negative_temp(self):
        # -10.0 * 9/5 + 32 = 14.0
        assert c_to_f_cli_rounded(-10.0) == 14


class TestBoundaryCalc:
    """The °C boundary where CLI rounds up from N°F to (N+1)°F."""

    def test_boundary_39_40(self):
        # Boundary between 39F and 40F
        boundary = get_f_boundary_c(39)
        # (39 + 0.5 - 32) * 5/9 = 7.5 * 5/9 = 4.1667
        assert abs(boundary - 4.1667) < 0.001

    def test_boundary_at_freezing(self):
        # Boundary between 32F and 33F
        boundary = get_f_boundary_c(32)
        # (32 + 0.5 - 32) * 5/9 = 0.5 * 5/9 = 0.2778
        assert abs(boundary - 0.2778) < 0.001

    def test_boundary_exactly_rounds_up(self):
        # At the boundary, the CLI should round UP
        boundary = get_f_boundary_c(39)
        assert c_to_f_cli_rounded(boundary) == 40

    def test_just_below_boundary_rounds_down(self):
        boundary = get_f_boundary_c(39)
        assert c_to_f_cli_rounded(boundary - 0.001) == 39


class TestParseTGroup:
    """T-group in METAR remarks: T[sign][3dig][sign][3dig]."""

    def test_positive_positive(self):
        temp, dew = parse_t_group("RMK AO2 SLP044 T00390023")
        assert temp == 3.9
        assert dew == 2.3

    def test_positive_negative(self):
        temp, dew = parse_t_group("RMK AO2 T01391106")
        assert temp == 13.9
        assert dew == -10.6

    def test_negative_negative(self):
        temp, dew = parse_t_group("RMK AO2 T10561117")
        assert temp == -5.6
        assert dew == -11.7

    def test_zero_temp(self):
        temp, dew = parse_t_group("RMK AO2 T00001050")
        assert temp == 0.0
        assert dew == -5.0

    def test_no_t_group(self):
        temp, dew = parse_t_group("RMK AO2 SLP044")
        assert temp is None
        assert dew is None

    def test_high_temp(self):
        temp, dew = parse_t_group("RMK AO2 T03720228")
        assert temp == 37.2
        assert dew == 22.8


class TestParse6hrExtremes:
    def test_max_only(self):
        max_c, min_c = parse_6hr_extremes("RMK 10044")
        assert max_c == 4.4
        assert min_c is None

    def test_min_only(self):
        max_c, min_c = parse_6hr_extremes("RMK 21017")
        assert max_c is None
        assert min_c == -1.7

    def test_both(self):
        max_c, min_c = parse_6hr_extremes("RMK 10044 21017")
        assert max_c == 4.4
        assert min_c == -1.7

    def test_negative_max(self):
        max_c, min_c = parse_6hr_extremes("RMK 11017")
        assert max_c == -1.7


class TestParse24hrExtremes:
    def test_normal(self):
        max_c, min_c = parse_24hr_extremes("RMK 400441017")
        assert max_c == 4.4
        assert min_c == -1.7

    def test_both_negative(self):
        max_c, min_c = parse_24hr_extremes("RMK 410171056")
        assert max_c == -1.7
        assert min_c == -5.6

    def test_no_group(self):
        max_c, min_c = parse_24hr_extremes("RMK AO2 SLP044")
        assert max_c is None
        assert min_c is None


class TestParseStandardTemp:
    def test_positive(self):
        temp, dew = parse_standard_temp("KMDW 042053Z 04/M11 A2990")
        assert temp == 4
        assert dew == -11

    def test_both_negative(self):
        temp, dew = parse_standard_temp("KMDW M05/M15")
        assert temp == -5
        assert dew == -15

    def test_no_match(self):
        temp, dew = parse_standard_temp("some random text")
        assert temp is None
        assert dew is None


class TestParseRawMetar:
    """Full METAR parsing integration."""

    SAMPLE_METAR = (
        "2026/02/24 19:53\n"
        "KMDW 241953Z 18008KT 10SM FEW250 04/M11 A2990 RMK AO2 "
        "SLP044 T00391106 10044 21017"
    )

    def test_station_icao(self):
        obs = parse_raw_metar(self.SAMPLE_METAR, "KMDW")
        assert obs.station_icao == "KMDW"

    def test_observation_time(self):
        obs = parse_raw_metar(self.SAMPLE_METAR, "KMDW")
        assert obs.observation_time_utc == datetime(2026, 2, 24, 19, 53)

    def test_t_group_parsed(self):
        obs = parse_raw_metar(self.SAMPLE_METAR, "KMDW")
        assert obs.has_t_group is True
        assert obs.temp_c_tenths == 3.9
        assert obs.dewpoint_c_tenths == -10.6

    def test_precise_fahrenheit(self):
        obs = parse_raw_metar(self.SAMPLE_METAR, "KMDW")
        assert obs.temp_f_precise is not None
        assert abs(obs.temp_f_precise - 39.02) < 0.01

    def test_standard_temp(self):
        obs = parse_raw_metar(self.SAMPLE_METAR, "KMDW")
        assert obs.temp_c_rounded == 4

    def test_6hr_extremes(self):
        obs = parse_raw_metar(self.SAMPLE_METAR, "KMDW")
        assert obs.six_hr_max_c == 4.4
        assert obs.six_hr_min_c == -1.7

    def test_metar_without_t_group(self):
        raw = "2026/02/24 19:53\nKMDW 241953Z 04/M11 A2990 RMK AO2 SLP044"
        obs = parse_raw_metar(raw, "KMDW")
        assert obs.has_t_group is False
        assert obs.temp_c_tenths is None
        assert obs.temp_c_rounded == 4


# ═══════════════════════════════════════════════════════════════════════
# NWS Scraper Parsing Tests (mocked HTTP)
# ═══════════════════════════════════════════════════════════════════════


class TestCurrentConditionsParsing:
    """Test HTML parsing for the current conditions page."""

    def test_parse_temp_standard(self):
        html = """
        <body>
        Temperature: 39.9 &deg;F (4.4 &deg;C)<br>
        </body>
        """
        cc = NWSScraper._parse_current_conditions(html, "KMDW")
        assert cc.temp_f == pytest.approx(39.9)
        assert cc.temp_c == pytest.approx(4.4)

    def test_parse_temp_no_degree_entity(self):
        html = "Temperature: 39.9 F ( 4.4 C)"
        cc = NWSScraper._parse_current_conditions(html, "KMDW")
        assert cc.temp_f == pytest.approx(39.9)
        assert cc.temp_c == pytest.approx(4.4)

    def test_parse_no_temp(self):
        html = "<body>No temperature data available</body>"
        cc = NWSScraper._parse_current_conditions(html, "KMDW")
        assert cc.temp_f is None
        assert cc.temp_c is None

    def test_parse_6hr_max(self):
        html = """
        Temperature: 39.9 &deg;F (4.4 &deg;C)<br>
        6 Hour Max: 41.2 &deg;F
        """
        cc = NWSScraper._parse_current_conditions(html, "KMDW")
        assert cc.six_hr_max_f == pytest.approx(41.2)

    def test_negative_temp(self):
        html = "Temperature: -5.3 &deg;F (-20.7 &deg;C)"
        cc = NWSScraper._parse_current_conditions(html, "KMDW")
        assert cc.temp_f == pytest.approx(-5.3)
        assert cc.temp_c == pytest.approx(-20.7)


class TestCliParsing:
    """Test CLI product text parsing."""

    SAMPLE_CLI = """
    CLIMATE REPORT
    PRELIMINARY
    NATIONAL WEATHER SERVICE CHICAGO IL

    VALID AS OF 438 PM CST MON FEB 24 2026

                       TEMPERATURE (F)
    TODAY
    MAXIMUM TEMPERATURE
      TODAY               36
    MINIMUM TEMPERATURE
      TODAY               18
    """

    def test_max_temp(self):
        report = NWSScraper._parse_cli_product(self.SAMPLE_CLI, "MDW")
        assert report.max_temp_f == 36

    def test_min_temp(self):
        report = NWSScraper._parse_cli_product(self.SAMPLE_CLI, "MDW")
        assert report.min_temp_f == 18

    def test_preliminary_flag(self):
        report = NWSScraper._parse_cli_product(self.SAMPLE_CLI, "MDW")
        assert report.is_preliminary is True

    def test_valid_as_of(self):
        report = NWSScraper._parse_cli_product(self.SAMPLE_CLI, "MDW")
        assert report.valid_as_of is not None
        assert "438 PM" in report.valid_as_of or "FEB 24" in report.valid_as_of

    def test_non_preliminary(self):
        text = """
        CLIMATE REPORT
        NATIONAL WEATHER SERVICE CHICAGO IL
        MAXIMUM TEMPERATURE
          TODAY               40
        """
        report = NWSScraper._parse_cli_product(text, "MDW")
        assert report.is_preliminary is False
        assert report.max_temp_f == 40

    def test_empty_text(self):
        report = NWSScraper._parse_cli_product("", "MDW")
        assert report.max_temp_f is None
        assert report.min_temp_f is None


class TestScraperFetchGracefulDegradation:
    """Verify that fetch methods return None on failure."""

    def test_raw_metar_failure(self):
        scraper = NWSScraper.__new__(NWSScraper)
        scraper._client = MagicMock()
        scraper._rate_limiter = MagicMock()
        scraper._config = MagicMock()
        scraper._config.rate_limit = MagicMock()

        with patch.object(scraper, "_get", side_effect=Exception("network error")):
            result = scraper.get_raw_metar("KMDW")
        assert result is None

    def test_current_conditions_failure(self):
        scraper = NWSScraper.__new__(NWSScraper)
        scraper._client = MagicMock()
        scraper._rate_limiter = MagicMock()
        scraper._config = MagicMock()
        scraper._config.rate_limit = MagicMock()

        with patch.object(scraper, "_get", side_effect=Exception("timeout")):
            result = scraper.get_current_conditions("KMDW")
        assert result is None

    def test_cli_failure(self):
        scraper = NWSScraper.__new__(NWSScraper)
        scraper._client = MagicMock()
        scraper._rate_limiter = MagicMock()
        scraper._config = MagicMock()
        scraper._config.rate_limit = MagicMock()

        with patch.object(scraper, "_get", side_effect=Exception("500")):
            result = scraper.get_preliminary_cli("MDW")
        assert result is None

    def test_obs_history_failure(self):
        scraper = NWSScraper.__new__(NWSScraper)
        scraper._client = MagicMock()
        scraper._rate_limiter = MagicMock()
        scraper._config = MagicMock()
        scraper._config.rate_limit = MagicMock()

        with patch.object(scraper, "_get", side_effect=Exception("404")):
            result = scraper.get_observation_history("KMDW")
        assert result is None


# ═══════════════════════════════════════════════════════════════════════
# Edge Analysis Tests
# ═══════════════════════════════════════════════════════════════════════


class TestClassifyMargin:
    """Thresholds: COMFORTABLE ≥0.20, MODERATE 0.12–0.20, CLOSE 0.06–0.12, RAZOR_THIN <0.06."""

    def test_comfortable(self):
        assert classify_margin(0.25) == MarginStatus.COMFORTABLE

    def test_moderate(self):
        assert classify_margin(0.15) == MarginStatus.MODERATE

    def test_close(self):
        assert classify_margin(0.08) == MarginStatus.CLOSE

    def test_razor_thin(self):
        assert classify_margin(0.03) == MarginStatus.RAZOR_THIN

    def test_exact_boundary_comfortable(self):
        assert classify_margin(0.20) == MarginStatus.COMFORTABLE

    def test_exact_boundary_moderate(self):
        assert classify_margin(0.12) == MarginStatus.MODERATE

    def test_exact_boundary_close(self):
        assert classify_margin(0.06) == MarginStatus.CLOSE


class TestClassifyTimeRisk:
    def test_morning(self):
        assert classify_time_risk(10) == TimeRisk.STILL_RISING

    def test_near_peak(self):
        assert classify_time_risk(15) == TimeRisk.NEAR_PEAK
        assert classify_time_risk(16) == TimeRisk.NEAR_PEAK

    def test_past_peak(self):
        assert classify_time_risk(17) == TimeRisk.PAST_PEAK
        assert classify_time_risk(20) == TimeRisk.PAST_PEAK

    def test_settled(self):
        assert classify_time_risk(22) == TimeRisk.SETTLED
        assert classify_time_risk(23) == TimeRisk.SETTLED


class TestBracketAnalysis:
    def test_comfortable_margin(self):
        # 10.5°C = 50.9°F → CLI 51, boundary 50/51 at 10.278°C
        # margin = 10.5 - 10.278 = 0.222°C → COMFORTABLE (≥0.20)
        ba = compute_bracket_analysis(10.5)
        assert ba.cli_rounded_f == 51
        assert ba.margin_status == MarginStatus.COMFORTABLE

    def test_moderate_margin(self):
        # 4.87°C = 40.766°F → CLI 41, boundary 40/41 at 4.722°C
        # margin = 0.148°C → MODERATE (0.12–0.20)
        ba = compute_bracket_analysis(4.87)
        assert ba.cli_rounded_f == 41
        assert ba.margin_status == MarginStatus.MODERATE

    def test_razor_thin_margin(self):
        # 4.2°C = 39.56°F → rounds to 40, just above the 39/40 boundary (4.1667)
        ba = compute_bracket_analysis(4.2)
        assert ba.cli_rounded_f == 40
        assert ba.margin_below_c == pytest.approx(4.2 - get_f_boundary_c(39), abs=0.001)
        # 4.2 - 4.1667 = 0.0333 → RAZOR_THIN
        assert ba.margin_status == MarginStatus.RAZOR_THIN

    def test_boundary_above(self):
        # 4.2°C: boundary above (40/41F) is at 4.7222°C
        ba = compute_bracket_analysis(4.2)
        assert ba.margin_above_c > 0  # Still below upper boundary
        assert ba.margin_above_c == pytest.approx(
            get_f_boundary_c(40) - 4.2, abs=0.001
        )


class TestSignalGeneration:
    """Test the edge signal generation using the Feb 24 Chicago scenario."""

    def _make_report(
        self,
        running_max_c: float,
        metar_temp_f: int | None = None,
        cli_max_f: int | None = None,
        time_risk: TimeRisk = TimeRisk.PAST_PEAK,
    ) -> EdgeReport:
        """Build a minimal EdgeReport for signal testing."""
        report = EdgeReport(
            city="Chicago",
            station_icao="KMDW",
            cli_code="MDW",
            timezone="America/Chicago",
            analysis_time_utc=datetime(2026, 2, 24, 20, 35, tzinfo=ZoneInfo("UTC")),
        )
        report.running_max_c = running_max_c
        report.running_max_f_precise = c_to_f_precise(running_max_c)
        report.running_max_cli_f = c_to_f_cli_rounded(running_max_c)
        report.metar_temp_f = metar_temp_f
        report.bracket = compute_bracket_analysis(running_max_c)
        report.time_risk = time_risk
        report.cli_max_f = cli_max_f
        return report

    def test_feb24_chicago_scenario(self):
        """4.4°C = 39.92°F → CLI 40, margin 0.233°C (COMFORTABLE), METAR 39 → STRONG_BUY."""
        from kalshi_weather.edge import _generate_signal

        report = self._make_report(
            running_max_c=4.4,
            metar_temp_f=39,
            time_risk=TimeRisk.PAST_PEAK,
        )
        signal, reason = _generate_signal(report)
        assert signal == Signal.STRONG_BUY
        assert "39" in reason or "40" in reason

    def test_cli_confirms_strong_buy(self):
        """Preliminary CLI matches our prediction → STRONG_BUY."""
        from kalshi_weather.edge import _generate_signal

        report = self._make_report(
            running_max_c=4.4,
            metar_temp_f=39,
            cli_max_f=40,
        )
        signal, reason = _generate_signal(report)
        assert signal == Signal.STRONG_BUY
        assert "CLI confirms" in reason or "Preliminary CLI" in reason

    def test_no_edge_when_sources_agree_comfortable(self):
        """All sources agree with comfortable margin → NO_EDGE."""
        from kalshi_weather.edge import _generate_signal

        # 10.5°C = 50.9°F → CLI 51, margin 0.222°C (COMFORTABLE), METAR agrees
        report = self._make_report(
            running_max_c=10.5,
            metar_temp_f=51,
        )
        signal, _ = _generate_signal(report)
        assert signal == Signal.NO_EDGE

    def test_caution_when_sources_agree_close(self):
        """Sources agree but margin is CLOSE → CAUTION."""
        from kalshi_weather.edge import _generate_signal

        # 4.79°C = 40.622°F → CLI 41, boundary 40/41 at 4.722°C
        # margin = 0.068°C → CLOSE
        report = self._make_report(
            running_max_c=4.79,
            metar_temp_f=41,
        )
        signal, _ = _generate_signal(report)
        assert signal == Signal.CAUTION

    def test_caution_razor_thin(self):
        """METAR disagrees but margin is razor thin → CAUTION."""
        from kalshi_weather.edge import _generate_signal

        # 4.18°C = 39.524°F → CLI rounds to 40 but only barely
        report = self._make_report(
            running_max_c=4.18,
            metar_temp_f=39,
        )
        signal, _ = _generate_signal(report)
        assert signal == Signal.CAUTION

    def test_still_rising_weaker_signal(self):
        """Before peak with comfortable margin, signal should be BUY not STRONG_BUY."""
        from kalshi_weather.edge import _generate_signal

        # 4.4°C → CLI 40, margin 0.233 (COMFORTABLE), METAR 39, but STILL_RISING
        report = self._make_report(
            running_max_c=4.4,
            metar_temp_f=39,
            time_risk=TimeRisk.STILL_RISING,
        )
        signal, _ = _generate_signal(report)
        assert signal == Signal.BUY

    def test_insufficient_data(self):
        """No running max → NO_EDGE."""
        from kalshi_weather.edge import _generate_signal

        report = EdgeReport(
            city="Chicago",
            station_icao="KMDW",
            cli_code="MDW",
            timezone="America/Chicago",
            analysis_time_utc=datetime(2026, 2, 24, 20, 35, tzinfo=ZoneInfo("UTC")),
        )
        signal, reason = _generate_signal(report)
        assert signal == Signal.NO_EDGE
        assert "Insufficient" in reason


class TestFormatEdgeReport:
    """Verify output format has key elements."""

    def test_contains_city_name(self):
        report = EdgeReport(
            city="Chicago",
            station_icao="KMDW",
            cli_code="MDW",
            timezone="America/Chicago",
            analysis_time_utc=datetime(2026, 2, 24, 20, 35, tzinfo=ZoneInfo("UTC")),
            running_max_c=4.4,
            running_max_f_precise=39.92,
            running_max_cli_f=40,
            bracket=compute_bracket_analysis(4.4),
            signal=Signal.BUY,
            signal_reason="Test signal",
            hours_to_cli_close=3.4,
        )
        output = format_edge_report(report)
        assert "Chicago" in output
        assert "KMDW" in output
        assert "BUY" in output

    def test_format_summary_table(self):
        reports = [
            EdgeReport(
                city="Chicago",
                station_icao="KMDW",
                cli_code="MDW",
                timezone="America/Chicago",
                analysis_time_utc=datetime(2026, 2, 24, 20, 35, tzinfo=ZoneInfo("UTC")),
                running_max_c=4.4,
                running_max_f_precise=39.92,
                running_max_cli_f=40,
                metar_temp_f=39,
                bracket=compute_bracket_analysis(4.4),
                signal=Signal.BUY,
                time_risk=TimeRisk.PAST_PEAK,
            ),
            EdgeReport(
                city="Miami",
                station_icao="KMIA",
                cli_code="MIA",
                timezone="America/New_York",
                analysis_time_utc=datetime(2026, 2, 24, 20, 35, tzinfo=ZoneInfo("UTC")),
                running_max_c=25.9,
                running_max_f_precise=78.62,
                running_max_cli_f=79,
                metar_temp_f=79,
                bracket=compute_bracket_analysis(25.9),
                signal=Signal.NO_EDGE,
                time_risk=TimeRisk.PAST_PEAK,
            ),
        ]
        output = format_edge_summary(reports)
        assert "Chicago" in output
        assert "Miami" in output
        assert "SUMMARY" in output
        assert "BUY" in output
