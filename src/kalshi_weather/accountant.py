"""Fees & EV Accountant — Phase 5 teammate D.

Computes fee-aware expected value, max acceptable NO buy price,
and edge vs implied probability for each candidate.
"""

from __future__ import annotations

import logging
import math

from kalshi_weather.config import DEFAULT_CONFIG, Config
from kalshi_weather.schemas import Accounting, ModelOutput, OrderbookSnapshot

logger = logging.getLogger(__name__)


def compute_taker_fee_cents(
    price_cents: int, contracts: int, config: Config = DEFAULT_CONFIG,
) -> int:
    """Compute taker fee in cents for buying contracts at a given price.

    Kalshi formula: fee = ceil(taker_rate * contracts * P * (1 - P))
    where P = price_cents / 100.
    """
    p = price_cents / 100.0
    raw = config.fees.taker_rate * contracts * p * (1 - p)
    return math.ceil(raw * 100)  # Convert to cents and round up.


def compute_maker_fee_cents(
    price_cents: int, contracts: int, config: Config = DEFAULT_CONFIG,
) -> int:
    """Compute maker fee in cents (for resting limit orders)."""
    p = price_cents / 100.0
    raw = config.fees.maker_rate * contracts * p * (1 - p)
    return math.ceil(raw * 100)


def compute_ev_no(
    buy_price_no_cents: int,
    p_no: float,
    contracts: int = 1,
    config: Config = DEFAULT_CONFIG,
) -> float:
    """Compute net expected value in cents for buying NO at a given price.

    If NO wins: payout = 100c per contract. Profit = 100 - buy_price.
    Fee is charged on execution (buying).
    If NO loses: lose the buy_price per contract.

    EV = P(NO) * (100 - buy_price) - P(YES) * buy_price - fee
       = P(NO) * 100 - buy_price - fee

    Returns EV in cents (per contract when contracts=1).
    """
    fee = compute_taker_fee_cents(buy_price_no_cents, contracts, config)
    fee_per_contract = fee / contracts if contracts > 0 else 0

    payout_if_win = 100.0 - buy_price_no_cents
    ev = p_no * payout_if_win - (1 - p_no) * buy_price_no_cents - fee_per_contract
    return round(ev, 2)


def compute_max_buy_price_no(
    p_no: float,
    config: Config = DEFAULT_CONFIG,
) -> int:
    """Find the maximum NO price (in cents) where EV >= 0 after fees.

    Searches downward from 99c to find the break-even point.
    """
    for price in range(99, 0, -1):
        ev = compute_ev_no(price, p_no, contracts=1, config=config)
        if ev >= 0:
            return price
    return 0


def compute_edge_vs_implied(p_no_model: float, implied_p_no: float) -> float:
    """Compute edge as percentage: (model_p_no - implied_p_no) / implied_p_no * 100.

    Positive edge means the model thinks NO is more likely than the market implies.
    """
    if implied_p_no <= 0:
        return 0.0
    return round((p_no_model - implied_p_no) / implied_p_no * 100, 2)


def compute_accounting(
    market_ticker: str,
    orderbook: OrderbookSnapshot,
    model: ModelOutput,
    recommended_limit_no_cents: int,
    config: Config = DEFAULT_CONFIG,
) -> Accounting:
    """Produce the full Accounting output for a candidate.

    Parameters
    ----------
    market_ticker : str
    orderbook : OrderbookSnapshot
    model : ModelOutput
    recommended_limit_no_cents : int — the price we'd actually bid at
    config : Config
    """
    implied_no_ask = orderbook.implied_best_no_ask_cents
    implied_p_no = implied_no_ask / 100.0 if implied_no_ask is not None else 0.0

    p_no = model.p_no

    # Use maker fee since our strategy is to place resting limit orders.
    maker_fee_cents = compute_maker_fee_cents(recommended_limit_no_cents, 1, config)
    # We'll use maker fee since our strategy is to place resting limit orders.
    fee_est = maker_fee_cents

    # EV at recommended limit (using maker fee since we're posting limits).
    ev_net = compute_ev_no(recommended_limit_no_cents, p_no, 1, config)
    # Adjust for maker vs taker fee difference.
    taker_fee = compute_taker_fee_cents(recommended_limit_no_cents, 1, config)
    ev_net_maker = ev_net + (taker_fee - maker_fee_cents)

    # Max buy price.
    max_buy = compute_max_buy_price_no(p_no, config)

    # Edge vs implied.
    edge = compute_edge_vs_implied(p_no, implied_p_no)

    # Notes and no-trade reason.
    notes: list[str] = []
    no_trade_reason = None

    if ev_net_maker <= 0:
        no_trade_reason = (
            f"Negative EV at recommended limit {recommended_limit_no_cents}c: "
            f"EV={ev_net_maker:.1f}c"
        )
        notes.append(no_trade_reason)
        logger.debug("No-trade: %s for %s", no_trade_reason, market_ticker)

    if implied_no_ask is not None and recommended_limit_no_cents > implied_no_ask:
        notes.append(
            f"WARNING: limit {recommended_limit_no_cents}c > implied ask {implied_no_ask}c"
        )

    notes.append(f"Taker fee={taker_fee}c, Maker fee={maker_fee_cents}c at limit")
    notes.append(f"Model p(NO)={p_no:.4f}, Implied p(NO)={implied_p_no:.4f}")

    return Accounting(
        market_ticker=market_ticker,
        implied_p_no_from_implied_ask=implied_p_no,
        fee_est_cents_per_contract=fee_est,
        ev_net_est_cents_at_recommended_limit=ev_net_maker,
        max_buy_price_no_cents=max_buy,
        edge_vs_implied_pct=edge,
        accounting_notes=notes,
        no_trade_reason_if_any=no_trade_reason,
    )
