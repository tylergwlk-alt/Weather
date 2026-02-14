"""Phase 5 tests — Fees & EV Accountant."""

from __future__ import annotations

from kalshi_weather.accountant import (
    compute_accounting,
    compute_edge_vs_implied,
    compute_ev_no,
    compute_maker_fee_cents,
    compute_max_buy_price_no,
    compute_taker_fee_cents,
)
from kalshi_weather.schemas import (
    KnifeEdgeRisk,
    ModelOutput,
    OrderbookSnapshot,
    UncertaintyLevel,
)

# ── Fee computation ────────────────────────────────────────────────────

class TestTakerFee:
    def test_at_50_cents(self):
        """Max fee at P=0.50: ceil(0.07 * 1 * 0.5 * 0.5 * 100) = ceil(1.75) = 2c."""
        fee = compute_taker_fee_cents(50, 1)
        assert fee == 2

    def test_at_90_cents(self):
        """P=0.90: ceil(0.07 * 1 * 0.9 * 0.1 * 100) = ceil(0.63) = 1c."""
        fee = compute_taker_fee_cents(90, 1)
        assert fee == 1

    def test_at_95_cents(self):
        """P=0.95: ceil(0.07 * 1 * 0.95 * 0.05 * 100) = ceil(0.3325) = 1c."""
        fee = compute_taker_fee_cents(95, 1)
        assert fee == 1

    def test_at_99_cents(self):
        """P=0.99: ceil(0.07 * 1 * 0.99 * 0.01 * 100) = ceil(0.0693) = 1c."""
        fee = compute_taker_fee_cents(99, 1)
        assert fee == 1

    def test_multiple_contracts(self):
        """Fee scales with contracts."""
        fee_1 = compute_taker_fee_cents(50, 1)
        fee_10 = compute_taker_fee_cents(50, 10)
        assert fee_10 >= fee_1  # Should scale up
        # ceil(0.07 * 10 * 0.5 * 0.5 * 100) = ceil(17.5) = 18
        assert fee_10 == 18

    def test_fee_at_1_cent(self):
        """Extreme: P=0.01."""
        fee = compute_taker_fee_cents(1, 1)
        assert fee == 1  # ceil(0.07 * 0.01 * 0.99 * 100) = ceil(0.0693) = 1


class TestMakerFee:
    def test_at_50_cents(self):
        """Maker at P=0.50: ceil(0.0175 * 1 * 0.5 * 0.5 * 100) = ceil(0.4375) = 1c."""
        fee = compute_maker_fee_cents(50, 1)
        assert fee == 1

    def test_at_90_cents(self):
        """Maker at P=0.90: ceil(0.0175 * 0.9 * 0.1 * 100) = ceil(0.1575) = 1c."""
        fee = compute_maker_fee_cents(90, 1)
        assert fee == 1

    def test_maker_less_than_taker(self):
        """Maker fee should always be <= taker fee."""
        for price in [10, 30, 50, 70, 90, 95]:
            assert compute_maker_fee_cents(price, 1) <= compute_taker_fee_cents(price, 1)


# ── EV computation ─────────────────────────────────────────────────────

class TestComputeEvNo:
    def test_favorable_no(self):
        """Buying NO at 90c with P(NO)=0.96 → positive EV."""
        ev = compute_ev_no(90, p_no=0.96)
        # EV = 0.96 * 10 - 0.04 * 90 - fee = 9.6 - 3.6 - 1 = 5.0
        assert ev > 0

    def test_unfavorable_no(self):
        """Buying NO at 95c with P(NO)=0.90 → likely negative EV."""
        ev = compute_ev_no(95, p_no=0.90)
        # EV = 0.90 * 5 - 0.10 * 95 - fee = 4.5 - 9.5 - 1 = -6.0
        assert ev < 0

    def test_breakeven_area(self):
        """At the right price, EV should be near zero."""
        # P(NO)=0.95, buying at 93c
        ev = compute_ev_no(93, p_no=0.95)
        # EV = 0.95 * 7 - 0.05 * 93 - fee = 6.65 - 4.65 - 1 = 1.0
        assert ev > 0  # Should still be positive

    def test_ev_decreases_with_price(self):
        """Higher buy price → lower EV."""
        ev_88 = compute_ev_no(88, p_no=0.95)
        ev_92 = compute_ev_no(92, p_no=0.95)
        ev_95 = compute_ev_no(95, p_no=0.95)
        assert ev_88 > ev_92 > ev_95


# ── Max buy price ──────────────────────────────────────────────────────

