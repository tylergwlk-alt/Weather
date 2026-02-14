"""Phase 11 — Task #46: Backtesting harness tests."""

from __future__ import annotations

import json

from kalshi_weather.backtest import (
    backtest_candidates,
    backtest_from_slates,
)
from kalshi_weather.output import build_daily_slate
from kalshi_weather.schemas import (
    Accounting,
    Bucket,
    DailySlate,
    KnifeEdgeRisk,
    MappingConfidence,
    MarketType,
    ModelOutput,
    OrderbookSnapshot,
    RiskRecommendation,
    SettlementSpec,
    UncertaintyLevel,
    UnifiedCandidate,
)

# ── Helpers ────────────────────────────────────────────────────────────

def _uc(
    ticker="T1",
    city="Chicago",
    ask=91,
    bid=88,
    room=3,
    ev=5.0,
    bucket=Bucket.PRIMARY,
    stake=4.2,
) -> UnifiedCandidate:
    return UnifiedCandidate(
        run_time_et="2026-02-12 07:00 ET",
        target_date_local="2026-02-12",
        city=city,
        market_type=MarketType.HIGH_TEMP,
        event_name="TEST",
        market_ticker=ticker,
        market_url=f"https://kalshi.com/{ticker}",
        bracket_definition="50°F or above",
        settlement_spec=SettlementSpec(
            city=city,
            market_type=MarketType.HIGH_TEMP,
            issuedby="ORD",
            cli_url="https://x",
            what_to_read_in_cli="MAX TEMP",
            day_window_note="CST",
            mapping_confidence=MappingConfidence.HIGH,
        ),
        orderbook_snapshot=OrderbookSnapshot(
            best_yes_bid_cents=100 - ask,
            best_no_bid_cents=bid,
            implied_best_no_ask_cents=ask,
            implied_best_yes_ask_cents=100 - bid,
            bid_room_cents=room,
            top3_yes_bids=[[9, 30]],
            top3_no_bids=[[88, 40]],
        ),
        model=ModelOutput(
            market_ticker=ticker,
            p_yes=0.05,
            p_no=0.95,
            method="test",
            uncertainty_level=UncertaintyLevel.LOW,
            local_time_at_station="2026-02-12 07:00 CST",
            hours_remaining_until_cli_day_close=17.0,
            hours_remaining_in_meaningful_volatility_window=6.0,
            knife_edge_risk=KnifeEdgeRisk.LOW,
        ),
        fees_ev=Accounting(
            market_ticker=ticker,
            implied_p_no_from_implied_ask=0.91,
            fee_est_cents_per_contract=1,
            ev_net_est_cents_at_recommended_limit=ev,
            max_buy_price_no_cents=93,
            edge_vs_implied_pct=4.0,
        ),
        allocation=RiskRecommendation(
            market_ticker=ticker,
            suggested_stake_usd=stake,
            max_loss_usd=stake,
            correlation_group="Great Lakes",
            metro_cluster="Chicago Metro",
        ),
        bucket=bucket,
        bucket_reason="test",
        rank=1,
    )


def _save_slate(slate: DailySlate, path):
    """Helper to save slate JSON."""
    path.write_text(
        json.dumps(slate.model_dump(mode="json"), indent=2, default=str),
        encoding="utf-8",
    )


# ── Backtest from saved slates ──────────────────────────────────────

class TestBacktestFromSlates:
    def test_single_day(self, tmp_path):
        slate = build_daily_slate(
            "2026-02-12 07:00 ET", "2026-02-12",
            primary=[_uc(ticker="P1"), _uc(ticker="P2")],
            tight=[_uc(ticker="T1", bucket=Bucket.TIGHT)],
            near_miss=[],
            rejected=[],
        )
        path = tmp_path / "slate.json"
        _save_slate(slate, path)

        summary = backtest_from_slates([path])
        assert summary.days_tested == 1
        assert summary.total_primary == 2
        assert summary.total_tight == 1
        assert summary.avg_primary_per_day == 2.0

    def test_multi_day(self, tmp_path):
        for i, date in enumerate(["2026-02-10", "2026-02-11", "2026-02-12"]):
            slate = build_daily_slate(
                f"{date} 07:00 ET", date,
                primary=[_uc(ticker=f"P{i}")],
                tight=[], near_miss=[], rejected=[],
            )
            _save_slate(slate, tmp_path / f"slate_{date}.json")

        paths = sorted(tmp_path.glob("slate_*.json"))
        summary = backtest_from_slates(paths)
        assert summary.days_tested == 3
        assert summary.total_primary == 3
        assert summary.avg_primary_per_day == 1.0

    def test_empty_slates(self, tmp_path):
        slate = build_daily_slate(
            "2026-02-12 07:00 ET", "2026-02-12",
            primary=[], tight=[], near_miss=[], rejected=[],
        )
        path = tmp_path / "empty.json"
        _save_slate(slate, path)

        summary = backtest_from_slates([path])
        assert summary.days_tested == 1
        assert summary.total_primary == 0
        assert summary.avg_primary_per_day == 0.0

    def test_missing_file(self, tmp_path):
        summary = backtest_from_slates([tmp_path / "nonexistent.json"])
        assert summary.days_tested == 0

    def test_invalid_json(self, tmp_path):
        bad = tmp_path / "bad.json"
        bad.write_text("not json", encoding="utf-8")
        summary = backtest_from_slates([bad])
        assert summary.days_tested == 0

    def test_avg_ev_computation(self, tmp_path):
        slate = build_daily_slate(
            "2026-02-12 07:00 ET", "2026-02-12",
            primary=[_uc(ticker="P1", ev=4.0), _uc(ticker="P2", ev=6.0)],
            tight=[], near_miss=[], rejected=[],
        )
        path = tmp_path / "slate.json"
        _save_slate(slate, path)

        summary = backtest_from_slates([path])
        assert summary.avg_ev_primary == 5.0  # (4+6)/2

    def test_stake_aggregation(self, tmp_path):
        slate = build_daily_slate(
            "2026-02-12 07:00 ET", "2026-02-12",
            primary=[_uc(ticker="P1", stake=10.0), _uc(ticker="P2", stake=11.0)],
            tight=[_uc(ticker="T1", stake=5.0, bucket=Bucket.TIGHT)],
            near_miss=[], rejected=[],
        )
        path = tmp_path / "slate.json"
        _save_slate(slate, path)

        summary = backtest_from_slates([path])
        result = summary.day_results[0]
        assert result.total_suggested_stake == 26.0  # 10+11+5


# ── Backtest from candidates ────────────────────────────────────────

class TestBacktestCandidates:
    def test_single_date(self, tmp_path):
        candidates = {
            "2026-02-12": [
                _uc(ticker="P1", ask=91, room=3, ev=5.0),
                _uc(ticker="P2", ask=92, room=2, ev=3.0),
            ]
        }
        summary = backtest_candidates(candidates)
        assert summary.days_tested == 1
        assert summary.total_primary >= 1

    def test_multi_date(self, tmp_path):
        candidates = {
            "2026-02-10": [_uc(ticker="P1", ask=91, room=3)],
            "2026-02-11": [_uc(ticker="P2", ask=90, room=2)],
            "2026-02-12": [_uc(ticker="P3", ask=93, room=4)],
        }
        summary = backtest_candidates(candidates)
        assert summary.days_tested == 3

    def test_empty_dates(self):
        summary = backtest_candidates({})
        assert summary.days_tested == 0
        assert summary.total_primary == 0
