"""Tests for the Spike Monitor system."""

from __future__ import annotations

import time
from datetime import datetime
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

# ======================================================================
# SpikeConfig Tests
# ======================================================================


class TestSpikeConfig:
    def test_defaults(self):
        from kalshi_weather.spike_config import SpikeConfig

        cfg = SpikeConfig()
        assert cfg.spike_threshold_cents == 20
        assert cfg.window_seconds == 360
        assert cfg.poll_interval_seconds == 30
        assert cfg.burst_count == 5
        assert cfg.burst_interval_seconds == 60
        assert cfg.start_hour_est == 8
        assert cfg.end_hour_est == 23
        assert cfg.cooldown_seconds == 600

    def test_custom_values(self):
        from kalshi_weather.spike_config import SpikeConfig

        cfg = SpikeConfig(spike_threshold_cents=25, window_seconds=300)
        assert cfg.spike_threshold_cents == 25
        assert cfg.window_seconds == 300


# ======================================================================
# Spike Detection Tests
# ======================================================================


class TestPriceHistory:
    """Test the rolling price snapshot storage."""

    def test_add_snapshot(self):
        from kalshi_weather.spike_monitor import PriceHistory

        ph = PriceHistory(max_age_seconds=360)
        now = time.monotonic()
        ph.record("TICKER-A", 10, now)
        assert len(ph.get_history("TICKER-A")) == 1

    def test_old_entries_pruned(self):
        from kalshi_weather.spike_monitor import PriceHistory

        ph = PriceHistory(max_age_seconds=360)
        old_time = time.monotonic() - 400  # 400s ago, outside 360s window
        ph.record("TICKER-A", 10, old_time)
        ph.record("TICKER-A", 15, time.monotonic())
        ph.prune("TICKER-A")
        assert len(ph.get_history("TICKER-A")) == 1

    def test_unknown_ticker_empty(self):
        from kalshi_weather.spike_monitor import PriceHistory

        ph = PriceHistory(max_age_seconds=360)
        assert ph.get_history("UNKNOWN") == []


class TestSpikeDetection:
    """Test the spike detection algorithm."""

    def test_spike_detected(self):
        from kalshi_weather.spike_config import SpikeConfig
        from kalshi_weather.spike_monitor import PriceHistory, detect_spike

        cfg = SpikeConfig(spike_threshold_cents=20, window_seconds=360)
        ph = PriceHistory(max_age_seconds=360)
        now = time.monotonic()

        ph.record("BRACKET-A", 7, now - 180)
        ph.record("BRACKET-A", 32, now)

        result = detect_spike(ph, cfg, now)
        assert result is not None
        assert result.ticker == "BRACKET-A"
        assert result.old_price == 7
        assert result.new_price == 32
        assert result.delta == 25

    def test_no_spike_below_threshold(self):
        from kalshi_weather.spike_config import SpikeConfig
        from kalshi_weather.spike_monitor import PriceHistory, detect_spike

        cfg = SpikeConfig(spike_threshold_cents=20)
        ph = PriceHistory(max_age_seconds=360)
        now = time.monotonic()

        ph.record("BRACKET-A", 10, now - 180)
        ph.record("BRACKET-A", 25, now)

        result = detect_spike(ph, cfg, now)
        assert result is None

    def test_no_spike_outside_window(self):
        from kalshi_weather.spike_config import SpikeConfig
        from kalshi_weather.spike_monitor import PriceHistory, detect_spike

        cfg = SpikeConfig(spike_threshold_cents=20, window_seconds=360)
        ph = PriceHistory(max_age_seconds=600)
        now = time.monotonic()

        ph.record("BRACKET-A", 7, now - 420)
        ph.record("BRACKET-A", 32, now)

        result = detect_spike(ph, cfg, now)
        assert result is None

    def test_largest_spike_wins(self):
        from kalshi_weather.spike_config import SpikeConfig
        from kalshi_weather.spike_monitor import PriceHistory, detect_spike

        cfg = SpikeConfig(spike_threshold_cents=20)
        ph = PriceHistory(max_age_seconds=360)
        now = time.monotonic()

        ph.record("BRACKET-A", 10, now - 180)
        ph.record("BRACKET-A", 35, now)

        ph.record("BRACKET-B", 5, now - 180)
        ph.record("BRACKET-B", 50, now)

        result = detect_spike(ph, cfg, now)
        assert result is not None
        assert result.ticker == "BRACKET-B"
        assert result.delta == 45

    def test_cooldown_prevents_retrigger(self):
        from kalshi_weather.spike_config import SpikeConfig
        from kalshi_weather.spike_monitor import PriceHistory, detect_spike

        cfg = SpikeConfig(spike_threshold_cents=20, cooldown_seconds=600)
        ph = PriceHistory(max_age_seconds=360)
        now = time.monotonic()

        ph.record("BRACKET-A", 7, now - 180)
        ph.record("BRACKET-A", 32, now)

        result = detect_spike(ph, cfg, now)
        assert result is not None

        cooldowns = {"BRACKET-A": now}
        result = detect_spike(ph, cfg, now, cooldowns=cooldowns)
        assert result is None

        # Add fresh data so a spike exists in the window at now+601
        ph.record("BRACKET-A", 7, now + 500)
        ph.record("BRACKET-A", 32, now + 601)
        result = detect_spike(ph, cfg, now + 601, cooldowns=cooldowns)
        assert result is not None


