"""Phase 11 — Task #45: Full integration test.

Tests the complete pipeline from CandidateRaw → DailySlate using
synthetic orderbook + weather data, verifying all modules chain correctly.
"""

from __future__ import annotations

from kalshi_weather.accountant import compute_accounting
from kalshi_weather.modeler import model_candidate
from kalshi_weather.orchestrator import run_pipeline
from kalshi_weather.planner import build_execution_plan, compute_recommended_limit
from kalshi_weather.risk import build_risk_recommendation
from kalshi_weather.rules import build_settlement_spec
from kalshi_weather.schemas import (
    CandidateRaw,
    MarketType,
    OrderbookSnapshot,
)
from kalshi_weather.team_lead import merge_candidate
from kalshi_weather.weather_api import StationForecast

# ── Synthetic test data ─────────────────────────────────────────────


def _synthetic_candidate(
    ticker="KXHIGHCHI-26FEB12-T50",
    city="Chicago",
    market_type=MarketType.HIGH_TEMP,
    bracket="50°F or above",
    ask=91,
    bid=88,
    room=3,
) -> CandidateRaw:
    return CandidateRaw(
        run_time_et="2026-02-12 07:00 ET",
        target_date_local="2026-02-12",
        city=city,
        market_type=market_type,
        event_name=f"KXHIGH{city[:3].upper()}",
        market_ticker=ticker,
        market_url=f"https://kalshi.com/markets/{ticker}",
        bracket_definition=bracket,
        orderbook_snapshot=OrderbookSnapshot(
            best_yes_bid_cents=100 - ask,
            best_no_bid_cents=bid,
            implied_best_no_ask_cents=ask,
            implied_best_yes_ask_cents=100 - bid,
            bid_room_cents=room,
            top3_yes_bids=[[100 - ask, 50], [100 - ask - 1, 30]],
            top3_no_bids=[[bid, 40], [bid - 1, 25]],
        ),
    )


def _synthetic_forecast(high_f=42.0, low_f=28.0) -> StationForecast:
    return StationForecast(
        station_icao="KORD",
        forecast_high_f=high_f,
        forecast_low_f=low_f,
    )


# ── Full module chain test ──────────────────────────────────────────


class TestFullModuleChain:
    """Test that all 6 modules chain together correctly."""

    def test_settlement_spec_for_known_city(self):
        """Rules module produces a valid settlement spec for Chicago."""
        spec = build_settlement_spec("Chicago", MarketType.HIGH_TEMP)
        assert spec.issuedby == "MDW"
        assert spec.mapping_confidence.value == "HIGH"
        assert "MAXIMUM" in spec.what_to_read_in_cli

    def test_modeler_produces_valid_output(self):
        """Modeler produces p_yes + p_no that sum to ~1.0."""
        raw = _synthetic_candidate()
        forecast = _synthetic_forecast()
        model = model_candidate(raw, forecast, current_obs_temp_f=35.0)
        assert abs(model.p_yes + model.p_no - 1.0) < 0.001
        assert model.uncertainty_level is not None
        assert model.knife_edge_risk is not None

    def test_accountant_uses_model_output(self):
        """Accountant computes EV from model's p_no."""
        raw = _synthetic_candidate()
        forecast = _synthetic_forecast()
        model = model_candidate(raw, forecast, current_obs_temp_f=35.0)
        ob = raw.orderbook_snapshot
        limit, _, _ = compute_recommended_limit(ob)
        acct = compute_accounting(
            market_ticker=raw.market_ticker,
            orderbook=ob,
            model=model,
            recommended_limit_no_cents=limit,
        )
        assert acct.market_ticker == raw.market_ticker
        assert acct.ev_net_est_cents_at_recommended_limit != 0
        assert acct.max_buy_price_no_cents > 0

    def test_planner_uses_orderbook(self):
        """Planner produces valid execution plan from orderbook."""
        raw = _synthetic_candidate()
        plan = build_execution_plan(
            raw.market_ticker,
            raw.market_url,
            raw.orderbook_snapshot,
            stake_usd=5.0,
        )
        assert plan.recommended_limit_no_cents > 0
        assert len(plan.manual_order_steps) == 8
        assert len(plan.cancel_replace_rules) >= 4

    def test_risk_uses_model_and_accounting(self):
        """Risk module produces valid recommendation."""
        raw = _synthetic_candidate()
        forecast = _synthetic_forecast()
        model = model_candidate(raw, forecast, current_obs_temp_f=35.0)
        ob = raw.orderbook_snapshot
        limit, _, _ = compute_recommended_limit(ob)
        acct = compute_accounting(raw.market_ticker, ob, model, limit)
        rec = build_risk_recommendation(
            raw.market_ticker, "Chicago", model, acct,
        )
        assert rec.correlation_group == "Great Lakes"
        assert rec.metro_cluster == "Chicago Metro"
        assert rec.suggested_stake_usd > 0

    def test_merge_all_modules(self):
        """All 6 module outputs merge into a valid UnifiedCandidate."""
        raw = _synthetic_candidate()
        spec = build_settlement_spec(raw.city, raw.market_type)
        forecast = _synthetic_forecast()
        model = model_candidate(raw, forecast, current_obs_temp_f=35.0)
        ob = raw.orderbook_snapshot
        plan = build_execution_plan(
            raw.market_ticker, raw.market_url, ob,
        )
        limit = plan.recommended_limit_no_cents
        acct = compute_accounting(raw.market_ticker, ob, model, limit)
        rec = build_risk_recommendation(
            raw.market_ticker, raw.city, model, acct,
        )
        unified = merge_candidate(raw, spec, model, acct, plan, rec)

        assert unified.market_ticker == raw.market_ticker
        assert unified.settlement_spec is not None
        assert unified.model is not None
        assert unified.fees_ev is not None
        assert unified.manual_trade_plan is not None
        assert unified.allocation is not None


