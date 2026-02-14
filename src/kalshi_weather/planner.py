"""Microstructure & Manual Execution Planner — Phase 6 teammate E.

Assesses liquidity, validates spreads, computes recommended limit prices,
and generates manual order placement instructions.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Optional

from kalshi_weather.config import DEFAULT_CONFIG, Config
from kalshi_weather.schemas import ExecutionPlan, OrderbookSnapshot

logger = logging.getLogger(__name__)


class LiquidityVerdict(str, Enum):
    OK = "OK"
    THIN = "THIN"
    REJECT = "REJECT"


class SpreadVerdict(str, Enum):
    OK = "OK"
    WIDE_EXCEPTION = "WIDE_EXCEPTION"
    REJECT = "REJECT"


@dataclass
class LiquidityAssessment:
    verdict: LiquidityVerdict
    top_of_book_depth: int
    top3_depth: int
    notes: str


@dataclass
class SpreadAssessment:
    verdict: SpreadVerdict
    spread_cents: Optional[int]
    notes: str


# ── Liquidity (Task #24) ──────────────────────────────────────────────

def assess_liquidity(ob: OrderbookSnapshot) -> LiquidityAssessment:
    """Evaluate orderbook liquidity.

    Reject or demote if:
    - Top-of-book depth is zero (no bids at all)
    - Top-3 depth is near-zero (< 5 contracts total)
    """
    # Top-of-book: quantity at the best bid level.
    yes_top = ob.top3_yes_bids[0][1] if ob.top3_yes_bids else 0
    no_top = ob.top3_no_bids[0][1] if ob.top3_no_bids else 0
    top_of_book = yes_top + no_top

    # Top-3 aggregate depth.
    yes_depth = sum(q for _, q in ob.top3_yes_bids)
    no_depth = sum(q for _, q in ob.top3_no_bids)
    top3 = yes_depth + no_depth

    if top_of_book == 0:
        return LiquidityAssessment(
            verdict=LiquidityVerdict.REJECT,
            top_of_book_depth=0,
            top3_depth=top3,
            notes="No bids on either side — book is empty",
        )

    if top3 < 5:
        return LiquidityAssessment(
            verdict=LiquidityVerdict.REJECT,
            top_of_book_depth=top_of_book,
            top3_depth=top3,
            notes=f"Top-3 depth too thin ({top3} contracts)",
        )

    if top3 < 20:
        return LiquidityAssessment(
            verdict=LiquidityVerdict.THIN,
            top_of_book_depth=top_of_book,
            top3_depth=top3,
            notes=f"Thin liquidity — top-3 depth {top3} contracts",
        )

    return LiquidityAssessment(
        verdict=LiquidityVerdict.OK,
        top_of_book_depth=top_of_book,
        top3_depth=top3,
        notes=f"Adequate liquidity — top-3 depth {top3} contracts",
    )


# ── Spread sanity (Task #25) ──────────────────────────────────────────

def assess_spread(
    ob: OrderbookSnapshot,
    model_edge_pct: Optional[float] = None,
    liquidity: Optional[LiquidityAssessment] = None,
    config: Config = DEFAULT_CONFIG,
) -> SpreadAssessment:
    """Check spread sanity.

    Reject if spread > max_spread_cents (6c), unless:
    - Depth is strong AND model edge is large → WIDE_EXCEPTION.
    """
    if ob.bid_room_cents is None:
        return SpreadAssessment(
            verdict=SpreadVerdict.REJECT,
            spread_cents=None,
            notes="Cannot compute spread — missing bid data",
        )

    spread = ob.bid_room_cents

    if spread <= config.spread.max_spread_cents:
        return SpreadAssessment(
            verdict=SpreadVerdict.OK,
            spread_cents=spread,
            notes=f"Spread {spread}c within limit ({config.spread.max_spread_cents}c)",
        )

    # Spread is wide — check for exception.
    strong_depth = (
        liquidity is not None and liquidity.verdict == LiquidityVerdict.OK
    )
    large_edge = model_edge_pct is not None and model_edge_pct > 3.0

    if strong_depth and large_edge:
        return SpreadAssessment(
            verdict=SpreadVerdict.WIDE_EXCEPTION,
            spread_cents=spread,
            notes=(
                f"WIDE-SPREAD EXCEPTION: spread {spread}c > {config.spread.max_spread_cents}c "
                f"but depth is strong and edge is {model_edge_pct:.1f}%"
            ),
        )

    return SpreadAssessment(
        verdict=SpreadVerdict.REJECT,
        spread_cents=spread,
        notes=(
            f"Spread {spread}c exceeds limit ({config.spread.max_spread_cents}c) "
            f"without qualifying for exception"
        ),
    )


# ── Bid improvement logic (Task #26) ──────────────────────────────────

def compute_recommended_limit(
    ob: OrderbookSnapshot,
    config: Config = DEFAULT_CONFIG,
) -> tuple[int, str, str]:
    """Compute the recommended NO limit price.

    Returns (limit_cents, rationale, fill_probability_notes).

    Rules:
    - bid_room >= 2: improve 2-6c below implied ask (prefer midpoint)
    - bid_room < 2: improve 1-3c below implied ask (TIGHT)
    - improvement > 6c: flag LOW FILL PROBABILITY
    """
    ask = ob.implied_best_no_ask_cents
    bid = ob.best_no_bid_cents

    if ask is None:
        return (
            bid if bid is not None else 90,
            "No implied ask available — using best NO bid or default",
            "UNKNOWN fill probability — no ask data",
        )

    room = ob.bid_room_cents if ob.bid_room_cents is not None else 0

    if room >= 2:
        # Standard case: bid 2-6c below the ask. Target midpoint.
        improvement = min(max(room // 2, 2), 6)
        limit = ask - improvement
        rationale = (
            f"bid_room={room}c >= 2: improving {improvement}c below "
            f"implied ask {ask}c"
        )
        fill_notes = "NORMAL fill probability"
    else:
        # Tight spread: bid 1-3c below ask.
        improvement = min(max(1, room), 3)
        limit = ask - improvement
        rationale = (
            f"TIGHT: bid_room={room}c < 2: improving {improvement}c below "
            f"implied ask {ask}c"
        )
        fill_notes = "MODERATE fill probability — tight spread"

    if improvement > 6:
        fill_notes = "LOW FILL PROBABILITY — improvement exceeds 6c"

    # Clamp to valid range.
    limit = max(1, min(99, limit))

    return limit, rationale, fill_notes


# ── Manual order steps (Task #27) ──────────────────────────────────────

def generate_manual_steps(
    market_ticker: str,
    market_url: str,
    limit_no_cents: int,
    stake_usd: Optional[float] = None,
) -> list[str]:
    """Generate human-readable manual order placement steps."""
    contracts_note = ""
    if stake_usd is not None and limit_no_cents > 0:
        max_contracts = int(stake_usd * 100 / limit_no_cents)
        contracts_note = f" ({max_contracts} contracts at {limit_no_cents}c)"

    return [
        f"1. Navigate to {market_url}",
        "2. Select the NO side",
        "3. Set order type to LIMIT",
        f"4. Set limit price to {limit_no_cents}c ($0.{limit_no_cents:02d})",
        f"5. Set quantity{contracts_note}",
        f"6. Review order summary — verify ticker is {market_ticker}",
        "7. Submit order",
        "8. Wait 5-10 minutes, then check fill status",
    ]


# ── Cancel/replace rules (Task #28) ────────────────────────────────────

def generate_cancel_replace_rules(
    limit_no_cents: int,
    implied_no_ask_cents: Optional[int],
) -> list[str]:
    """Generate conditions for when the human should cancel or revise."""
    rules = [
        f"CANCEL if implied NO ask moves above {limit_no_cents + 3}c "
        f"(edge has evaporated)",
        "CANCEL if market status changes to closed/halted",
        "CANCEL if not filled within 15 minutes and edge is shrinking",
    ]

    if implied_no_ask_cents is not None:
        rules.append(
            f"ADJUST +1c toward ask (to {limit_no_cents + 1}c) if not filled "
            f"after 10 min and ask is still at {implied_no_ask_cents}c"
        )
        rules.append(
            f"DO NOT chase above {min(limit_no_cents + 2, implied_no_ask_cents)}c"
        )

    rules.append("NEVER place market orders — always use limits")

    return rules


# ── Full execution plan builder ────────────────────────────────────────

def build_execution_plan(
    market_ticker: str,
    market_url: str,
    ob: OrderbookSnapshot,
    stake_usd: Optional[float] = None,
    config: Config = DEFAULT_CONFIG,
) -> ExecutionPlan:
    """Build a complete ExecutionPlan for a candidate."""
    limit, rationale, fill_notes = compute_recommended_limit(ob, config)

    steps = generate_manual_steps(market_ticker, market_url, limit, stake_usd)
    cancel_rules = generate_cancel_replace_rules(
        limit, ob.implied_best_no_ask_cents
    )

    return ExecutionPlan(
        market_ticker=market_ticker,
        implied_best_no_ask_cents=ob.implied_best_no_ask_cents,
        best_no_bid_cents=ob.best_no_bid_cents,
        bid_room_cents=ob.bid_room_cents,
        recommended_limit_no_cents=limit,
        limit_rationale=rationale,
        manual_order_steps=steps,
        cancel_replace_rules=cancel_rules,
        fill_probability_notes=fill_notes,
    )
