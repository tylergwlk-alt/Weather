"""Team Lead Merge & Bucket Logic — Phase 8.

Merges all 6 module outputs into unified candidates, applies hard reject
pipeline, classifies into buckets (PRIMARY/TIGHT/NEAR-MISS/REJECTED),
ranks, and enforces pick count limits.
"""

from __future__ import annotations

import logging
from typing import Optional

from kalshi_weather.config import DEFAULT_CONFIG, Config
from kalshi_weather.planner import (
    SpreadVerdict,
    assess_spread,
)
from kalshi_weather.schemas import (
    Accounting,
    Bucket,
    CandidateRaw,
    ExecutionPlan,
    MappingConfidence,
    ModelOutput,
    RiskRecommendation,
    SettlementSpec,
    UncertaintyLevel,
    UnifiedCandidate,
)

logger = logging.getLogger(__name__)


# ── Task #33: Unified object merger ──────────────────────────────────

def merge_candidate(
    raw: CandidateRaw,
    settlement: Optional[SettlementSpec] = None,
    model: Optional[ModelOutput] = None,
    accounting: Optional[Accounting] = None,
    execution: Optional[ExecutionPlan] = None,
    risk: Optional[RiskRecommendation] = None,
) -> UnifiedCandidate:
    """Combine all module outputs into a single UnifiedCandidate."""
    return UnifiedCandidate(
        run_time_et=raw.run_time_et,
        target_date_local=raw.target_date_local,
        city=raw.city,
        market_type=raw.market_type,
        event_name=raw.event_name,
        market_ticker=raw.market_ticker,
        market_url=raw.market_url,
        bracket_definition=raw.bracket_definition,
        settlement_spec=settlement,
        orderbook_snapshot=raw.orderbook_snapshot,
        model=model,
        fees_ev=accounting,
        manual_trade_plan=execution,
        allocation=risk,
        bucket=Bucket.REJECTED,
        bucket_reason="",
        rank=None,
        warnings=[],
    )


# ── Task #34: Hard reject pipeline ──────────────────────────────────

def apply_hard_rejects(candidate: UnifiedCandidate) -> tuple[bool, str]:
    """Apply all hard reject gates in sequence.

    Returns (rejected: bool, reason: str).
    """
    ob = candidate.orderbook_snapshot

    # 1. Mapping confidence must be HIGH.
    if candidate.settlement_spec is not None:
        if candidate.settlement_spec.mapping_confidence != MappingConfidence.HIGH:
            conf = candidate.settlement_spec.mapping_confidence.value
            return True, f"Mapping confidence {conf} != HIGH"

    # 2. Must have implied NO ask (requires best_yes_bid).
    if ob.implied_best_no_ask_cents is None:
        return True, "Cannot compute implied_best_no_ask — missing best_yes_bid"

    # 3. Spread sanity.
    spread = assess_spread(ob)
    if spread.verdict == SpreadVerdict.REJECT:
        return True, f"Spread reject: {spread.notes}"

    # 4. EV must be positive.
    if candidate.fees_ev is not None:
        if candidate.fees_ev.no_trade_reason_if_any:
            return True, f"EV reject: {candidate.fees_ev.no_trade_reason_if_any}"

    # 5. LOW lock-in gate.
    if candidate.model is not None:
        m = candidate.model
        if (
            m.lock_in_flag_if_low is not None
            and m.lock_in_flag_if_low.value == "LOCKING"
            and m.p_new_lower_low_after_now is not None
            and m.p_new_lower_low_after_now < 0.05
        ):
            return True, "LOW lock-in: past sunrise+2h and P(new low) < 5%"

        # 6. HIGH lock-in gate.
        if (
            m.high_lock_in_flag is not None
            and m.high_lock_in_flag.value == "LOCKING"
            and m.p_new_higher_high_after_now is not None
            and m.p_new_higher_high_after_now < 0.05
        ):
            return True, "HIGH lock-in: past peak+2h and P(new high) < 5%"

    return False, ""


# ── Task #35: Bucket classifier ─────────────────────────────────────

def classify_bucket(
    candidate: UnifiedCandidate,
    config: Config = DEFAULT_CONFIG,
) -> tuple[Bucket, str]:
    """Classify a candidate into PRIMARY / TIGHT / NEAR-MISS / REJECTED.

    Assumes hard rejects have already been filtered out.
    """
    ob = candidate.orderbook_snapshot
    ask = ob.implied_best_no_ask_cents
    pw = config.price_window

    if ask is None:
        return Bucket.REJECTED, "No implied NO ask price"

    room = ob.bid_room_cents if ob.bid_room_cents is not None else 0

    # PRIMARY: ask in [90, 93] AND bid_room >= 2
    min_room = config.spread.min_bid_room_primary
    if pw.primary_low <= ask <= pw.primary_high:
        window = f"[{pw.primary_low},{pw.primary_high}]"
        if room >= min_room:
            return Bucket.PRIMARY, f"ask={ask}c in {window}, room={room}c >= {min_room}"
        else:
            return Bucket.TIGHT, f"ask={ask}c in {window}, room={room}c < {min_room}"

    # NEAR-MISS: ask in [88,89] or [94,95]
    lo_lo, lo_hi = pw.near_miss_low_band
    hi_lo, hi_hi = pw.near_miss_high_band
    if lo_lo <= ask <= lo_hi:
        return Bucket.NEAR_MISS, f"ask={ask}c in near-miss low band [{lo_lo},{lo_hi}]"
    if hi_lo <= ask <= hi_hi:
        return Bucket.NEAR_MISS, f"ask={ask}c in near-miss high band [{hi_lo},{hi_hi}]"

    return Bucket.REJECTED, f"ask={ask}c outside scan window"