# ── End-to-end pipeline test ────────────────────────────────────────


class TestEndToEndPipeline:
    """Test the full pipeline from synthetic candidates to DailySlate."""

    def _build_unified_candidates(self):
        """Build a realistic set of unified candidates from scratch."""
        candidates = []
        test_cases = [
            # (ticker, city, bracket, ask, bid, room, obs_f, high_f, low_f)
            ("KXHIGHCHI-T50", "Chicago", "50°F or above", 91, 88, 3, 35.0, 42.0, 28.0),
            ("KXHIGHNYC-T45", "New York", "45°F or above", 92, 90, 2, 38.0, 44.0, 30.0),
            ("KXHIGHMIA-T75", "Miami", "75°F or above", 89, 86, 3, 72.0, 78.0, 65.0),
            ("KXHIGHDAL-T60", "Dallas", "60°F or above", 94, 91, 3, 55.0, 62.0, 45.0),
            ("KXHIGHDEN-T40", "Denver", "40°F or above", 85, 82, 3, 30.0, 38.0, 22.0),
        ]

        for ticker, city, bracket, ask, bid, room, obs, high, low in test_cases:
            raw = _synthetic_candidate(ticker, city, MarketType.HIGH_TEMP, bracket, ask, bid, room)
            spec = build_settlement_spec(city, MarketType.HIGH_TEMP)
            forecast = StationForecast(
                station_icao="TEST",
                forecast_high_f=high,
                forecast_low_f=low,
            )
            model = model_candidate(raw, forecast, current_obs_temp_f=obs)
            ob = raw.orderbook_snapshot
            plan = build_execution_plan(ticker, raw.market_url, ob)
            limit = plan.recommended_limit_no_cents
            acct = compute_accounting(ticker, ob, model, limit)
            rec = build_risk_recommendation(ticker, city, model, acct)
            unified = merge_candidate(raw, spec, model, acct, plan, rec)
            candidates.append(unified)

        return candidates

    def test_pipeline_produces_slate(self, tmp_path):
        """Full pipeline produces a valid DailySlate with correct buckets."""
        candidates = self._build_unified_candidates()
        slate = run_pipeline(
            candidates,
            run_time_et="2026-02-12 07:00 ET",
            target_date="2026-02-12",
            events_scanned=5,
            brackets_scanned=25,
            candidates_in_window=5,
            output_dir=tmp_path,
        )

        total = (
            len(slate.picks_primary)
            + len(slate.picks_tight)
            + len(slate.picks_near_miss)
            + len(slate.rejected)
        )
        assert total == 5  # All 5 candidates accounted for

        # Verify scan stats match.
        assert slate.scan_stats.events_scanned == 5

    def test_pipeline_writes_artifacts(self, tmp_path):
        """Pipeline writes both REPORT.md and DAILY_SLATE.json."""
        candidates = self._build_unified_candidates()
        run_pipeline(
            candidates,
            run_time_et="2026-02-12 07:00 ET",
            target_date="2026-02-12",
            output_dir=tmp_path,
        )

        date_dir = tmp_path / "2026-02-12"
        assert date_dir.exists()
        reports = list(date_dir.glob("REPORT_*.md"))
        jsons = list(date_dir.glob("DAILY_SLATE_*.json"))
        assert len(reports) == 1
        assert len(jsons) == 1

        # Report should contain meaningful content.
        content = reports[0].read_text(encoding="utf-8")
        assert "Slate" in content or "2026-02-12" in content

    def test_multi_run_delta(self, tmp_path):
        """Two sequential runs produce delta notes."""
        candidates = self._build_unified_candidates()

        # Run 1: 7 AM
        run_pipeline(
            candidates,
            run_time_et="2026-02-12 07:00 ET",
            target_date="2026-02-12",
            output_dir=tmp_path,
        )

        # Run 2: 8 AM (same candidates)
        slate2 = run_pipeline(
            candidates,
            run_time_et="2026-02-12 08:00 ET",
            target_date="2026-02-12",
            output_dir=tmp_path,
        )

        # Should have delta notes (even if "no material changes").
        assert len(slate2.notes) > 0

    def test_all_primary_ranked(self, tmp_path):
        """All PRIMARY picks should have rank numbers."""
        candidates = self._build_unified_candidates()
        slate = run_pipeline(
            candidates,
            run_time_et="2026-02-12 07:00 ET",
            target_date="2026-02-12",
            output_dir=tmp_path,
        )

        for pick in slate.picks_primary:
            assert pick.rank is not None
            assert pick.rank >= 1

    def test_rejected_have_reasons(self, tmp_path):
        """All REJECTED picks should have bucket_reason."""
        candidates = self._build_unified_candidates()
        slate = run_pipeline(
            candidates,
            run_time_et="2026-02-12 07:00 ET",
            target_date="2026-02-12",
            output_dir=tmp_path,
        )

        for pick in slate.rejected:
            assert pick.bucket_reason != ""