# ======================================================================
# HTML Email Builder Tests
# ======================================================================


class TestSignalColor:
    def test_strong_buy_green(self):
        from kalshi_weather.spike_alerter import signal_to_color

        color, label = signal_to_color("STRONG_BUY")
        assert color == "#22c55e"
        assert label == "STRONG_BUY"

    def test_buy_green(self):
        from kalshi_weather.spike_alerter import signal_to_color

        color, _ = signal_to_color("BUY")
        assert color == "#22c55e"

    def test_hold_yellow(self):
        from kalshi_weather.spike_alerter import signal_to_color

        color, _ = signal_to_color("HOLD")
        assert color == "#eab308"

    def test_caution_red(self):
        from kalshi_weather.spike_alerter import signal_to_color

        color, _ = signal_to_color("CAUTION")
        assert color == "#ef4444"

    def test_no_edge_red(self):
        from kalshi_weather.spike_alerter import signal_to_color

        color, _ = signal_to_color("NO_EDGE")
        assert color == "#ef4444"


class TestBuildConvictionRow:
    def test_completed_row(self):
        from kalshi_weather.spike_alerter import build_conviction_row

        row = build_conviction_row(
            index=1,
            total=5,
            time_str="3:39 PM",
            signal="STRONG_BUY",
            temp_f=39.9,
            market_price=32,
            is_current=False,
        )
        assert "3:39 PM" in row
        assert "STRONG_BUY" in row
        assert "39.9" in row
        assert "32" in row

    def test_pending_row(self):
        from kalshi_weather.spike_alerter import build_conviction_row

        row = build_conviction_row(
            index=3,
            total=5,
            time_str="3:41 PM",
            signal=None,
            temp_f=None,
            market_price=None,
            is_current=False,
        )
        assert "(pending)" in row

    def test_current_row_marker(self):
        from kalshi_weather.spike_alerter import build_conviction_row

        row = build_conviction_row(
            index=2,
            total=5,
            time_str="3:40 PM",
            signal="BUY",
            temp_f=39.9,
            market_price=29,
            is_current=True,
        )
        assert "\u2190" in row or "here" in row.lower()


