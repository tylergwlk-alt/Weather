"""Phase 12 — Tests for runner, emailer, and __main__ entry point."""

from __future__ import annotations

from email.mime.multipart import MIMEMultipart
from unittest.mock import MagicMock, patch

import pytest

from kalshi_weather.emailer import build_email, send_report_email
from kalshi_weather.runner import enrich_candidate, run_full_scan
from kalshi_weather.schemas import CandidateRaw, MarketType, OrderbookSnapshot
from kalshi_weather.weather_api import CurrentObs, StationForecast

# ── Fixtures ──────────────────────────────────────────────────────────


def _make_raw(
    ticker="KXHIGHCHI-26FEB14-T50",
    city="Chicago",
) -> CandidateRaw:
    return CandidateRaw(
        run_time_et="2026-02-14 07:00 ET",
        target_date_local="2026-02-14",
        city=city,
        market_type=MarketType.HIGH_TEMP,
        event_name="KXHIGHCHI",
        market_ticker=ticker,
        market_url=f"https://kalshi.com/markets/{ticker}",
        bracket_definition="50°F or above",
        orderbook_snapshot=OrderbookSnapshot(
            best_yes_bid_cents=9,
            best_no_bid_cents=88,
            implied_best_no_ask_cents=91,
            implied_best_yes_ask_cents=12,
            bid_room_cents=3,
            top3_yes_bids=[[9, 50], [8, 30]],
            top3_no_bids=[[88, 40], [87, 25]],
        ),
    )


def _mock_weather() -> MagicMock:
    weather = MagicMock()
    weather.get_current_obs.return_value = CurrentObs(
        station_icao="KMDW",
        timestamp="2026-02-14T07:00:00Z",
        temp_c=2.0,
        temp_f=35.6,
    )
    weather.get_hourly_forecast.return_value = StationForecast(
        station_icao="KMDW",
        forecast_high_f=42.0,
        forecast_low_f=28.0,
    )
    return weather


# ── Runner tests ──────────────────────────────────────────────────────


class TestEnrichCandidate:
    """Test that enrich_candidate wires all modules correctly."""

    def test_enrich_produces_unified(self):
        raw = _make_raw()
        weather = _mock_weather()
        unified = enrich_candidate(raw, weather)

        assert unified.market_ticker == raw.market_ticker
        assert unified.city == "Chicago"
        assert unified.settlement_spec is not None
        assert unified.model is not None
        assert unified.fees_ev is not None
        assert unified.manual_trade_plan is not None
        assert unified.allocation is not None

    def test_enrich_calls_weather_api(self):
        raw = _make_raw()
        weather = _mock_weather()
        enrich_candidate(raw, weather)

        weather.get_current_obs.assert_called_once_with("KMDW")
        weather.get_hourly_forecast.assert_called_once_with("KMDW")

    def test_enrich_handles_unknown_city(self):
        raw = _make_raw(city="UnknownCity")
        weather = _mock_weather()
        unified = enrich_candidate(raw, weather)

        # Should still produce a unified candidate, just without weather data
        assert unified.market_ticker == raw.market_ticker
        weather.get_current_obs.assert_not_called()
        weather.get_hourly_forecast.assert_not_called()

    def test_enrich_handles_missing_obs(self):
        raw = _make_raw()
        weather = _mock_weather()
        weather.get_current_obs.return_value = None

        unified = enrich_candidate(raw, weather)
        assert unified.model is not None


class TestRunFullScan:
    """Test the full scan pipeline with mocked API clients."""

    def test_full_scan_empty_markets(self, tmp_path):
        client = MagicMock(spec=["close"])
        weather = _mock_weather()

        with patch("kalshi_weather.runner.scan_today_markets", return_value=[]):
            slate, out_dir = run_full_scan(client, weather, output_dir=tmp_path)

        assert len(slate.picks_primary) == 0
        assert len(slate.picks_tight) == 0

    def test_full_scan_with_candidates(self, tmp_path):
        client = MagicMock(spec=["close"])
        weather = _mock_weather()
        raw_candidates = [_make_raw()]

        with patch("kalshi_weather.runner.scan_today_markets", return_value=raw_candidates):
            slate, out_dir = run_full_scan(client, weather, output_dir=tmp_path)

        # Should have processed the candidate into some bucket
        total = (
            len(slate.picks_primary)
            + len(slate.picks_tight)
            + len(slate.picks_near_miss)
            + len(slate.rejected)
        )
        assert total == 1

    def test_full_scan_skips_failed_enrichment(self, tmp_path):
        client = MagicMock(spec=["close"])
        weather = _mock_weather()
        raw_candidates = [_make_raw()]

        with (
            patch("kalshi_weather.runner.scan_today_markets", return_value=raw_candidates),
            patch("kalshi_weather.runner.enrich_candidate", side_effect=RuntimeError("boom")),
        ):
            slate, _ = run_full_scan(client, weather, output_dir=tmp_path)

        # Candidate was skipped, so slate should be empty
        total = (
            len(slate.picks_primary)
            + len(slate.picks_tight)
            + len(slate.picks_near_miss)
            + len(slate.rejected)
        )
        assert total == 0


