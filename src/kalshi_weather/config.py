"""Central configuration for the Kalshi Weather Scanner."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class BankrollConfig:
    total_usd: float = 42.00


@dataclass(frozen=True)
class PriceWindowConfig:
    """Implied-best-NO-ask cent windows for bucket classification."""

    primary_low: int = 90
    primary_high: int = 93
    scan_low: int = 88
    scan_high: int = 95
    near_miss_low_band: tuple[int, int] = (88, 89)
    near_miss_high_band: tuple[int, int] = (94, 95)


@dataclass(frozen=True)
class SpreadConfig:
    max_spread_cents: int = 6
    min_bid_room_primary: int = 2


@dataclass(frozen=True)
class CorrelationConfig:
    max_picks_per_correlation_group: int = 3
    max_picks_per_metro_cluster: int = 2


@dataclass(frozen=True)
class LockInConfig:
    """Thresholds for LOW/HIGH temperature lock-in gates."""

    sunrise_buffer_hours: float = 2.0
    peak_buffer_hours: float = 2.0
    p_new_extreme_reject_threshold: float = 0.05


@dataclass(frozen=True)
class StabilityConfig:
    """Multi-run stability thresholds (7 -> 8 -> 9 AM ET)."""

    min_price_move_cents: int = 2


@dataclass(frozen=True)
class FeeConfig:
    """Kalshi fee schedule (Feb 2026).

    Taker fee = ceil(taker_rate * contracts * P * (1 - P))
    Maker fee = ceil(maker_rate * contracts * P * (1 - P))
    where P = price in dollars (cents / 100).
    Fee is per-trade, charged on execution (not settlement).
    """

    taker_rate: float = 0.07
    maker_rate: float = 0.0175
    min_contract_price_cents: int = 1
    max_contract_price_cents: int = 99


@dataclass(frozen=True)
class RunScheduleConfig:
    """Morning run times in ET (24h format)."""

    run_hours_et: tuple[int, ...] = (7, 8, 9)


@dataclass(frozen=True)
class PickLimitsConfig:
    max_primary_picks: int = 10


@dataclass(frozen=True)
class RateLimitConfig:
    """Rate limiting and retry settings for API clients."""

    kalshi_requests_per_second: float = 5.0
    nws_requests_per_second: float = 5.0
    retry_max_attempts: int = 3
    retry_base_delay_seconds: float = 1.0
    retry_max_delay_seconds: float = 30.0
    retry_jitter_seconds: float = 0.5


@dataclass(frozen=True)
class Config:
    """Top-level configuration aggregating all sub-configs."""

    bankroll: BankrollConfig = field(default_factory=BankrollConfig)
    price_window: PriceWindowConfig = field(default_factory=PriceWindowConfig)
    spread: SpreadConfig = field(default_factory=SpreadConfig)
    correlation: CorrelationConfig = field(default_factory=CorrelationConfig)
    lock_in: LockInConfig = field(default_factory=LockInConfig)
    stability: StabilityConfig = field(default_factory=StabilityConfig)
    fees: FeeConfig = field(default_factory=FeeConfig)
    schedule: RunScheduleConfig = field(default_factory=RunScheduleConfig)
    picks: PickLimitsConfig = field(default_factory=PickLimitsConfig)
    rate_limit: RateLimitConfig = field(default_factory=RateLimitConfig)


# Singleton default config — import this throughout the project.
DEFAULT_CONFIG = Config()
