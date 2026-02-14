"""Phase 6 tests — Microstructure & Manual Execution Planner."""

from __future__ import annotations

from kalshi_weather.planner import (
    LiquidityVerdict,
    SpreadVerdict,
    assess_liquidity,
    assess_spread,
    build_execution_plan,
    compute_recommended_limit,
    generate_cancel_replace_rules,
    generate_manual_steps,
)
from kalshi_weather.schemas import OrderbookSnapshot

# ── Helpers ────────────────────────────────────────────────────────────

def _ob(
    yes_bid=8,
    no_bid=89,
    implied_no_ask=92,
    bid_room=3,
    top3_yes=None,
    top3_no=None,
) -> OrderbookSnapshot:
    return OrderbookSnapshot(
        best_yes_bid_cents=yes_bid,
        best_no_bid_cents=no_bid,
        implied_best_no_ask_cents=implied_no_ask,
        implied_best_yes_ask_cents=100 - no_bid if no_bid else None,
        bid_room_cents=bid_room,
        top3_yes_bids=[[8, 50], [7, 30], [6, 20]] if top3_yes is None else top3_yes,
        top3_no_bids=[[89, 40], [88, 25], [87, 15]] if top3_no is None else top3_no,
    )


# ── Liquidity assessment (Task #24) ───────────────────────────────────

class TestLiquidity:
    def test_good_liquidity(self):
        ob = _ob(top3_yes=[[8, 50], [7, 30]], top3_no=[[89, 40], [88, 25]])
        result = assess_liquidity(ob)
        assert result.verdict == LiquidityVerdict.OK
        assert result.top3_depth == 145  # 50+30+40+25

    def test_thin_liquidity(self):
        ob = _ob(top3_yes=[[8, 5]], top3_no=[[89, 8]])
        result = assess_liquidity(ob)
        assert result.verdict == LiquidityVerdict.THIN
        assert result.top3_depth == 13

    def test_reject_empty_book(self):
        ob = _ob(top3_yes=[], top3_no=[])
        result = assess_liquidity(ob)
        assert result.verdict == LiquidityVerdict.REJECT
        assert result.top_of_book_depth == 0

    def test_reject_very_thin(self):
        ob = _ob(top3_yes=[[8, 2]], top3_no=[[89, 1]])
        result = assess_liquidity(ob)
        assert result.verdict == LiquidityVerdict.REJECT
        assert result.top3_depth == 3

    def test_one_side_empty(self):
        ob = _ob(top3_yes=[], top3_no=[[89, 30], [88, 20]])
        result = assess_liquidity(ob)
        # top_of_book = 0 + 30 = 30, top3 = 50
        assert result.verdict == LiquidityVerdict.OK


# ── Spread sanity (Task #25) ──────────────────────────────────────────

class TestSpread:
    def test_ok_spread(self):
        ob = _ob(bid_room=3)
        result = assess_spread(ob)
        assert result.verdict == SpreadVerdict.OK
        assert result.spread_cents == 3

    def test_max_spread_boundary(self):
        ob = _ob(bid_room=6)
        result = assess_spread(ob)
        assert result.verdict == SpreadVerdict.OK

    def test_reject_wide_spread(self):
        ob = _ob(bid_room=8)
        result = assess_spread(ob)
        assert result.verdict == SpreadVerdict.REJECT

    def test_wide_spread_exception(self):
        ob = _ob(bid_room=8, top3_yes=[[8, 50], [7, 30]], top3_no=[[89, 40], [88, 25]])
        liq = assess_liquidity(ob)
        result = assess_spread(ob, model_edge_pct=5.0, liquidity=liq)
        assert result.verdict == SpreadVerdict.WIDE_EXCEPTION
        assert "WIDE-SPREAD EXCEPTION" in result.notes

    def test_wide_no_exception_without_edge(self):
        ob = _ob(bid_room=8, top3_yes=[[8, 50]], top3_no=[[89, 40]])
        liq = assess_liquidity(ob)
        result = assess_spread(ob, model_edge_pct=1.0, liquidity=liq)
        assert result.verdict == SpreadVerdict.REJECT

    def test_missing_bid_data(self):
        ob = _ob(bid_room=None)
        result = assess_spread(ob)
        assert result.verdict == SpreadVerdict.REJECT


# ── Bid improvement logic (Task #26) ──────────────────────────────────