# ── Emailer tests ─────────────────────────────────────────────────────


class TestBuildEmail:
    """Test MIME message construction."""

    def test_build_email_basic(self, tmp_path):
        report = tmp_path / "REPORT.md"
        report.write_text("# Daily Report\nSome picks here.", encoding="utf-8")

        msg = build_email(
            from_addr="sender@gmail.com",
            to_addr="recipient@example.com",
            subject="Test Report",
            report_md_path=report,
        )

        assert isinstance(msg, MIMEMultipart)
        assert msg["From"] == "sender@gmail.com"
        assert msg["To"] == "recipient@example.com"
        assert msg["Subject"] == "Test Report"

        # Body should be the report text
        parts = msg.get_payload()
        assert len(parts) == 1  # Just the text body, no attachment
        assert "Daily Report" in parts[0].get_payload(decode=True).decode("utf-8")

    def test_build_email_with_attachment(self, tmp_path):
        report = tmp_path / "REPORT.md"
        report.write_text("# Report", encoding="utf-8")
        slate = tmp_path / "DAILY_SLATE.json"
        slate.write_text('{"picks": []}', encoding="utf-8")

        msg = build_email(
            from_addr="sender@gmail.com",
            to_addr="recipient@example.com",
            subject="Test",
            report_md_path=report,
            slate_json_path=slate,
        )

        parts = msg.get_payload()
        assert len(parts) == 2  # Text body + JSON attachment
        assert parts[1].get_filename() == "DAILY_SLATE.json"


class TestSendReportEmail:
    """Test email sending with mocked SMTP."""

    def test_send_email_missing_credentials(self, tmp_path):
        report = tmp_path / "REPORT.md"
        report.write_text("test", encoding="utf-8")

        with pytest.raises(ValueError, match="GMAIL_ADDRESS not set"):
            send_report_email(
                to_addr="test@example.com",
                subject="Test",
                report_md_path=report,
                gmail_address="",
                gmail_app_password="",
            )

    def test_send_email_missing_password(self, tmp_path):
        report = tmp_path / "REPORT.md"
        report.write_text("test", encoding="utf-8")

        with pytest.raises(ValueError, match="GMAIL_APP_PASSWORD not set"):
            send_report_email(
                to_addr="test@example.com",
                subject="Test",
                report_md_path=report,
                gmail_address="sender@gmail.com",
                gmail_app_password="",
            )

    @patch("kalshi_weather.emailer.smtplib.SMTP")
    def test_send_email_success(self, mock_smtp_class, tmp_path):
        report = tmp_path / "REPORT.md"
        report.write_text("# Report content", encoding="utf-8")

        mock_server = MagicMock()
        mock_smtp_class.return_value.__enter__ = MagicMock(return_value=mock_server)
        mock_smtp_class.return_value.__exit__ = MagicMock(return_value=False)

        send_report_email(
            to_addr="recipient@example.com",
            subject="Daily Report",
            report_md_path=report,
            gmail_address="sender@gmail.com",
            gmail_app_password="app-password-123",
        )

        mock_smtp_class.assert_called_once_with("smtp.gmail.com", 587)
        mock_server.starttls.assert_called_once()
        mock_server.login.assert_called_once_with("sender@gmail.com", "app-password-123")
        mock_server.send_message.assert_called_once()


# ── __main__ importability ────────────────────────────────────────────


class TestMainModule:
    """Test that __main__ is importable and has expected interface."""

    def test_main_importable(self):
        from kalshi_weather.__main__ import main
        assert callable(main)

    def test_main_missing_credentials(self):
        """main() returns 1 when credentials are missing."""
        from kalshi_weather.__main__ import main

        with patch.dict("os.environ", {}, clear=True):
            result = main()
        assert result == 1
