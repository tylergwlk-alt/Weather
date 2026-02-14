"""Risk & Portfolio Manager — Phase 7 teammate F.

Defines correlation groups, enforces correlation caps, allocates
bankroll across picks, and aggregates risk flags.
"""

from __future__ import annotations

import logging
from collections import Counter

from kalshi_weather.config import DEFAULT_CONFIG, Config
from kalshi_weather.schemas import (
    Accounting,
    KnifeEdgeRisk,
    ModelOutput,
    NoTradeEntry,
    RiskRecommendation,
    UncertaintyLevel,
)

logger = logging.getLogger(__name__)

# ── Correlation groups (Task #29) ──────────────────────────────────────
# Group cities by regional weather regime. Cities in the same group
# tend to experience correlated temperature outcomes.

CORRELATION_GROUPS: dict[str, list[str]] = {
    "Northeast": [
        "New York", "NYC", "New York City",
        "Boston",
        "Philadelphia", "Philly",
        "LaGuardia", "LGA",
    ],
    "Mid-Atlantic": [
        "Washington", "Washington D.C.", "DC", "Washington DC",
        "Charlotte",
    ],
    "Southeast": [
        "Miami",
        "Jacksonville",
        "Tampa",
        "Atlanta",
    ],
    "Great Lakes": [
        "Chicago",
        "Detroit",
        "Minneapolis",
    ],
    "South Central": [
        "Dallas", "Dallas-Fort Worth", "DFW",
        "Houston",
        "Austin",
        "San Antonio",
        "Oklahoma City", "OKC",
        "Nashville",
        "New Orleans",
    ],
    "Mountain": [
        "Denver",
        "Phoenix",
        "Las Vegas",
    ],
    "Pacific": [
        "Los Angeles", "LA",
        "San Francisco", "SF",
        "Seattle",
    ],
}

# Metro clusters — cities sharing the same weather station area.
METRO_CLUSTERS: dict[str, list[str]] = {
    "NYC Metro": ["New York", "NYC", "New York City", "LaGuardia", "LGA"],
    "Chicago Metro": ["Chicago"],
    "DFW Metro": ["Dallas", "Dallas-Fort Worth", "DFW"],
    "South Florida": ["Miami", "Tampa"],
    "Texas Triangle": ["Houston", "Austin", "San Antonio"],
    "SoCal": ["Los Angeles", "LA"],
    "NorCal": ["San Francisco", "SF"],
}

# Precompute reverse lookups.
_CITY_TO_CORR_GROUP: dict[str, str] = {}
for _group, _cities in CORRELATION_GROUPS.items():
    for _city in _cities:
        _CITY_TO_CORR_GROUP[_city.lower()] = _group

_CITY_TO_METRO: dict[str, str] = {}
for _cluster, _cities in METRO_CLUSTERS.items():
    for _city in _cities:
        _CITY_TO_METRO[_city.lower()] = _cluster


def _safe_substring_match(key: str, candidate: str) -> bool:
    """Substring match that requires at least 4 chars to avoid false positives."""
    if len(candidate) < 4 or len(key) < 4:
        return False
    return candidate in key or key in candidate


def get_correlation_group(city: str) -> str:
    """Return the correlation group for a city, or 'Other'."""
    key = city.strip().lower()
    if key in _CITY_TO_CORR_GROUP:
        return _CITY_TO_CORR_GROUP[key]
    for k, v in _CITY_TO_CORR_GROUP.items():
        if _safe_substring_match(key, k):
            return v
    return "Other"


def get_metro_cluster(city: str) -> str:
    """Return the metro cluster for a city, or 'Standalone'."""
    key = city.strip().lower()
    if key in _CITY_TO_METRO:
        return _CITY_TO_METRO[key]
    for k, v in _CITY_TO_METRO.items():
        if _safe_substring_match(key, k):
            return v
    return "Standalone"


# ── Correlation caps (Task #30) ────────────────────────────────────────

def enforce_correlation_caps(
    picks: list[dict],
    config: Config = DEFAULT_CONFIG,
) -> tuple[list[dict], list[NoTradeEntry]]:
    """Enforce correlation group and metro cluster caps.

    Parameters
    ----------
    picks : list of dicts with at least 'city', 'market_ticker', and 'rank_score'
            (higher rank_score = better pick, keeps priority).

    Returns
    -------
    (kept, rejected) — kept picks and NoTradeEntry for rejected ones.
    """
    max_corr = config.correlation.max_picks_per_correlation_group
    max_metro = config.correlation.max_picks_per_metro_cluster

    # Sort by rank_score descending (best first).
    sorted_picks = sorted(picks, key=lambda p: p.get("rank_score", 0), reverse=True)

    corr_counts: Counter = Counter()
    metro_counts: Counter = Counter()
    kept: list[dict] = []
    rejected: list[NoTradeEntry] = []

    for pick in sorted_picks:
        city = pick.get("city", "")
        ticker = pick.get("market_ticker", "")
        corr_group = get_correlation_group(city)
        metro = get_metro_cluster(city)

        if corr_counts[corr_group] >= max_corr:
            rejected.append(NoTradeEntry(
                market_ticker=ticker,
                reason=f"Correlation cap: {corr_group} already has {max_corr} picks",
            ))
            continue

        if metro_counts[metro] >= max_metro:
            rejected.append(NoTradeEntry(
                market_ticker=ticker,
                reason=f"Metro cap: {metro} already has {max_metro} picks",
            ))
            continue

        corr_counts[corr_group] += 1
        metro_counts[metro] += 1
        kept.append(pick)

    return kept, rejected


