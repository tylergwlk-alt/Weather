"""Configuration for the Spike Monitor."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class SpikeConfig:
    """All spike monitor thresholds — configurable via CLI flags."""

    spike_threshold_cents: int = 15
    window_seconds: int = 360          # 6 minutes
    poll_interval_seconds: int = 30
    burst_count: int = 5
    burst_interval_seconds: int = 60
    start_hour_est: int = 8            # 08:00 EST
    end_hour_est: int = 23             # 23:59 EST
    cooldown_seconds: int = 600        # 10 min before same bracket re-triggers
    tracked_cities: tuple[str, ...] = (
        "Washington", "New Orleans", "Phoenix",
        "San Francisco", "Atlanta", "Minneapolis",
        "Boston", "Las Vegas", "Dallas",
        "Seattle", "Miami", "New York",
    )
