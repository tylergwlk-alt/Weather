# src/kalshi_weather/spike_monitor.py
"""Spike Monitor — detects sudden Kalshi price movements and triggers alerts.

State machine with two modes:
  MONITORING — polls event-level prices every 30s, detects spikes
  BURST — sends 5 emails at 1-minute intervals with edge analysis
"""

from __future__ import annotations

import logging
import time
from collections import deque
from dataclasses import dataclass
from typing import Optional

from kalshi_weather.spike_config import SpikeConfig

logger = logging.getLogger(__name__)


# ── Price History ────────────────────────────────────────────────────


@dataclass
class PriceSnapshot:
    """A single price observation."""

    price_cents: int
    timestamp: float  # monotonic time


class PriceHistory:
    """Rolling price history for all tracked brackets."""

    def __init__(self, max_age_seconds: int = 600) -> None:
        self._max_age = max_age_seconds
        self._data: dict[str, deque[PriceSnapshot]] = {}

    def record(self, ticker: str, price_cents: int, ts: float | None = None) -> None:
        if ts is None:
            ts = time.monotonic()
        if ticker not in self._data:
            self._data[ticker] = deque()
        self._data[ticker].append(PriceSnapshot(price_cents, ts))

    def prune(self, ticker: str, now: float | None = None) -> None:
        if now is None:
            now = time.monotonic()
        if ticker not in self._data:
            return
        cutoff = now - self._max_age
        dq = self._data[ticker]
        while dq and dq[0].timestamp < cutoff:
            dq.popleft()

    def prune_all(self, now: float | None = None) -> None:
        for ticker in list(self._data.keys()):
            self.prune(ticker, now)

    def get_history(self, ticker: str) -> list[PriceSnapshot]:
        return list(self._data.get(ticker, []))


# ── Spike Detection ─────────────────────────────────────────────────


@dataclass
class SpikeEvent:
    """A detected price spike."""

    ticker: str
    old_price: int
    new_price: int
    delta: int
    seconds_elapsed: float


def detect_spike(
    history: PriceHistory,
    config: SpikeConfig,
    now: float | None = None,
    cooldowns: dict[str, float] | None = None,
) -> Optional[SpikeEvent]:
    """Check all tracked tickers for a spike exceeding the threshold.

    Returns the largest spike found, or None.
    """
    if now is None:
        now = time.monotonic()
    if cooldowns is None:
        cooldowns = {}

    window_start = now - config.window_seconds
    best: Optional[SpikeEvent] = None

    for ticker, snapshots in history._data.items():
        # Check cooldown
        if ticker in cooldowns:
            if now - cooldowns[ticker] < config.cooldown_seconds:
                continue

        if len(snapshots) < 2:
            continue

        # Find the oldest snapshot within the window
        oldest_in_window: Optional[PriceSnapshot] = None
        for snap in snapshots:
            if snap.timestamp >= window_start:
                oldest_in_window = snap
                break

        if oldest_in_window is None:
            continue

        # Current price is the latest snapshot
        current = snapshots[-1]
        delta = current.price_cents - oldest_in_window.price_cents

        if delta >= config.spike_threshold_cents:
            elapsed = current.timestamp - oldest_in_window.timestamp
            event = SpikeEvent(
                ticker=ticker,
                old_price=oldest_in_window.price_cents,
                new_price=current.price_cents,
                delta=delta,
                seconds_elapsed=elapsed,
            )
            if best is None or delta > best.delta:
                best = event

    return best
