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
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

from kalshi_weather.edge import analyze_city
from kalshi_weather.nws_scraper import NWSScraper
from kalshi_weather.scanner import (
    _classify_series,
    _extract_city_from_event,
    _is_today_event,
)
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


# ── Market polling helpers ───────────────────────────────────────────


def extract_bracket_prices(event: dict) -> dict[str, int]:
    """Extract {ticker: yes_bid_cents} from nested markets."""
    prices: dict[str, int] = {}
    for mkt in event.get("markets", []):
        ticker = mkt.get("ticker", "")
        yes_bid = mkt.get("yes_bid")
        if ticker and yes_bid is not None:
            prices[ticker] = int(yes_bid)
    return prices


def is_in_operating_window(
    now_est: datetime, config: SpikeConfig,
) -> bool:
    """Check if current EST time is within operating hours."""
    return (
        config.start_hour_est
        <= now_est.hour
        <= config.end_hour_est
    )


# ── Burst data collection ───────────────────────────────────────────


def collect_burst_data(
    city: str,
    ticker: str,
    client: object,
    scraper: NWSScraper,
) -> Optional[dict]:
    """Collect edge analysis + orderbook data for a burst email.

    Returns a dict with all fields needed by
    build_spike_email_html, or None if analysis fails.
    """
    report = analyze_city(city, scraper)
    if report is None:
        return None

    # Fetch orderbook for precise current price
    orderbook_price: Optional[int] = None
    try:
        raw_ob = client.get_orderbook(ticker, depth=10)
        ob = raw_ob.get("orderbook", {})
        yes_bids = ob.get("yes") or []
        if yes_bids:
            orderbook_price = yes_bids[-1][0]
    except Exception:
        logger.warning(
            "Failed to fetch orderbook for %s",
            ticker,
            exc_info=True,
        )

    bracket_obj = report.bracket
    return {
        "signal": report.signal.value,
        "signal_reason": report.signal_reason,
        "time_risk": report.time_risk.value,
        "metar_f": report.metar_temp_f,
        "precise_f": report.running_max_f_precise,
        "precise_c": report.running_max_c,
        "precise_source": (
            report.running_max_source or "unknown"
        ),
        "running_max_f": report.running_max_cli_f,
        "margin_c": (
            bracket_obj.margin_below_c
            if bracket_obj
            else None
        ),
        "margin_status": (
            bracket_obj.margin_status.value
            if bracket_obj
            else "UNKNOWN"
        ),
        "current_price": orderbook_price,
    }


# ── Main monitoring loop ────────────────────────────────────────────


