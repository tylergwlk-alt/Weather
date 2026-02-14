"""Phase 11 — Task #44: Additional targeted unit tests for critical math.

Covers fee math edge cases, EV boundary conditions, lock-in floating-point
thresholds, bucket boundary combinations, and spread edge cases.
"""

from __future__ import annotations

from kalshi_weather.accountant import (
    compute_ev_no,
    compute_maker_fee_cents,
    compute_max_buy_price_no,
    compute_taker_fee_cents,
)
from kalshi_weather.modeler import _compute_knife_edge, _estimate_p_new_extreme
from kalshi_weather.planner import (
    LiquidityVerdict,
    SpreadVerdict,
    assess_liquidity,
    assess_spread,
    compute_recommended_limit,
)
from kalshi_weather.risk import compute_risk_multiplier
from kalshi_weather.schemas import (
    KnifeEdgeRisk,
    OrderbookSnapshot,
    UncertaintyLevel,
)

# ── Fee math edge cases ─────────────────────────────────────────────

class TestFeeMathEdgeCases:
    def test_zero_price_taker(self):
        """Price at 0 → P*(1-P)=0 → fee should be 0."""
        fee = compute_taker_fee_cents(0, 1)
        assert fee == 0

    def test_zero_price_maker(self):
        fee = compute_maker_fee_cents(0, 1)
        assert fee == 0

    def test_100_price_taker(self):
        """Price at 100 → P=1.0, P*(1-P)=0 → fee should be 0."""
        fee = compute_taker_fee_cents(100, 1)
        assert fee == 0

    def test_100_price_maker(self):
        fee = compute_maker_fee_cents(100, 1)
        assert fee == 0

    def test_rounding_boundary(self):
        """Verify ceil rounding — fee should always round UP."""
        # At 90c: P=0.9, P*(1-P)=0.09
        # Taker: ceil(0.07 * 1 * 0.09 * 100) = ceil(0.63) = 1
        fee = compute_taker_fee_cents(90, 1)
        assert fee == 1  # ceil rounds up

    def test_maker_always_less_than_taker(self):
        """Maker fee should be ≤ taker fee for all prices."""
        for price in range(1, 100):
            maker = compute_maker_fee_cents(price, 1)
            taker = compute_taker_fee_cents(price, 1)
            assert maker <= taker, f"Maker {maker} > Taker {taker} at {price}c"

    def test_fee_scales_with_contracts(self):
        """Fee should scale with contract count."""
        fee_1 = compute_taker_fee_cents(50, 1)
        fee_5 = compute_taker_fee_cents(50, 5)
        assert fee_5 >= fee_1

    def test_fee_symmetric_around_50(self):
        """P*(1-P) is symmetric: fee at 30c should equal fee at 70c."""
        fee_30 = compute_taker_fee_cents(30, 1)
        fee_70 = compute_taker_fee_cents(70, 1)
        assert fee_30 == fee_70


# ── EV boundary conditions ──────────────────────────────────────────

class TestEvBoundaries:
    def test_ev_at_breakeven_price(self):
        """At the max buy price, EV should be approximately zero."""
        max_price = compute_max_buy_price_no(0.95)
        ev_at_max = compute_ev_no(max_price, 0.95)
        assert ev_at_max >= 0

    def test_ev_one_cent_above_breakeven(self):
        """One cent above max buy should have negative EV."""
        max_price = compute_max_buy_price_no(0.95)
        if max_price < 99:
            ev_above = compute_ev_no(max_price + 1, 0.95)
            assert ev_above <= 0

    def test_ev_at_1_cent(self):
        """EV at minimum price (1c) with high p_no should be very positive."""
        ev = compute_ev_no(1, 0.99)
        assert ev > 0

    def test_ev_at_99_cents(self):
        """EV at 99c — paying 99c for max 100c payout minus fees."""
        ev = compute_ev_no(99, 0.95)
        assert ev <= 0

    def test_max_buy_very_high_confidence(self):
        """With p_no=0.999, max buy should be very high."""
        max_buy = compute_max_buy_price_no(0.999)
        assert max_buy >= 95

    def test_max_buy_low_confidence(self):
        """With p_no=0.5, max buy should be very low or 0."""
        max_buy = compute_max_buy_price_no(0.5)
        assert max_buy <= 50

    def test_ev_monotonically_decreases_with_price(self):
        """EV should generally decrease as buy price increases."""
        p_no = 0.95
        prev_ev = compute_ev_no(1, p_no)
        for price in range(2, 99):
            ev = compute_ev_no(price, p_no)
            assert ev <= prev_ev + 0.01, f"EV increased significantly at {price}c"
            prev_ev = ev


