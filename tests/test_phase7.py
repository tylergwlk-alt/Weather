"""Phase 7 tests — Risk & Portfolio Manager."""

from __future__ import annotations

from kalshi_weather.risk import (
    aggregate_risk_flags,
    allocate_stakes,
    build_risk_recommendation,
    compute_risk_multiplier,
    enforce_correlation_caps,
    get_correlation_group,
    get_metro_cluster,
)
from kalshi_weather.schemas import (
    Accounting,
    KnifeEdgeRisk,
    LockInFlag,
    ModelOutput,
    UncertaintyLevel,
)

# ── Helpers ────────────────────────────────────────────────────────────

def _model(
    uncertainty=UncertaintyLevel.LOW,
    knife_edge=KnifeEdgeRisk.LOW,
    hours_vol=6.0,
    lock_low=None,
    lock_high=None,
) -> ModelOutput:
    return ModelOutput(
        market_ticker="TEST",
        p_yes=0.05,
        p_no=0.95,
        method="test",
        uncertainty_level=uncertainty,
        local_time_at_station="2026-02-12 07:00 EST",
        hours_remaining_until_cli_day_close=17.0,
        hours_remaining_in_meaningful_volatility_window=hours_vol,
        knife_edge_risk=knife_edge,
        lock_in_flag_if_low=lock_low,
        high_lock_in_flag=lock_high,
    )


def _accounting(ev=5.0, edge=4.0, no_trade_reason=None) -> Accounting:
    return Accounting(
        market_ticker="TEST",
        implied_p_no_from_implied_ask=0.92,
        fee_est_cents_per_contract=1,
        ev_net_est_cents_at_recommended_limit=ev,
        max_buy_price_no_cents=93,
        edge_vs_implied_pct=edge,
        no_trade_reason_if_any=no_trade_reason,
    )


# ── Correlation groups (Task #29) ──────────────────────────────────────

class TestCorrelationGroups:
    def test_nyc_northeast(self):
        assert get_correlation_group("New York") == "Northeast"

    def test_chicago_great_lakes(self):
        assert get_correlation_group("Chicago") == "Great Lakes"

    def test_miami_southeast(self):
        assert get_correlation_group("Miami") == "Southeast"

    def test_denver_mountain(self):
        assert get_correlation_group("Denver") == "Mountain"

    def test_la_pacific(self):
        assert get_correlation_group("Los Angeles") == "Pacific"

    def test_dallas_south_central(self):
        assert get_correlation_group("Dallas") == "South Central"

    def test_unknown_city(self):
        assert get_correlation_group("Timbuktu") == "Other"

    def test_case_insensitive(self):
        assert get_correlation_group("CHICAGO") == "Great Lakes"

    def test_alias(self):
        assert get_correlation_group("NYC") == "Northeast"
        assert get_correlation_group("DFW") == "South Central"


class TestMetroClusters:
    def test_nyc_metro(self):
        assert get_metro_cluster("New York") == "NYC Metro"
        assert get_metro_cluster("LaGuardia") == "NYC Metro"

    def test_standalone(self):
        assert get_metro_cluster("Denver") == "Standalone"
        assert get_metro_cluster("Atlanta") == "Standalone"

    def test_south_florida(self):
        assert get_metro_cluster("Miami") == "South Florida"
        assert get_metro_cluster("Tampa") == "South Florida"


# ── Correlation caps (Task #30) ────────────────────────────────────────

class TestCorrelationCaps:
    def test_under_limit(self):
        picks = [
            {"city": "New York", "market_ticker": "T1", "rank_score": 10},
            {"city": "Chicago", "market_ticker": "T2", "rank_score": 9},
            {"city": "Miami", "market_ticker": "T3", "rank_score": 8},
        ]
        kept, rejected = enforce_correlation_caps(picks)
        assert len(kept) == 3
        assert len(rejected) == 0

    def test_corr_group_cap(self):
        """4 Northeast picks but cap is 3 → 1 rejected."""
        picks = [
            {"city": "New York", "market_ticker": "T1", "rank_score": 10},
            {"city": "Boston", "market_ticker": "T2", "rank_score": 9},
            {"city": "Philadelphia", "market_ticker": "T3", "rank_score": 8},
            {"city": "New York", "market_ticker": "T4", "rank_score": 7},  # 4th NE
        ]
        kept, rejected = enforce_correlation_caps(picks)
        assert len(kept) == 3
        assert len(rejected) == 1
        assert rejected[0].market_ticker == "T4"
        assert "Correlation cap" in rejected[0].reason

    def test_metro_cap(self):
        """3 NYC Metro picks but cap is 2 → 1 rejected."""
        picks = [
            {"city": "New York", "market_ticker": "T1", "rank_score": 10},
            {"city": "New York", "market_ticker": "T2", "rank_score": 9},
            {"city": "New York", "market_ticker": "T3", "rank_score": 8},
        ]
        kept, rejected = enforce_correlation_caps(picks)
        assert len(kept) == 2
        assert len(rejected) == 1
        assert "Metro cap" in rejected[0].reason

    def test_priority_order(self):
        """Higher rank_score picks should be kept over lower ones."""
        picks = [
            {"city": "New York", "market_ticker": "LOW", "rank_score": 1},
            {"city": "New York", "market_ticker": "HIGH", "rank_score": 10},
            {"city": "New York", "market_ticker": "MED", "rank_score": 5},
        ]
        kept, rejected = enforce_correlation_caps(picks)
        kept_tickers = [p["market_ticker"] for p in kept]
        assert "HIGH" in kept_tickers
        assert "MED" in kept_tickers
        assert "LOW" not in kept_tickers