# ── Task #36: Ranking engine ────────────────────────────────────────

_UNCERTAINTY_RANK = {
    UncertaintyLevel.LOW: 0,
    UncertaintyLevel.MED: 1,
    UncertaintyLevel.HIGH: 2,
}

_KNIFE_EDGE_RANK = {
    "LOW": 0,
    "MED": 1,
    "HIGH": 2,
}


def _rank_sort_key(candidate: UnifiedCandidate) -> tuple:
    """Build a sort key for ranking (lower = better rank).

    Priority order:
    1) EV net (higher is better → negate)
    2) Uncertainty level (LOW is best)
    3) Knife-edge risk (LOW is best)
    4) Liquidity/depth (higher depth is better → negate)
    5) Correlation diversification (fewer picks in same group is better)
    6) Hours remaining in vol window (more is better → negate)
    """
    ev = 0.0
    if candidate.fees_ev is not None:
        ev = candidate.fees_ev.ev_net_est_cents_at_recommended_limit

    uncertainty = 1  # default MED
    knife_edge = 1
    if candidate.model is not None:
        uncertainty = _UNCERTAINTY_RANK.get(candidate.model.uncertainty_level, 1)
        knife_edge = _KNIFE_EDGE_RANK.get(candidate.model.knife_edge_risk.value, 1)

    # Liquidity: sum of top-3 depth from orderbook.
    ob = candidate.orderbook_snapshot
    depth = sum(q for _, q in ob.top3_yes_bids) + sum(q for _, q in ob.top3_no_bids)

    hours_vol = 0.0
    if candidate.model is not None:
        hours_vol = candidate.model.hours_remaining_in_meaningful_volatility_window

    return (
        -ev,           # 1) higher EV first
        uncertainty,   # 2) lower uncertainty first
        knife_edge,    # 3) lower knife-edge first
        -depth,        # 4) higher depth first
        -hours_vol,    # 6) more hours first
    )


def rank_candidates(candidates: list[UnifiedCandidate]) -> list[UnifiedCandidate]:
    """Sort candidates by ranking criteria and assign rank numbers."""
    sorted_cands = sorted(candidates, key=_rank_sort_key)
    for i, c in enumerate(sorted_cands, 1):
        c.rank = i
    return sorted_cands


# ── Task #37: Pick count enforcement ────────────────────────────────

def enforce_pick_counts(
    primary: list[UnifiedCandidate],
    tight: list[UnifiedCandidate],
    near_miss: list[UnifiedCandidate],
    config: Config = DEFAULT_CONFIG,
) -> tuple[list[UnifiedCandidate], list[UnifiedCandidate], list[UnifiedCandidate]]:
    """Enforce pick count limits.

    - Up to max_primary_picks (10) PRIMARY.
    - If <10 PRIMARY, supplement with TIGHT (if EV positive and risks acceptable).
    - NEAR-MISS is watchlist only.
    - Excess PRIMARY are demoted to TIGHT.
    """
    max_picks = config.picks.max_primary_picks

    # Cap PRIMARY.
    if len(primary) > max_picks:
        demoted = primary[max_picks:]
        primary = primary[:max_picks]
        for c in demoted:
            c.bucket = Bucket.TIGHT
            c.bucket_reason += " (demoted: exceeded pick limit)"
        tight = demoted + tight

    return primary, tight, near_miss


# ── Full pipeline ───────────────────────────────────────────────────

def run_bucket_pipeline(
    candidates: list[UnifiedCandidate],
    config: Config = DEFAULT_CONFIG,
) -> tuple[
    list[UnifiedCandidate],
    list[UnifiedCandidate],
    list[UnifiedCandidate],
    list[UnifiedCandidate],
]:
    """Run the full bucket classification, ranking, and enforcement pipeline.

    Returns (primary, tight, near_miss, rejected).
    """
    primary: list[UnifiedCandidate] = []
    tight: list[UnifiedCandidate] = []
    near_miss: list[UnifiedCandidate] = []
    rejected: list[UnifiedCandidate] = []

    for candidate in candidates:
        # Step 1: Hard reject check.
        is_rejected, reason = apply_hard_rejects(candidate)
        if is_rejected:
            candidate.bucket = Bucket.REJECTED
            candidate.bucket_reason = reason
            rejected.append(candidate)
            continue

        # Step 2: Bucket classification.
        bucket, reason = classify_bucket(candidate, config)
        candidate.bucket = bucket
        candidate.bucket_reason = reason

        if bucket == Bucket.PRIMARY:
            primary.append(candidate)
        elif bucket == Bucket.TIGHT:
            tight.append(candidate)
        elif bucket == Bucket.NEAR_MISS:
            near_miss.append(candidate)
        else:
            rejected.append(candidate)

    # Step 3: Rank within each bucket.
    primary = rank_candidates(primary)
    tight = rank_candidates(tight)
    near_miss = rank_candidates(near_miss)

    # Step 4: Enforce pick counts.
    primary, tight, near_miss = enforce_pick_counts(
        primary, tight, near_miss, config
    )

    return primary, tight, near_miss, rejected