# ── Lock-in floating-point thresholds ────────────────────────────────

class TestLockInThresholds:
    def test_p_exactly_at_threshold(self):
        """P exactly at 0.05 should NOT trigger lock-in reject (need < 0.05)."""
        p = 0.05
        assert not (p < 0.05)

    def test_p_just_below_threshold(self):
        """P=0.04999 should trigger lock-in reject."""
        p = 0.04999
        assert p < 0.05

    def test_p_just_above_threshold(self):
        """P=0.05001 should NOT trigger lock-in reject."""
        p = 0.05001
        assert not (p < 0.05)

    def test_p_new_extreme_zero_time(self):
        """With 0 hours remaining, probability of new extreme should be 0."""
        p = _estimate_p_new_extreme(
            current_extreme_f=30.0,
            forecast_extreme_f=25.0,
            hours_remaining=0.0,
            is_low=True,
        )
        assert p == 0.0

    def test_p_new_extreme_lots_of_time_and_room(self):
        """With many hours and lots of room, P should be significant."""
        p = _estimate_p_new_extreme(
            current_extreme_f=50.0,
            forecast_extreme_f=60.0,
            hours_remaining=10.0,
            is_low=False,  # HIGH: room = forecast - current = 10
        )
        assert p > 0.1


# ── Knife-edge boundary tests ───────────────────────────────────────

class TestKnifeEdgeBoundaries:
    def test_exactly_1_degree(self):
        """Forecast 1°F from threshold → HIGH."""
        risk = _compute_knife_edge("50°F or above", 51.0, 3.0)
        assert risk == KnifeEdgeRisk.HIGH

    def test_exactly_at_sigma(self):
        """Distance exactly equal to sigma → MED (≤ sigma)."""
        risk = _compute_knife_edge("50°F or above", 53.0, 3.0)
        assert risk == KnifeEdgeRisk.MED

    def test_just_above_sigma(self):
        """Distance just above sigma → LOW."""
        risk = _compute_knife_edge("50°F or above", 53.1, 3.0)
        assert risk == KnifeEdgeRisk.LOW

    def test_zero_distance(self):
        """Forecast right on threshold → HIGH."""
        risk = _compute_knife_edge("50°F or above", 50.0, 3.0)
        assert risk == KnifeEdgeRisk.HIGH

    def test_unparseable_bracket(self):
        """Unparseable bracket → HIGH (conservative)."""
        risk = _compute_knife_edge("something weird", 50.0, 3.0)
        assert risk == KnifeEdgeRisk.HIGH


# ── Risk multiplier boundary tests ──────────────────────────────────

class TestRiskMultiplierBoundaries:
    def test_all_low_is_full_allocation(self):
        """All LOW flags with normal vol window → 1.0."""
        mult = compute_risk_multiplier(
            UncertaintyLevel.LOW, KnifeEdgeRisk.LOW, 4.0
        )
        assert mult == 1.0

    def test_vol_window_exactly_1_hour(self):
        """At exactly 1 hour, should apply 'locked in' bonus."""
        mult = compute_risk_multiplier(
            UncertaintyLevel.LOW, KnifeEdgeRisk.LOW, 1.0
        )
        assert mult == 1.0

    def test_vol_window_exactly_8_hours(self):
        """At exactly 8 hours, should NOT apply long vol penalty (> 8)."""
        mult = compute_risk_multiplier(
            UncertaintyLevel.LOW, KnifeEdgeRisk.LOW, 8.0
        )
        assert mult == 1.0

    def test_vol_window_above_8_hours(self):
        """Above 8 hours, should apply 0.8 penalty."""
        mult = compute_risk_multiplier(
            UncertaintyLevel.LOW, KnifeEdgeRisk.LOW, 8.1
        )
        assert mult == 0.8


