"""Phase 11 — Task #47: Edge case coverage.

DST transitions, missing orderbooks, station outages, empty brackets,
inverted markets, None data propagation, and correlation cap edge cases.
"""

from __future__ import annotations

from datetime import datetime

from kalshi_weather.modeler import model_candidate
from kalshi_weather.planner import (
    LiquidityVerdict,
    assess_liquidity,
    assess_spread,
    compute_recommended_limit,
)
from kalshi_weather.risk import (
    enforce_correlation_caps,
    get_correlation_group,
    get_metro_cluster,
)
from kalshi_weather.rules import build_settlement_spec, get_cli_day_window
from kalshi_weather.schemas import (
    Bucket,
    CandidateRaw,
    MarketType,
    OrderbookSnapshot,
)
from kalshi_weather.team_lead import apply_hard_rejects, classify_bucket, merge_candidate
from kalshi_weather.weather_api import StationForecast

# ── DST transition edge cases ───────────────────────────────────────


class TestDSTTransitions:
    def test_march_dst_spring_forward(self):
        """March 8, 2026 (DST spring forward) — CLI day uses LST via UTC."""
        start, end = get_cli_day_window(
            datetime(2026, 3, 8), "US/Eastern"
        )
        # Eastern Standard Time is UTC-5.  CLI day midnight LST = 05:00 UTC.
        assert start.hour == 5  # 00:00 EST = 05:00 UTC
        assert end.hour == 5  # next day 00:00 EST = 05:00 UTC

    def test_november_dst_fall_back(self):
        """November 1, 2026 (DST fall back) — CLI day uses LST via UTC."""
        start, end = get_cli_day_window(
            datetime(2026, 11, 1), "US/Eastern"
        )
        assert start.hour == 5  # 00:00 EST = 05:00 UTC

    def test_phoenix_any_date(self):
        """Phoenix (no DST) should have same UTC offset year-round."""
        jan_start, _ = get_cli_day_window(
            datetime(2026, 1, 15), "US/Arizona"
        )
        jul_start, _ = get_cli_day_window(
            datetime(2026, 7, 15), "US/Arizona"
        )
        # Arizona is MST year-round (UTC-7), so midnight = 07:00 UTC
        assert jan_start.hour == jul_start.hour

    def test_central_dst_boundary(self):
        """Central time DST transition — should use CST (UTC-6)."""
        start, end = get_cli_day_window(
            datetime(2026, 3, 8), "US/Central"
        )
        assert start.hour == 6  # 00:00 CST = 06:00 UTC

    def test_pacific_dst_boundary(self):
        """Pacific time DST transition — should use PST (UTC-8)."""
        start, end = get_cli_day_window(
            datetime(2026, 3, 8), "US/Pacific"
        )
        assert start.hour == 8  # 00:00 PST = 08:00 UTC


# ── Missing/None orderbook data ─────────────────────────────────────


class TestMissingOrderbookData:
    def test_all_none_orderbook(self):
        """Completely empty orderbook with all None values."""
        ob = OrderbookSnapshot(
            best_yes_bid_cents=None,
            best_no_bid_cents=None,
            implied_best_no_ask_cents=None,
            implied_best_yes_ask_cents=None,
            bid_room_cents=None,
            top3_yes_bids=[],
            top3_no_bids=[],
        )
        liq = assess_liquidity(ob)
        assert liq.verdict == LiquidityVerdict.REJECT

        spread = assess_spread(ob)
        assert spread.verdict.value == "REJECT"

    def test_yes_side_only(self):
        """Only YES bids available — NO side empty."""
        ob = OrderbookSnapshot(
            best_yes_bid_cents=8,
            best_no_bid_cents=None,
            implied_best_no_ask_cents=92,
            implied_best_yes_ask_cents=None,
            bid_room_cents=None,
            top3_yes_bids=[[8, 50], [7, 30]],
            top3_no_bids=[],
        )
        liq = assess_liquidity(ob)
        # YES side has depth, so top_of_book > 0
        assert liq.top_of_book_depth == 50

    def test_no_side_only(self):
        """Only NO bids available — YES side empty."""
        ob = OrderbookSnapshot(
            best_yes_bid_cents=None,
            best_no_bid_cents=89,
            implied_best_no_ask_cents=None,
            implied_best_yes_ask_cents=11,
            bid_room_cents=None,
            top3_yes_bids=[],
            top3_no_bids=[[89, 40], [88, 20]],
        )
        liq = assess_liquidity(ob)
        assert liq.top3_depth == 60

    def test_recommended_limit_all_none(self):
        """Recommended limit with no price data → fallback."""
        ob = OrderbookSnapshot(
            best_yes_bid_cents=None,
            best_no_bid_cents=None,
            implied_best_no_ask_cents=None,
            implied_best_yes_ask_cents=None,
            bid_room_cents=None,
            top3_yes_bids=[],
            top3_no_bids=[],
        )
        limit, rationale, _ = compute_recommended_limit(ob)
        assert limit == 90  # Default fallback
        assert "No implied ask" in rationale


