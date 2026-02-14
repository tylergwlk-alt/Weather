"""Pydantic models for all data schemas defined in the design document.

Covers the 6 teammate output schemas, the unified merged object,
and the final DAILY_SLATE.json structure.
"""

from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field

# ── Enums ──────────────────────────────────────────────────────────────

class MarketType(str, Enum):
    HIGH_TEMP = "HIGH_TEMP"
    LOW_TEMP = "LOW_TEMP"


class MappingConfidence(str, Enum):
    HIGH = "HIGH"
    MED = "MED"
    LOW = "LOW"


class UncertaintyLevel(str, Enum):
    LOW = "LOW"
    MED = "MED"
    HIGH = "HIGH"


class LockInFlag(str, Enum):
    LOCKING = "LOCKING"
    NOT_LOCKED = "NOT_LOCKED"
    UNKNOWN = "UNKNOWN"


class KnifeEdgeRisk(str, Enum):
    LOW = "LOW"
    MED = "MED"
    HIGH = "HIGH"


class Bucket(str, Enum):
    PRIMARY = "PRIMARY"
    TIGHT = "TIGHT"
    NEAR_MISS = "NEAR_MISS"
    REJECTED = "REJECTED"


# ── A) Rules — SETTLEMENT_SPECS ───────────────────────────────────────

class SettlementSpec(BaseModel):
    city: str
    market_type: MarketType
    issuedby: str
    cli_url: str
    what_to_read_in_cli: str
    day_window_note: str
    special_risks: list[str] = Field(default_factory=list)
    mapping_confidence: MappingConfidence
    mapping_notes: list[str] = Field(default_factory=list)


# ── B) Scanner — CANDIDATES_RAW ───────────────────────────────────────

class OrderbookSnapshot(BaseModel):
    best_yes_bid_cents: Optional[int] = None
    best_no_bid_cents: Optional[int] = None
    implied_best_no_ask_cents: Optional[int] = None
    implied_best_yes_ask_cents: Optional[int] = None
    bid_room_cents: Optional[int] = None
    top3_yes_bids: list[list[int]] = Field(default_factory=list)
    top3_no_bids: list[list[int]] = Field(default_factory=list)
    depth_notes: str = ""


class CandidateRaw(BaseModel):
    run_time_et: str
    target_date_local: str
    city: str
    market_type: MarketType
    event_name: str
    market_ticker: str
    market_url: str
    bracket_definition: str
    orderbook_snapshot: OrderbookSnapshot
    market_status_notes: str = ""


# ── C) Modeler — MODEL_OUTPUTS ────────────────────────────────────────

class ModelOutput(BaseModel):
    market_ticker: str
    p_yes: float
    p_no: float
    method: str
    signals_used: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    uncertainty_level: UncertaintyLevel
    local_time_at_station: str
    hours_remaining_until_cli_day_close: float
    hours_remaining_in_meaningful_volatility_window: float
    sunrise_estimate_local: Optional[str] = None
    p_new_lower_low_after_now: Optional[float] = Field(None, alias="P_new_lower_low_after_now")
    lock_in_flag_if_low: Optional[LockInFlag] = None
    typical_peak_time_estimate_local: Optional[str] = None
    p_new_higher_high_after_now: Optional[float] = Field(None, alias="P_new_higher_high_after_now")
    high_lock_in_flag: Optional[LockInFlag] = None
    knife_edge_risk: KnifeEdgeRisk
    model_notes: list[str] = Field(default_factory=list)

    model_config = {"populate_by_name": True}


# ── D) Accountant — ACCOUNTING ────────────────────────────────────────

class Accounting(BaseModel):
    market_ticker: str
    implied_p_no_from_implied_ask: float
    fee_est_cents_per_contract: float
    ev_net_est_cents_at_recommended_limit: float
    max_buy_price_no_cents: int
    edge_vs_implied_pct: float
    accounting_notes: list[str] = Field(default_factory=list)
    no_trade_reason_if_any: Optional[str] = None


# ── E) Planner — EXECUTION_PLANS ──────────────────────────────────────

class ExecutionPlan(BaseModel):
    market_ticker: str
    implied_best_no_ask_cents: Optional[int] = None
    best_no_bid_cents: Optional[int] = None
    bid_room_cents: Optional[int] = None
    recommended_limit_no_cents: int
    limit_rationale: str
    manual_order_steps: list[str] = Field(default_factory=list)
    cancel_replace_rules: list[str] = Field(default_factory=list)
    fill_probability_notes: str = ""


# ── F) Risk — RISK_RECOMMENDATIONS + NO_TRADE_LIST ────────────────────

class RiskRecommendation(BaseModel):
    market_ticker: str
    suggested_stake_usd: float
    max_loss_usd: float
    risk_flags: list[str] = Field(default_factory=list)
    correlation_group: str
    metro_cluster: str
    risk_notes: list[str] = Field(default_factory=list)


class NoTradeEntry(BaseModel):
    market_ticker: str
    reason: str


# ── Unified Merged Object ─────────────────────────────────────────────

class UnifiedCandidate(BaseModel):
    run_time_et: str
    target_date_local: str
    city: str
    market_type: MarketType
    event_name: str
    market_ticker: str
    market_url: str
    bracket_definition: str
    settlement_spec: Optional[SettlementSpec] = None
    orderbook_snapshot: OrderbookSnapshot
    model: Optional[ModelOutput] = None
    fees_ev: Optional[Accounting] = None
    manual_trade_plan: Optional[ExecutionPlan] = None
    allocation: Optional[RiskRecommendation] = None
    bucket: Bucket = Bucket.REJECTED
    bucket_reason: str = ""
    rank: Optional[int] = None
    warnings: list[str] = Field(default_factory=list)


# ── DAILY_SLATE.json top-level ────────────────────────────────────────

class ScanStats(BaseModel):
    events_scanned: int = 0
    bracket_markets_scanned: int = 0
    candidates_in_88_95_window: int = 0
    primary_count: int = 0
    tight_count: int = 0
    near_miss_count: int = 0
    rejected_count: int = 0


class DailySlate(BaseModel):
    run_time_et: str
    target_date_local: str
    bankroll_usd: float = 42.0
    scan_stats: ScanStats = Field(default_factory=ScanStats)
    picks_primary: list[UnifiedCandidate] = Field(default_factory=list)
    picks_tight: list[UnifiedCandidate] = Field(default_factory=list)
    picks_near_miss: list[UnifiedCandidate] = Field(default_factory=list)
    rejected: list[UnifiedCandidate] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