# ── Stake allocation (Task #31) ────────────────────────────────────────

def allocate_stakes(
    picks: list[dict],
    config: Config = DEFAULT_CONFIG,
) -> list[dict]:
    """Distribute bankroll across picks.

    Strategy: equal-weight allocation across all picks, with adjustments
    for risk flags. Each pick gets bankroll / n_picks, then adjusted
    down for high-risk picks.

    Parameters
    ----------
    picks : list of dicts with 'market_ticker', 'risk_multiplier' (0-1, default 1.0),
            and 'limit_cents'.

    Returns
    -------
    picks with 'suggested_stake_usd' and 'max_loss_usd' added.
    """
    if not picks:
        return picks

    bankroll = config.bankroll.total_usd
    n = len(picks)
    base_stake = bankroll / n

    for pick in picks:
        risk_mult = pick.get("risk_multiplier", 1.0)
        stake = round(base_stake * risk_mult, 2)
        stake = max(0.01, min(stake, bankroll))  # Clamp.

        # Max loss = stake (we lose the entire buy if NO doesn't hit).
        pick["suggested_stake_usd"] = stake
        pick["max_loss_usd"] = stake

    return picks


# ── Risk flag aggregation (Task #32) ───────────────────────────────────

def compute_risk_multiplier(
    uncertainty: UncertaintyLevel,
    knife_edge: KnifeEdgeRisk,
    hours_vol_remaining: float,
    liquidity_thin: bool = False,
) -> float:
    """Compute a 0-1 risk multiplier for stake sizing.

    1.0 = full allocation, lower = reduced allocation.
    """
    mult = 1.0

    if uncertainty == UncertaintyLevel.HIGH:
        mult *= 0.5
    elif uncertainty == UncertaintyLevel.MED:
        mult *= 0.8

    if knife_edge == KnifeEdgeRisk.HIGH:
        mult *= 0.4
    elif knife_edge == KnifeEdgeRisk.MED:
        mult *= 0.7

    if hours_vol_remaining < 1:
        mult *= 1.0  # Locked in — actually safer.
    elif hours_vol_remaining > 8:
        mult *= 0.8  # Still very uncertain.

    if liquidity_thin:
        mult *= 0.6

    return round(max(0.1, mult), 2)


def aggregate_risk_flags(
    model: ModelOutput,
    accounting: Accounting,
    liquidity_thin: bool = False,
    spread_wide: bool = False,
) -> list[str]:
    """Collect all risk flags for a candidate."""
    flags: list[str] = []

    if model.uncertainty_level == UncertaintyLevel.HIGH:
        flags.append("HIGH_UNCERTAINTY")
    if model.knife_edge_risk == KnifeEdgeRisk.HIGH:
        flags.append("KNIFE_EDGE_HIGH")
    if model.knife_edge_risk == KnifeEdgeRisk.MED:
        flags.append("KNIFE_EDGE_MED")

    if model.lock_in_flag_if_low and model.lock_in_flag_if_low.value == "LOCKING":
        flags.append("LOW_TEMP_LOCKING")
    if model.high_lock_in_flag and model.high_lock_in_flag.value == "LOCKING":
        flags.append("HIGH_TEMP_LOCKING")

    if model.hours_remaining_in_meaningful_volatility_window > 8:
        flags.append("LONG_VOL_WINDOW")
    if model.hours_remaining_in_meaningful_volatility_window < 1:
        flags.append("VOL_WINDOW_CLOSING")

    if liquidity_thin:
        flags.append("THIN_LIQUIDITY")
    if spread_wide:
        flags.append("WIDE_SPREAD")

    if accounting.no_trade_reason_if_any:
        flags.append("NEGATIVE_EV")

    if accounting.edge_vs_implied_pct < 1.0:
        flags.append("MINIMAL_EDGE")

    return flags


def build_risk_recommendation(
    market_ticker: str,
    city: str,
    model: ModelOutput,
    accounting: Accounting,
    liquidity_thin: bool = False,
    spread_wide: bool = False,
    config: Config = DEFAULT_CONFIG,
) -> RiskRecommendation:
    """Build a complete RiskRecommendation for a candidate."""
    corr_group = get_correlation_group(city)
    metro = get_metro_cluster(city)

    flags = aggregate_risk_flags(model, accounting, liquidity_thin, spread_wide)

    risk_mult = compute_risk_multiplier(
        model.uncertainty_level,
        model.knife_edge_risk,
        model.hours_remaining_in_meaningful_volatility_window,
        liquidity_thin,
    )

    # Base stake from equal allocation (placeholder — actual allocation
    # happens in allocate_stakes after cap enforcement).
    base_stake = config.bankroll.total_usd * risk_mult / 10  # assume ~10 picks
    base_stake = round(max(0.01, base_stake), 2)

    notes: list[str] = []
    if risk_mult < 0.5:
        notes.append(f"Heavily reduced stake (risk_mult={risk_mult})")
    if "NEGATIVE_EV" in flags:
        notes.append("NO TRADE — negative EV")
    if "KNIFE_EDGE_HIGH" in flags and "HIGH_UNCERTAINTY" in flags:
        notes.append("REJECT — knife-edge + high uncertainty combo")

    return RiskRecommendation(
        market_ticker=market_ticker,
        suggested_stake_usd=base_stake,
        max_loss_usd=base_stake,
        risk_flags=flags,
        correlation_group=corr_group,
        metro_cluster=metro,
        risk_notes=notes,
    )
