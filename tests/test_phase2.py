"""Phase 2 tests — Kalshi client safety, orderbook parsing, scanner logic."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock

from kalshi_weather.kalshi_client import _ALLOWED_PATH_PREFIXES, KalshiClient
from kalshi_weather.scanner import (
    _bracket_definition,
    _classify_series,
    _extract_city_from_event,
    _is_today_event,
    _market_is_tradable,
    _parse_orderbook,
    scan_today_markets,
)
from kalshi_weather.schemas import MarketType

# ── Client safety tests ───────────────────────────────────────────────

class TestClientSafety:
    """Verify the client is truly read-only."""

    def test_no_post_method_exists(self):
        """Client must not have any POST/PUT/DELETE/PATCH methods."""
        for attr in dir(KalshiClient):
            lower = attr.lower()
            assert "post" not in lower, f"Found POST-like method: {attr}"
            assert "order" not in lower or "orderbook" in lower, (
                f"Found order-like method: {attr}"
            )
            assert "submit" not in lower, f"Found submit-like method: {attr}"
            assert "cancel" not in lower, f"Found cancel-like method: {attr}"
            assert "delete" not in lower, f"Found delete-like method: {attr}"

    def test_allowed_paths_are_read_only(self):
        """All allowed path prefixes must be read-only endpoints."""
        for prefix in _ALLOWED_PATH_PREFIXES:
            assert "order" not in prefix.lower() or "orderbook" in prefix.lower()
            assert "portfolio" not in prefix.lower()


# ── Series classification ──────────────────────────────────────────────

class TestClassifySeries:
    def test_high_series(self):
        assert _classify_series("KXHIGHCHI") == MarketType.HIGH_TEMP
        assert _classify_series("KXHIGHLAX") == MarketType.HIGH_TEMP
        assert _classify_series("KXHIGHNY") == MarketType.HIGH_TEMP

    def test_low_series(self):
        assert _classify_series("KXLOWCHI") == MarketType.LOW_TEMP
        assert _classify_series("KXLOWNY") == MarketType.LOW_TEMP

    def test_non_temp_series(self):
        assert _classify_series("KXRAIN") is None
        assert _classify_series("ELECTION") is None


# ── City extraction ────────────────────────────────────────────────────

class TestCityExtraction:
    def test_from_title_high(self):
        event = {"title": "Highest temperature in Chicago on Feb 12", "event_ticker": "T"}
        assert _extract_city_from_event(event) == "Chicago"

    def test_from_title_low(self):
        event = {"title": "Lowest temperature in New York today", "event_ticker": "T"}
        assert _extract_city_from_event(event) == "New York"

    def test_fallback_to_ticker(self):
        event = {"title": "Some other format", "event_ticker": "KXHIGHCHI-26FEB12"}
        assert _extract_city_from_event(event) == "KXHIGHCHI-26FEB12"


# ── Today-event detection ──────────────────────────────────────────────

class TestIsTodayEvent:
    def test_strike_date_today(self):
        event = {"strike_date": "2026-02-12T00:00:00Z"}
        assert _is_today_event(event, "2026-02-12") is True

    def test_strike_date_yesterday(self):
        event = {"strike_date": "2026-02-11T00:00:00Z"}
        assert _is_today_event(event, "2026-02-12") is False

    def test_no_strike_uses_market_close(self):
        event = {
            "strike_date": None,
            "markets": [{"close_time": "2026-02-12T23:00:00Z"}],
        }
        assert _is_today_event(event, "2026-02-12") is True


# ── Orderbook parsing ─────────────────────────────────────────────────

class TestParseOrderbook:
    def test_normal_orderbook(self):
        raw = {
            "orderbook": {
                "yes": [[5, 10], [7, 20], [8, 15]],
                "no": [[88, 5], [89, 10], [90, 8]],
            }
        }
        ob = _parse_orderbook(raw)
        assert ob.best_yes_bid_cents == 8
        assert ob.best_no_bid_cents == 90
        assert ob.implied_best_no_ask_cents == 92  # 100 - 8
        assert ob.implied_best_yes_ask_cents == 10  # 100 - 90
        assert ob.bid_room_cents == 2  # 92 - 90

    def test_top3_ordering(self):
        raw = {
            "orderbook": {
                "yes": [[3, 5], [5, 10], [7, 20], [8, 15]],
                "no": [[85, 3], [88, 5], [89, 10], [90, 8]],
            }
        }
        ob = _parse_orderbook(raw)
        # top3 should be highest-first
        assert ob.top3_yes_bids == [[8, 15], [7, 20], [5, 10]]
        assert ob.top3_no_bids == [[90, 8], [89, 10], [88, 5]]

    def test_empty_yes_bids(self):
        raw = {"orderbook": {"yes": [], "no": [[90, 8]]}}
        ob = _parse_orderbook(raw)
        assert ob.best_yes_bid_cents is None
        assert ob.implied_best_no_ask_cents is None
        assert ob.best_no_bid_cents == 90
        assert ob.bid_room_cents is None
        assert "NO YES BIDS" in ob.depth_notes

    def test_empty_book(self):
        raw = {"orderbook": {"yes": [], "no": []}}
        ob = _parse_orderbook(raw)
        assert ob.best_yes_bid_cents is None
        assert ob.best_no_bid_cents is None
        assert ob.implied_best_no_ask_cents is None
        assert ob.bid_room_cents is None

    def test_primary_window_candidate(self):
        """Orderbook that produces implied_no_ask in [90,93]."""
        raw = {
            "orderbook": {
                "yes": [[7, 50]],  # implied_no_ask = 93
                "no": [[90, 30]],
            }
        }
        ob = _parse_orderbook(raw)
        assert ob.implied_best_no_ask_cents == 93
        assert ob.bid_room_cents == 3


# ── Market tradability ──────────────────────────────────────────────────

class TestMarketTradable:
    def test_active_is_tradable(self):
        assert _market_is_tradable({"status": "active"}) is True

    def test_open_is_tradable(self):
        assert _market_is_tradable({"status": "open"}) is True

    def test_closed_not_tradable(self):
        assert _market_is_tradable({"status": "closed"}) is False

    def test_determined_not_tradable(self):
        assert _market_is_tradable({"status": "determined"}) is False

    def test_missing_status(self):
        assert _market_is_tradable({}) is False


# ── Bracket definition ──────────────────────────────────────────────────

class TestBracketDefinition:
    def test_yes_sub_title(self):
        mkt = {"yes_sub_title": "50°F or above", "title": "Chicago High"}
        assert _bracket_definition(mkt) == "50°F or above"

    def test_fallback_to_title(self):
        mkt = {"yes_sub_title": "", "title": "Chicago High >= 50"}
        assert _bracket_definition(mkt) == "Chicago High >= 50"


# ── Full scanner integration (mocked API) ──────────────────────────────

class TestScanTodayMarkets:
    """Integration test with mocked KalshiClient."""

    def _make_mock_client(self):
        client = MagicMock(spec=KalshiClient)

        # Series discovery
        client.get_series_list.return_value = [
            {"ticker": "KXHIGHCHI"},
            {"ticker": "KXLOWCHI"},
            {"ticker": "KXRAIN"},  # should be ignored
        ]

        # Events for KXHIGHCHI
        high_event = {
            "event_ticker": "KXHIGHCHI-26FEB12",
            "title": "Highest temperature in Chicago on Feb 12",
            "strike_date": "2026-02-12T00:00:00Z",
            "markets": [
                {
                    "ticker": "KXHIGHCHI-26FEB12-T40",
                    "status": "active",
                    "yes_sub_title": "40°F or above",
                },
                {
                    "ticker": "KXHIGHCHI-26FEB12-T50",
                    "status": "active",
                    "yes_sub_title": "50°F or above",
                },
                {
                    "ticker": "KXHIGHCHI-26FEB12-T60",
                    "status": "closed",  # should be skipped
                    "yes_sub_title": "60°F or above",
                },
            ],
        }

        # Events for KXLOWCHI
        low_event = {
            "event_ticker": "KXLOWCHI-26FEB12",
            "title": "Lowest temperature in Chicago on Feb 12",
            "strike_date": "2026-02-12T00:00:00Z",
            "markets": [
                {
                    "ticker": "KXLOWCHI-26FEB12-T20",
                    "status": "active",
                    "yes_sub_title": "20°F or below",
                },
            ],
        }

        def get_all_events(series_ticker=None, **kw):
            if series_ticker == "KXHIGHCHI":
                return [high_event]
            if series_ticker == "KXLOWCHI":
                return [low_event]
            return []

        client.get_all_events.side_effect = get_all_events

        # Orderbooks
        def get_orderbook(ticker, depth=10):
            books = {
                # implied_no_ask = 92 → in [88,95]
                "KXHIGHCHI-26FEB12-T40": {
                    "orderbook": {
                        "yes": [[8, 50]],
                        "no": [[89, 30]],
                    }
                },
                # implied_no_ask = 80 → outside [88,95], should be filtered
                "KXHIGHCHI-26FEB12-T50": {
                    "orderbook": {
                        "yes": [[20, 50]],
                        "no": [[75, 10]],
                    }
                },
                # implied_no_ask = 95 → in [88,95]
                "KXLOWCHI-26FEB12-T20": {
                    "orderbook": {
                        "yes": [[5, 10]],
                        "no": [[93, 5]],
                    }
                },
            }
            return books.get(ticker, {"orderbook": {"yes": [], "no": []}})

        client.get_orderbook.side_effect = get_orderbook
        return client

    def test_scan_finds_candidates(self):
        client = self._make_mock_client()
        run_time = datetime(2026, 2, 12, 12, 0, 0, tzinfo=timezone.utc)

        candidates = scan_today_markets(client, run_time=run_time)

        tickers = [c.market_ticker for c in candidates]
        # T40 (implied=92) and T20 (implied=95) should pass
        assert "KXHIGHCHI-26FEB12-T40" in tickers
        assert "KXLOWCHI-26FEB12-T20" in tickers
        # T50 (implied=80) should be filtered out
        assert "KXHIGHCHI-26FEB12-T50" not in tickers
        # T60 is closed, should not appear
        assert "KXHIGHCHI-26FEB12-T60" not in tickers

    def test_scan_candidate_fields(self):
        client = self._make_mock_client()
        run_time = datetime(2026, 2, 12, 12, 0, 0, tzinfo=timezone.utc)

        candidates = scan_today_markets(client, run_time=run_time)
        chi_high = next(c for c in candidates if c.market_ticker == "KXHIGHCHI-26FEB12-T40")

        assert chi_high.city == "Chicago"
        assert chi_high.market_type == MarketType.HIGH_TEMP
        assert chi_high.target_date_local == "2026-02-12"
        assert chi_high.bracket_definition == "40°F or above"
        assert chi_high.orderbook_snapshot.implied_best_no_ask_cents == 92
        assert chi_high.orderbook_snapshot.bid_room_cents == 3  # 92 - 89
        assert "kalshi.com" in chi_high.market_url

    def test_scan_skips_non_today(self):
        client = self._make_mock_client()
        # Run on a different day — no events should match
        run_time = datetime(2026, 2, 13, 12, 0, 0, tzinfo=timezone.utc)

        candidates = scan_today_markets(client, run_time=run_time)
        assert len(candidates) == 0