# ── Stake allocation (Task #31) ────────────────────────────────────────

class TestAllocateStakes:
    def test_equal_allocation(self):
        picks = [
            {"market_ticker": "T1", "limit_cents": 90},
            {"market_ticker": "T2", "limit_cents": 91},
        ]
        result = allocate_stakes(picks)
        # $42 / 2 = $21 each
        assert result[0]["suggested_stake_usd"] == 21.0
        assert result[1]["suggested_stake_usd"] == 21.0

    def test_risk_reduced(self):
        picks = [
            {"market_ticker": "T1", "limit_cents": 90, "risk_multiplier": 1.0},
            {"market_ticker": "T2", "limit_cents": 91, "risk_multiplier": 0.5},
        ]
        result = allocate_stakes(picks)
        assert result[0]["suggested_stake_usd"] > result[1]["suggested_stake_usd"]

    def test_empty_picks(self):
        assert allocate_stakes([]) == []

    def test_max_loss_equals_stake(self):
        picks = [{"market_ticker": "T1", "limit_cents": 90}]
        result = allocate_stakes(picks)
        assert result[0]["max_loss_usd"] == result[0]["suggested_stake_usd"]


# ── Risk multiplier (Task #32) ─────────────────────────────────────────

class TestRiskMultiplier:
    def test_low_risk(self):
        mult = compute_risk_multiplier(
            UncertaintyLevel.LOW, KnifeEdgeRisk.LOW, 4.0
        )
        assert mult >= 0.8

    def test_high_uncertainty(self):
        mult = compute_risk_multiplier(
            UncertaintyLevel.HIGH, KnifeEdgeRisk.LOW, 4.0
        )
        assert mult < 0.6

    def test_knife_edge_high(self):
        mult = compute_risk_multiplier(
            UncertaintyLevel.LOW, KnifeEdgeRisk.HIGH, 4.0
        )
        assert mult < 0.5

    def test_combined_worst_case(self):
        mult = compute_risk_multiplier(
            UncertaintyLevel.HIGH, KnifeEdgeRisk.HIGH, 10.0, liquidity_thin=True
        )
        assert mult <= 0.15

    def test_minimum_floor(self):
        """Multiplier should never go below 0.1."""
        mult = compute_risk_multiplier(
            UncertaintyLevel.HIGH, KnifeEdgeRisk.HIGH, 10.0, liquidity_thin=True
        )
        assert mult >= 0.1


# ── Risk flag aggregation ─────────────────────────────────────────────

class TestRiskFlags:
    def test_clean_candidate(self):
        flags = aggregate_risk_flags(_model(), _accounting())
        assert len(flags) == 0 or "MINIMAL_EDGE" not in flags

    def test_high_uncertainty_flag(self):
        flags = aggregate_risk_flags(
            _model(uncertainty=UncertaintyLevel.HIGH), _accounting()
        )
        assert "HIGH_UNCERTAINTY" in flags

    def test_knife_edge_flag(self):
        flags = aggregate_risk_flags(
            _model(knife_edge=KnifeEdgeRisk.HIGH), _accounting()
        )
        assert "KNIFE_EDGE_HIGH" in flags

    def test_locking_flags(self):
        flags = aggregate_risk_flags(
            _model(lock_low=LockInFlag.LOCKING), _accounting()
        )
        assert "LOW_TEMP_LOCKING" in flags

    def test_negative_ev_flag(self):
        flags = aggregate_risk_flags(
            _model(), _accounting(ev=-2.0, no_trade_reason="neg EV")
        )
        assert "NEGATIVE_EV" in flags

    def test_thin_liquidity_flag(self):
        flags = aggregate_risk_flags(
            _model(), _accounting(), liquidity_thin=True
        )
        assert "THIN_LIQUIDITY" in flags

    def test_minimal_edge_flag(self):
        flags = aggregate_risk_flags(_model(), _accounting(edge=0.5))
        assert "MINIMAL_EDGE" in flags


# ── Full risk recommendation ──────────────────────────────────────────

class TestBuildRiskRecommendation:
    def test_basic(self):
        rec = build_risk_recommendation(
            "KXHIGHCHI-T50", "Chicago", _model(), _accounting()
        )
        assert rec.market_ticker == "KXHIGHCHI-T50"
        assert rec.correlation_group == "Great Lakes"
        assert rec.metro_cluster == "Chicago Metro"
        assert rec.suggested_stake_usd > 0
        assert rec.max_loss_usd > 0
        assert isinstance(rec.risk_flags, list)

    def test_high_risk_reduced_stake(self):
        rec = build_risk_recommendation(
            "T", "Chicago",
            _model(uncertainty=UncertaintyLevel.HIGH, knife_edge=KnifeEdgeRisk.HIGH),
            _accounting(),
        )
        rec_clean = build_risk_recommendation(
            "T", "Chicago", _model(), _accounting()
        )
        assert rec.suggested_stake_usd < rec_clean.suggested_stake_usd

    def test_negative_ev_notes(self):
        rec = build_risk_recommendation(
            "T", "Miami",
            _model(),
            _accounting(ev=-3.0, no_trade_reason="neg EV"),
        )
        notes = " ".join(rec.risk_notes)
        assert "NO TRADE" in notes