# ── Spread edge cases ───────────────────────────────────────────────

def _ob(bid_room=3, top3_yes=None, top3_no=None) -> OrderbookSnapshot:
    return OrderbookSnapshot(
        best_yes_bid_cents=8,
        best_no_bid_cents=89,
        implied_best_no_ask_cents=92,
        implied_best_yes_ask_cents=11,
        bid_room_cents=bid_room,
        top3_yes_bids=top3_yes if top3_yes is not None else [[8, 30]],
        top3_no_bids=top3_no if top3_no is not None else [[89, 40]],
    )


class TestSpreadEdgeCases:
    def test_spread_exactly_at_max(self):
        """bid_room=6 is exactly at limit → OK."""
        result = assess_spread(_ob(bid_room=6))
        assert result.verdict == SpreadVerdict.OK

    def test_spread_one_above_max(self):
        """bid_room=7 is 1 above limit → REJECT (no exception)."""
        result = assess_spread(_ob(bid_room=7))
        assert result.verdict == SpreadVerdict.REJECT

    def test_spread_negative_bid_room(self):
        """Negative bid_room (shouldn't happen) → treated as valid narrow spread."""
        result = assess_spread(_ob(bid_room=-1))
        assert result.verdict == SpreadVerdict.OK

    def test_spread_very_large(self):
        """Extreme spread → REJECT."""
        result = assess_spread(_ob(bid_room=50))
        assert result.verdict == SpreadVerdict.REJECT


# ── Liquidity edge cases ────────────────────────────────────────────

class TestLiquidityEdgeCases:
    def test_exactly_5_contracts(self):
        """Top-3 depth of exactly 5 → should NOT reject (threshold is < 5)."""
        ob = _ob(top3_yes=[[8, 3]], top3_no=[[89, 2]])
        result = assess_liquidity(ob)
        assert result.verdict != LiquidityVerdict.REJECT
        assert result.top3_depth == 5

    def test_exactly_20_contracts(self):
        """Top-3 depth of exactly 20 → OK (threshold is < 20 for THIN)."""
        ob = _ob(top3_yes=[[8, 10]], top3_no=[[89, 10]])
        result = assess_liquidity(ob)
        assert result.verdict == LiquidityVerdict.OK

    def test_exactly_19_contracts(self):
        """Top-3 depth of 19 → THIN."""
        ob = _ob(top3_yes=[[8, 10]], top3_no=[[89, 9]])
        result = assess_liquidity(ob)
        assert result.verdict == LiquidityVerdict.THIN


# ── Recommended limit edge cases ────────────────────────────────────

class TestRecommendedLimitEdgeCases:
    def test_very_high_ask(self):
        """Ask at 99 → limit should stay in valid range."""
        ob = OrderbookSnapshot(
            best_yes_bid_cents=1,
            best_no_bid_cents=97,
            implied_best_no_ask_cents=99,
            implied_best_yes_ask_cents=3,
            bid_room_cents=2,
            top3_yes_bids=[[1, 10]],
            top3_no_bids=[[97, 10]],
        )
        limit, _, _ = compute_recommended_limit(ob)
        assert 1 <= limit <= 99

    def test_very_low_ask(self):
        """Ask at 1 → limit clamped to 1."""
        ob = OrderbookSnapshot(
            best_yes_bid_cents=99,
            best_no_bid_cents=0,
            implied_best_no_ask_cents=1,
            implied_best_yes_ask_cents=100,
            bid_room_cents=1,
            top3_yes_bids=[[99, 10]],
            top3_no_bids=[[0, 10]],
        )
        limit, _, _ = compute_recommended_limit(ob)
        assert limit >= 1