class TestBuildSpikeEmailHtml:
    def test_contains_key_elements(self):
        from kalshi_weather.spike_alerter import build_spike_email_html

        html = build_spike_email_html(
            city="Chicago",
            bracket="40-41\u00b0F",
            email_number=2,
            email_total=5,
            time_str="3:42 PM EST",
            old_price=7,
            new_price=32,
            current_price=29,
            spike_delta=25,
            metar_f=39,
            precise_f=39.9,
            precise_c=4.4,
            precise_source="NWS Current Conditions",
            running_max_f=40,
            margin_c=0.23,
            margin_status="COMFORTABLE",
            signal="STRONG_BUY",
            signal_reason="Precise data shows 40F with COMFORTABLE margin.",
            time_risk="PAST_PEAK",
            conviction_rows=["row1", "row2"],
        )
        assert "Chicago" in html
        assert "40-41" in html
        assert "#22c55e" in html  # green for STRONG_BUY
        assert "STRONG_BUY" in html
        assert "2 of 5" in html or "2/5" in html
        assert "39.9" in html

    def test_caution_uses_red(self):
        from kalshi_weather.spike_alerter import build_spike_email_html

        html = build_spike_email_html(
            city="Miami",
            bracket="78-79\u00b0F",
            email_number=1,
            email_total=5,
            time_str="4:00 PM EST",
            old_price=10,
            new_price=35,
            current_price=35,
            spike_delta=25,
            metar_f=78,
            precise_f=78.1,
            precise_c=25.6,
            precise_source="METAR T-group",
            running_max_f=78,
            margin_c=0.05,
            margin_status="RAZOR_THIN",
            signal="CAUTION",
            signal_reason="Razor thin margin.",
            time_risk="STILL_RISING",
            conviction_rows=[],
        )
        assert "#ef4444" in html  # red for CAUTION


class TestSendSpikeEmail:
    def test_send_constructs_html_message(self):
        """Verify email is constructed with HTML content type."""
        from unittest.mock import MagicMock, patch

        from kalshi_weather.spike_alerter import send_spike_email

        with patch("kalshi_weather.spike_alerter.smtplib.SMTP") as mock_smtp:
            mock_server = MagicMock()
            mock_smtp.return_value.__enter__ = MagicMock(return_value=mock_server)
            mock_smtp.return_value.__exit__ = MagicMock(return_value=False)

            send_spike_email(
                subject="Test",
                html_body="<html><body>Test</body></html>",
                gmail_address="test@gmail.com",
                gmail_app_password="password",
            )

            mock_server.send_message.assert_called_once()
            msg = mock_server.send_message.call_args[0][0]
            assert msg["To"] == "test@gmail.com"
            assert msg["From"] == "test@gmail.com"


# ======================================================================
# Market Polling Tests
# ======================================================================


class TestExtractBracketPrices:
    """Test extracting yes_price from nested market objects."""

    def test_extract_prices_from_event(self):
        from kalshi_weather.spike_monitor import (
            extract_bracket_prices,
        )

        event = {
            "event_ticker": "KXHIGHCHI-26FEB25",
            "title": (
                "Highest temperature in Chicago "
                "on February 26"
            ),
            "markets": [
                {
                    "ticker": "KXHIGHCHI-26FEB25-B40",
                    "yes_bid": 7,
                    "yes_ask": 12,
                },
                {
                    "ticker": "KXHIGHCHI-26FEB25-B45",
                    "yes_bid": 50,
                    "yes_ask": 55,
                },
            ],
        }
        prices = extract_bracket_prices(event)
        assert len(prices) == 2
        assert prices["KXHIGHCHI-26FEB25-B40"] == 7
        assert prices["KXHIGHCHI-26FEB25-B45"] == 50

    def test_missing_yes_bid_skipped(self):
        from kalshi_weather.spike_monitor import (
            extract_bracket_prices,
        )

        event = {
            "event_ticker": "KXHIGHCHI-26FEB25",
            "markets": [
                {"ticker": "KXHIGHCHI-26FEB25-B40"},
            ],
        }
        prices = extract_bracket_prices(event)
        assert len(prices) == 0