class TestMaxBuyPrice:
    def test_high_confidence(self):
        """P(NO)=0.98 → can afford a high price."""
        max_buy = compute_max_buy_price_no(0.98)
        assert max_buy >= 95

    def test_moderate_confidence(self):
        """P(NO)=0.93 → max buy should be around 91-93."""
        max_buy = compute_max_buy_price_no(0.93)
        assert 85 <= max_buy <= 93

    def test_low_confidence(self):
        """P(NO)=0.50 → max buy should be low."""
        max_buy = compute_max_buy_price_no(0.50)
        assert max_buy < 50

    def test_max_buy_always_profitable(self):
        """EV at max_buy should be >= 0."""
        for p in [0.90, 0.93, 0.95, 0.98]:
            max_buy = compute_max_buy_price_no(p)
            if max_buy > 0:
                ev = compute_ev_no(max_buy, p)
                assert ev >= 0, f"EV={ev} at max_buy={max_buy} for p_no={p}"


# ── Edge vs implied ───────────────────────────────────────────────────

class TestEdge:
    def test_positive_edge(self):
        """Model thinks NO is more likely than market → positive edge."""
        edge = compute_edge_vs_implied(0.96, 0.92)
        assert edge > 0

    def test_negative_edge(self):
        """Model thinks NO is less likely than market → negative edge."""
        edge = compute_edge_vs_implied(0.88, 0.92)
        assert edge < 0

    def test_zero_implied(self):
        edge = compute_edge_vs_implied(0.95, 0.0)
        assert edge == 0.0

    def test_no_edge(self):
        edge = compute_edge_vs_implied(0.92, 0.92)
        assert abs(edge) < 0.01


# ── Full accounting ───────────────────────────────────────────────────

def _make_orderbook(implied_no_ask=92, best_no_bid=89) -> OrderbookSnapshot:
    return OrderbookSnapshot(
        best_yes_bid_cents=100 - implied_no_ask if implied_no_ask else None,
        best_no_bid_cents=best_no_bid,
        implied_best_no_ask_cents=implied_no_ask,
        implied_best_yes_ask_cents=100 - best_no_bid if best_no_bid else None,
        bid_room_cents=(implied_no_ask - best_no_bid)
        if implied_no_ask and best_no_bid
        else None,
    )


def _make_model(p_no=0.96) -> ModelOutput:
    return ModelOutput(
        market_ticker="TEST-T50",
        p_yes=1 - p_no,
        p_no=p_no,
        method="test",
        uncertainty_level=UncertaintyLevel.LOW,
        local_time_at_station="2026-02-12 07:00 EST",
        hours_remaining_until_cli_day_close=17.0,
        hours_remaining_in_meaningful_volatility_window=8.0,
        knife_edge_risk=KnifeEdgeRisk.LOW,
    )


class TestComputeAccounting:
    def test_positive_ev_case(self):
        """Standard candidate: implied_no_ask=92, p(NO)=0.96, limit=90."""
        ob = _make_orderbook(implied_no_ask=92)
        model = _make_model(p_no=0.96)

        result = compute_accounting("TEST-T50", ob, model, recommended_limit_no_cents=90)

        assert result.market_ticker == "TEST-T50"
        assert result.implied_p_no_from_implied_ask == 0.92
        assert result.ev_net_est_cents_at_recommended_limit > 0
        assert result.max_buy_price_no_cents >= 90
        assert result.edge_vs_implied_pct > 0
        assert result.no_trade_reason_if_any is None

    def test_negative_ev_case(self):
        """p(NO) barely above implied → negative EV at high price."""
        ob = _make_orderbook(implied_no_ask=92)
        model = _make_model(p_no=0.90)

        result = compute_accounting("TEST-T50", ob, model, recommended_limit_no_cents=92)

        assert result.ev_net_est_cents_at_recommended_limit <= 0
        assert result.no_trade_reason_if_any is not None
        assert "Negative EV" in result.no_trade_reason_if_any

    def test_notes_include_fee_info(self):
        ob = _make_orderbook()
        model = _make_model()

        result = compute_accounting("TEST-T50", ob, model, recommended_limit_no_cents=90)

        fee_notes = " ".join(result.accounting_notes)
        assert "Taker fee" in fee_notes
        assert "Maker fee" in fee_notes
        assert "Model p(NO)" in fee_notes

    def test_limit_above_ask_warning(self):
        """Should warn if recommended limit > implied ask."""
        ob = _make_orderbook(implied_no_ask=90)
        model = _make_model()

        result = compute_accounting("TEST-T50", ob, model, recommended_limit_no_cents=92)

        notes = " ".join(result.accounting_notes)
        assert "WARNING" in notes