# ── Missing weather/forecast data ───────────────────────────────────


class TestMissingWeatherData:
    def test_no_forecast_data(self):
        """Model should handle missing forecast gracefully."""
        raw = CandidateRaw(
            run_time_et="2026-02-12 07:00 ET",
            target_date_local="2026-02-12",
            city="Chicago",
            market_type=MarketType.HIGH_TEMP,
            event_name="TEST",
            market_ticker="TEST",
            market_url="https://x",
            bracket_definition="50°F or above",
            orderbook_snapshot=OrderbookSnapshot(
                best_yes_bid_cents=9,
                best_no_bid_cents=88,
                implied_best_no_ask_cents=91,
                implied_best_yes_ask_cents=12,
                bid_room_cents=3,
                top3_yes_bids=[[9, 30]],
                top3_no_bids=[[88, 40]],
            ),
        )
        model = model_candidate(raw, forecast=None, current_obs_temp_f=None)
        # Should still produce output with default uncertainty
        assert model.p_yes + model.p_no == 1.0
        assert model.method is not None

    def test_partial_forecast(self):
        """Only high forecast, no low or obs."""
        raw = CandidateRaw(
            run_time_et="2026-02-12 07:00 ET",
            target_date_local="2026-02-12",
            city="Miami",
            market_type=MarketType.HIGH_TEMP,
            event_name="TEST",
            market_ticker="TEST",
            market_url="https://x",
            bracket_definition="80°F or above",
            orderbook_snapshot=OrderbookSnapshot(
                best_yes_bid_cents=9,
                best_no_bid_cents=88,
                implied_best_no_ask_cents=91,
                implied_best_yes_ask_cents=12,
                bid_room_cents=3,
                top3_yes_bids=[[9, 30]],
                top3_no_bids=[[88, 40]],
            ),
        )
        forecast = StationForecast(
            station_icao="KMIA",
            forecast_high_f=82.0,
            forecast_low_f=None,
        )
        model = model_candidate(raw, forecast, current_obs_temp_f=None)
        assert model.p_yes + model.p_no == 1.0

    def test_unknown_city_in_modeler(self):
        """Unknown city should still produce model output."""
        raw = CandidateRaw(
            run_time_et="2026-02-12 07:00 ET",
            target_date_local="2026-02-12",
            city="Timbuktu",
            market_type=MarketType.HIGH_TEMP,
            event_name="TEST",
            market_ticker="TEST",
            market_url="https://x",
            bracket_definition="50°F or above",
            orderbook_snapshot=OrderbookSnapshot(
                best_yes_bid_cents=9,
                best_no_bid_cents=88,
                implied_best_no_ask_cents=91,
                implied_best_yes_ask_cents=12,
                bid_room_cents=3,
                top3_yes_bids=[[9, 30]],
                top3_no_bids=[[88, 40]],
            ),
        )
        forecast = StationForecast(
            station_icao="XXXX",
            forecast_high_f=45.0,
            forecast_low_f=35.0,
        )
        model = model_candidate(raw, forecast, current_obs_temp_f=40.0)
        assert model.p_yes + model.p_no == 1.0


# ── Station outage handling ─────────────────────────────────────────


class TestStationOutage:
    def test_unknown_city_settlement(self):
        """Unknown city produces LOW confidence settlement spec."""
        spec = build_settlement_spec("Timbuktu", MarketType.HIGH_TEMP)
        assert spec.mapping_confidence.value == "LOW"
        assert spec.issuedby == "UNKNOWN"

    def test_unknown_city_correlation(self):
        """Unknown city should be in 'Other' correlation group."""
        assert get_correlation_group("Timbuktu") == "Other"
        assert get_metro_cluster("Timbuktu") == "Standalone"


# ── Correlation cap edge cases ──────────────────────────────────────


class TestCorrelationCapEdgeCases:
    def test_identical_rank_scores(self):
        """Picks with identical rank_score — should still process without error."""
        picks = [
            {"city": "New York", "market_ticker": "T1", "rank_score": 10},
            {"city": "Boston", "market_ticker": "T2", "rank_score": 10},
            {"city": "Philadelphia", "market_ticker": "T3", "rank_score": 10},
            {"city": "New York", "market_ticker": "T4", "rank_score": 10},
        ]
        kept, rejected = enforce_correlation_caps(picks)
        assert len(kept) + len(rejected) == 4
        assert len(rejected) >= 1  # At least 1 rejected (metro cap)

    def test_both_caps_violated(self):
        """Both correlation and metro caps violated simultaneously."""
        picks = [
            {"city": "New York", "market_ticker": "T1", "rank_score": 10},
            {"city": "New York", "market_ticker": "T2", "rank_score": 9},
            {"city": "New York", "market_ticker": "T3", "rank_score": 8},
            {"city": "Boston", "market_ticker": "T4", "rank_score": 7},
            {"city": "Philadelphia", "market_ticker": "T5", "rank_score": 6},
            {"city": "New York", "market_ticker": "T6", "rank_score": 5},
        ]
        kept, rejected = enforce_correlation_caps(picks)
        assert len(rejected) >= 2

    def test_zero_picks(self):
        """Empty picks list should work."""
        kept, rejected = enforce_correlation_caps([])
        assert kept == []
        assert rejected == []

    def test_all_different_groups(self):
        """Each city in a different group — no caps triggered."""
        picks = [
            {"city": "New York", "market_ticker": "T1", "rank_score": 10},
            {"city": "Chicago", "market_ticker": "T2", "rank_score": 9},
            {"city": "Miami", "market_ticker": "T3", "rank_score": 8},
            {"city": "Denver", "market_ticker": "T4", "rank_score": 7},
            {"city": "Dallas", "market_ticker": "T5", "rank_score": 6},
            {"city": "Los Angeles", "market_ticker": "T6", "rank_score": 5},
        ]
        kept, rejected = enforce_correlation_caps(picks)
        assert len(kept) == 6
        assert len(rejected) == 0