class TestIsInOperatingWindow:
    def test_inside_window(self):
        from kalshi_weather.spike_config import SpikeConfig
        from kalshi_weather.spike_monitor import (
            is_in_operating_window,
        )

        cfg = SpikeConfig(
            start_hour_est=8, end_hour_est=23,
        )
        dt = datetime(
            2026, 2, 25, 15, 0,
            tzinfo=ZoneInfo("US/Eastern"),
        )
        assert is_in_operating_window(dt, cfg) is True

    def test_before_window(self):
        from kalshi_weather.spike_config import SpikeConfig
        from kalshi_weather.spike_monitor import (
            is_in_operating_window,
        )

        cfg = SpikeConfig(
            start_hour_est=8, end_hour_est=23,
        )
        dt = datetime(
            2026, 2, 25, 6, 0,
            tzinfo=ZoneInfo("US/Eastern"),
        )
        assert is_in_operating_window(dt, cfg) is False

    def test_after_window(self):
        from kalshi_weather.spike_config import SpikeConfig
        from kalshi_weather.spike_monitor import (
            is_in_operating_window,
        )

        cfg = SpikeConfig(
            start_hour_est=8, end_hour_est=23,
        )
        dt = datetime(
            2026, 2, 25, 23, 30,
            tzinfo=ZoneInfo("US/Eastern"),
        )
        assert is_in_operating_window(dt, cfg) is True

    def test_midnight_outside(self):
        from kalshi_weather.spike_config import SpikeConfig
        from kalshi_weather.spike_monitor import (
            is_in_operating_window,
        )

        cfg = SpikeConfig(
            start_hour_est=8, end_hour_est=23,
        )
        dt = datetime(
            2026, 2, 26, 0, 5,
            tzinfo=ZoneInfo("US/Eastern"),
        )
        assert is_in_operating_window(dt, cfg) is False


class TestBurstCollectData:
    """Test burst data collection calls edge analysis."""

    def test_collect_burst_data(self):
        from kalshi_weather.spike_monitor import (
            collect_burst_data,
        )

        mock_scraper = MagicMock()
        mock_client = MagicMock()

        mock_report = MagicMock()
        mock_report.running_max_f_precise = 39.9
        mock_report.running_max_c = 4.4
        mock_report.running_max_cli_f = 40
        mock_report.running_max_source = (
            "Current Conditions"
        )
        mock_report.metar_temp_f = 39
        mock_report.bracket = MagicMock()
        mock_report.bracket.margin_below_c = 0.23
        mock_report.bracket.margin_status.value = (
            "COMFORTABLE"
        )
        mock_report.signal.value = "STRONG_BUY"
        mock_report.signal_reason = "Test reason"
        mock_report.time_risk.value = "PAST_PEAK"

        with patch(
            "kalshi_weather.spike_monitor.analyze_city",
            return_value=mock_report,
        ):
            data = collect_burst_data(
                city="Chicago",
                ticker="KXHIGHCHI-26FEB25-B40",
                client=mock_client,
                scraper=mock_scraper,
            )

        assert data is not None
        assert data["signal"] == "STRONG_BUY"
        assert data["precise_f"] == 39.9


# ======================================================================
# CLI Integration Tests
# ======================================================================


class TestSpikeSubcommand:
    def test_spike_args_parsed(self):
        """Verify argparse recognizes the spike subcommand."""
        from unittest.mock import patch
        from kalshi_weather.__main__ import main

        # Should fail on missing credentials, not on arg parsing
        with patch.dict("os.environ", {}, clear=True):
            result = main(
                ["spike", "--threshold", "25", "--interval", "20"],
            )
        assert result == 1  # fails on missing creds

    def test_spike_default_runs_scan(self):
        """No args still defaults to scan."""
        from unittest.mock import patch
        from kalshi_weather.__main__ import main

        with patch.dict("os.environ", {}, clear=True):
            result = main([])
        assert result == 1  # missing creds for scan