class TestRecommendedLimit:
    def test_normal_room(self):
        """bid_room=3 → improve ~2c below ask."""
        ob = _ob(implied_no_ask=92, no_bid=89, bid_room=3)
        limit, rationale, fill = compute_recommended_limit(ob)
        assert 89 <= limit <= 91
        assert "NORMAL" in fill

    def test_wide_room(self):
        """bid_room=6 → improve 3c (midpoint)."""
        ob = _ob(implied_no_ask=92, no_bid=86, bid_room=6)
        limit, rationale, fill = compute_recommended_limit(ob)
        assert 86 <= limit <= 90
        assert "NORMAL" in fill

    def test_tight_room(self):
        """bid_room=1 → TIGHT, improve 1c."""
        ob = _ob(implied_no_ask=92, no_bid=91, bid_room=1)
        limit, rationale, fill = compute_recommended_limit(ob)
        assert limit == 91
        assert "TIGHT" in rationale

    def test_zero_room(self):
        """bid_room=0 → TIGHT."""
        ob = _ob(implied_no_ask=92, no_bid=92, bid_room=0)
        limit, rationale, fill = compute_recommended_limit(ob)
        assert "TIGHT" in rationale

    def test_no_ask_data(self):
        """No implied ask → fallback to bid."""
        ob = _ob(implied_no_ask=None, no_bid=89, bid_room=None)
        limit, rationale, _ = compute_recommended_limit(ob)
        assert limit == 89

    def test_limit_clamped(self):
        """Limit should never go below 1 or above 99."""
        ob = _ob(implied_no_ask=2, no_bid=0, bid_room=2)
        limit, _, _ = compute_recommended_limit(ob)
        assert 1 <= limit <= 99


# ── Manual steps (Task #27) ────────────────────────────────────────────

class TestManualSteps:
    def test_step_count(self):
        steps = generate_manual_steps("TICKER-T50", "https://kalshi.com/t50", 90)
        assert len(steps) == 8

    def test_contains_ticker(self):
        steps = generate_manual_steps("TICKER-T50", "https://kalshi.com/t50", 90)
        combined = " ".join(steps)
        assert "TICKER-T50" in combined
        assert "90c" in combined
        assert "NO" in combined

    def test_with_stake(self):
        steps = generate_manual_steps("T", "http://x", 90, stake_usd=9.0)
        combined = " ".join(steps)
        assert "10 contracts" in combined  # 9.00 * 100 / 90 = 10

    def test_limit_step(self):
        steps = generate_manual_steps("T", "http://x", 92)
        assert any("LIMIT" in s for s in steps)


# ── Cancel/replace rules (Task #28) ────────────────────────────────────

class TestCancelRules:
    def test_has_cancel_rules(self):
        rules = generate_cancel_replace_rules(90, 92)
        assert len(rules) >= 4

    def test_cancel_if_ask_moves(self):
        rules = generate_cancel_replace_rules(90, 92)
        combined = " ".join(rules)
        assert "93" in combined  # limit + 3

    def test_adjust_rule(self):
        rules = generate_cancel_replace_rules(90, 92)
        combined = " ".join(rules)
        assert "91" in combined  # limit + 1

    def test_never_market_order(self):
        rules = generate_cancel_replace_rules(90, 92)
        combined = " ".join(rules)
        assert "NEVER" in combined and "market" in combined

    def test_no_chase_cap(self):
        rules = generate_cancel_replace_rules(90, 92)
        combined = " ".join(rules)
        assert "DO NOT chase" in combined


# ── Full execution plan ───────────────────────────────────────────────

class TestBuildExecutionPlan:
    def test_complete_plan(self):
        ob = _ob(implied_no_ask=92, no_bid=89, bid_room=3)
        plan = build_execution_plan(
            "KXHIGHCHI-T50",
            "https://kalshi.com/markets/KXHIGHCHI-T50",
            ob,
            stake_usd=5.0,
        )
        assert plan.market_ticker == "KXHIGHCHI-T50"
        assert plan.implied_best_no_ask_cents == 92
        assert plan.best_no_bid_cents == 89
        assert plan.bid_room_cents == 3
        assert 89 <= plan.recommended_limit_no_cents <= 91
        assert len(plan.manual_order_steps) == 8
        assert len(plan.cancel_replace_rules) >= 4
        assert plan.limit_rationale != ""
        assert plan.fill_probability_notes != ""

    def test_tight_plan(self):
        ob = _ob(implied_no_ask=92, no_bid=91, bid_room=1)
        plan = build_execution_plan("T", "http://x", ob)
        assert "TIGHT" in plan.limit_rationale