# ── Hard reject with None submodule outputs ─────────────────────────


class TestHardRejectNoneOutputs:
    def test_no_settlement_spec(self):
        """Candidate without settlement spec — should NOT reject on confidence."""
        raw = CandidateRaw(
            run_time_et="2026-02-12 07:00 ET",
            target_date_local="2026-02-12",
            city="Chicago",
            market_type=MarketType.HIGH_TEMP,
            event_name="TEST",
            market_ticker="TEST",
            market_url="https://x",
            bracket_definition="50°F or above",
            orderbook_snapshot=OrderbookSnapshot(
                best_yes_bid_cents=9,
                best_no_bid_cents=88,
                implied_best_no_ask_cents=91,
                implied_best_yes_ask_cents=12,
                bid_room_cents=3,
                top3_yes_bids=[[9, 30]],
                top3_no_bids=[[88, 40]],
            ),
        )
        uc = merge_candidate(raw)
        rejected, reason = apply_hard_rejects(uc)
        # Should not be rejected just because no settlement spec
        assert not rejected or "Mapping confidence" not in reason

    def test_no_model_no_lock_in_reject(self):
        """Candidate without model — lock-in gates should not reject."""
        raw = CandidateRaw(
            run_time_et="2026-02-12 07:00 ET",
            target_date_local="2026-02-12",
            city="Chicago",
            market_type=MarketType.HIGH_TEMP,
            event_name="TEST",
            market_ticker="TEST",
            market_url="https://x",
            bracket_definition="50°F or above",
            orderbook_snapshot=OrderbookSnapshot(
                best_yes_bid_cents=9,
                best_no_bid_cents=88,
                implied_best_no_ask_cents=91,
                implied_best_yes_ask_cents=12,
                bid_room_cents=3,
                top3_yes_bids=[[9, 30]],
                top3_no_bids=[[88, 40]],
            ),
        )
        uc = merge_candidate(raw)
        rejected, reason = apply_hard_rejects(uc)
        assert "lock-in" not in reason.lower()

    def test_no_fees_ev_no_ev_reject(self):
        """Candidate without fees_ev — EV gate should not reject."""
        raw = CandidateRaw(
            run_time_et="2026-02-12 07:00 ET",
            target_date_local="2026-02-12",
            city="Chicago",
            market_type=MarketType.HIGH_TEMP,
            event_name="TEST",
            market_ticker="TEST",
            market_url="https://x",
            bracket_definition="50°F or above",
            orderbook_snapshot=OrderbookSnapshot(
                best_yes_bid_cents=9,
                best_no_bid_cents=88,
                implied_best_no_ask_cents=91,
                implied_best_yes_ask_cents=12,
                bid_room_cents=3,
                top3_yes_bids=[[9, 30]],
                top3_no_bids=[[88, 40]],
            ),
        )
        uc = merge_candidate(raw)
        rejected, reason = apply_hard_rejects(uc)
        assert "EV reject" not in reason


# ── Bucket classification with None ask ─────────────────────────────


class TestBucketNoneAsk:
    def test_none_ask_is_rejected(self):
        """Candidate with None ask → REJECTED."""
        raw = CandidateRaw(
            run_time_et="2026-02-12 07:00 ET",
            target_date_local="2026-02-12",
            city="Chicago",
            market_type=MarketType.HIGH_TEMP,
            event_name="TEST",
            market_ticker="TEST",
            market_url="https://x",
            bracket_definition="50°F or above",
            orderbook_snapshot=OrderbookSnapshot(
                best_yes_bid_cents=None,
                best_no_bid_cents=None,
                implied_best_no_ask_cents=None,
                implied_best_yes_ask_cents=None,
                bid_room_cents=None,
                top3_yes_bids=[],
                top3_no_bids=[],
            ),
        )
        uc = merge_candidate(raw)
        bucket, reason = classify_bucket(uc)
        assert bucket == Bucket.REJECTED