def run_spike_monitor(
    client: object,
    config: SpikeConfig | None = None,
    gmail_address: str = "",
    gmail_app_password: str = "",
) -> None:
    """Run the spike monitor state machine.

    This is the main entry point. Runs indefinitely
    (or until interrupted with Ctrl+C). Sleeps through
    off-hours and resumes when the window reopens.
    """
    import time as _time

    from kalshi_weather.schemas import MarketType
    from kalshi_weather.spike_alerter import (
        build_conviction_row,
        build_spike_email_html,
        send_spike_email,
    )

    if config is None:
        config = SpikeConfig()

    history = PriceHistory(
        max_age_seconds=config.window_seconds + 120,
    )
    cooldowns: dict[str, float] = {}
    ticker_meta: dict[str, tuple[str, str, str]] = {}
    et = ZoneInfo("US/Eastern")

    logger.info(
        "Spike monitor starting "
        "(threshold=%d\u00a2, window=%ds, poll=%ds)",
        config.spike_threshold_cents,
        config.window_seconds,
        config.poll_interval_seconds,
    )

    try:
        while True:
            now_est = datetime.now(et)

            if not is_in_operating_window(now_est, config):
                logger.info(
                    "Outside operating window "
                    "(%02d:00-%02d:59 EST). Sleeping...",
                    config.start_hour_est,
                    config.end_hour_est,
                )
                _time.sleep(60)
                continue

            # ── MONITORING phase ─────────────────────────
            mono_now = _time.monotonic()
            today_str = now_est.strftime("%Y-%m-%d")

            try:
                all_events = client.get_all_events(
                    status="open",
                    with_nested_markets=True,
                )
            except Exception:
                logger.warning(
                    "Failed to fetch events",
                    exc_info=True,
                )
                _time.sleep(config.poll_interval_seconds)
                continue

            for event in all_events:
                series = event.get("series_ticker", "")
                classified = _classify_series(series)
                if classified != MarketType.HIGH_TEMP:
                    continue
                if not _is_today_event(event, today_str):
                    continue

                city = _extract_city_from_event(event)
                evt_ticker = event.get(
                    "event_ticker", "",
                )
                prices = extract_bracket_prices(event)

                for ticker, price in prices.items():
                    history.record(
                        ticker, price, mono_now,
                    )
                    bracket_def = ""
                    for mkt in event.get("markets", []):
                        if mkt.get("ticker") == ticker:
                            bracket_def = mkt.get(
                                "yes_sub_title",
                                mkt.get("title", ""),
                            )
                            break
                    ticker_meta[ticker] = (
                        city,
                        bracket_def,
                        evt_ticker,
                    )

            history.prune_all(mono_now)

            spike = detect_spike(
                history, config, mono_now, cooldowns,
            )

            if spike is None:
                logger.debug(
                    "No spike detected "
                    "(%d tickers tracked)",
                    len(ticker_meta),
                )
                _time.sleep(config.poll_interval_seconds)
                continue

            # ── BURST phase ──────────────────────────────
            city, bracket_def, evt_ticker = (
                ticker_meta.get(
                    spike.ticker,
                    ("Unknown", "Unknown", ""),
                )
            )
            logger.info(
                "SPIKE DETECTED: %s %s "
                "— %d\u00a2 \u2192 %d\u00a2 (+%d\u00a2)",
                city,
                bracket_def,
                spike.old_price,
                spike.new_price,
                spike.delta,
            )

            cooldowns[spike.ticker] = _time.monotonic()
            conviction_history: list[dict] = []

            with NWSScraper() as scraper:
                for burst_idx in range(
                    1, config.burst_count + 1,
                ):
                    burst_time = datetime.now(et)
                    time_str = burst_time.strftime(
                        "%I:%M %p EST",
                    ).lstrip("0")

                    data = collect_burst_data(
                        city,
                        spike.ticker,
                        client,
                        scraper,
                    )

                    if data is not None:
                        conviction_history.append({
                            "time_str": time_str,
                            "signal": data["signal"],
                            "temp_f": (
                                data["precise_f"]
                            ),
                            "market_price": (
                                data.get(
                                    "current_price",
                                )
                                or spike.new_price
                            ),
                        })
                    else:
                        conviction_history.append({
                            "time_str": time_str,
                            "signal": "NO_EDGE",
                            "temp_f": None,
                            "market_price": None,
                        })

                    rows: list[str] = []
                    for i, entry in enumerate(
                        conviction_history, 1,
                    ):
                        rows.append(
                            build_conviction_row(
                                index=i,
                                total=(
                                    config.burst_count
                                ),
                                time_str=(
                                    entry["time_str"]
                                ),
                                signal=(
                                    entry["signal"]
                                ),
                                temp_f=(
                                    entry["temp_f"]
                                ),
                                market_price=(
                                    entry[
                                        "market_price"
                                    ]
                                ),
                                is_current=(
                                    i == burst_idx
                                ),
                            )
                        )
                    for j in range(
                        burst_idx + 1,
                        config.burst_count + 1,
                    ):
                        rows.append(
                            build_conviction_row(
                                index=j,
                                total=(
                                    config.burst_count
                                ),
                                time_str="(pending)",
                                signal=None,
                                temp_f=None,
                                market_price=None,
                                is_current=False,
                            )
                        )

                    d = data or {}
                    html = build_spike_email_html(
                        city=city,
                        bracket=bracket_def,
                        email_number=burst_idx,
                        email_total=(
                            config.burst_count
                        ),
                        time_str=time_str,
                        old_price=spike.old_price,
                        new_price=spike.new_price,
                        current_price=(
                            d.get("current_price")
                            or spike.new_price
                        ),
                        spike_delta=spike.delta,
                        metar_f=d.get("metar_f"),
                        precise_f=d.get("precise_f"),
                        precise_c=d.get("precise_c"),
                        precise_source=d.get(
                            "precise_source", "",
                        ),
                        running_max_f=d.get(
                            "running_max_f",
                        ),
                        margin_c=d.get("margin_c"),
                        margin_status=d.get(
                            "margin_status",
                            "UNKNOWN",
                        ),
                        signal=d.get(
                            "signal", "NO_EDGE",
                        ),
                        signal_reason=d.get(
                            "signal_reason",
                            "Analysis unavailable.",
                        ),
                        time_risk=d.get(
                            "time_risk", "UNKNOWN",
                        ),
                        conviction_rows=rows,
                    )

                    subject = (
                        f"SPIKE: {city} "
                        f"{bracket_def} "
                        f"[{burst_idx}"
                        f"/{config.burst_count}]"
                    )
                    if (
                        gmail_address
                        and gmail_app_password
                    ):
                        try:
                            send_spike_email(
                                subject,
                                html,
                                gmail_address,
                                gmail_app_password,
                            )
                        except Exception:
                            logger.exception(
                                "Failed to send "
                                "spike email "
                                "%d/%d",
                                burst_idx,
                                config.burst_count,
                            )
                    else:
                        logger.warning(
                            "Email not configured "
                            "— spike alert "
                            "not sent",
                        )

                    logger.info(
                        "Burst email %d/%d sent: "
                        "%s %s — signal=%s",
                        burst_idx,
                        config.burst_count,
                        city,
                        bracket_def,
                        d.get("signal", "N/A"),
                    )

                    if (
                        burst_idx
                        < config.burst_count
                    ):
                        _time.sleep(
                            config
                            .burst_interval_seconds,
                        )

            logger.info(
                "Burst complete. "
                "Returning to monitoring.",
            )

    except KeyboardInterrupt:
        logger.info(
            "Spike monitor stopped by user.",
        )
