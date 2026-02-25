# Spike Monitor Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Build a dormant monitoring bot that detects sudden Kalshi HIGH_TEMP price spikes and sends a 5-email burst with minute-by-minute edge analysis.

**Architecture:** Single-loop state machine with two states (MONITORING and BURST). Monitoring polls event-level prices every 30s. When a bracket moves >=20c in 6 minutes, transitions to BURST: fetches precise orderbook + NWS edge analysis, sends 5 HTML emails at 1-minute intervals, then returns to monitoring.

**Tech Stack:** Python 3.14, httpx (HTTP), smtplib (email), existing KalshiClient + NWSScraper + edge.py

---

## Task 1: SpikeConfig Dataclass

**Files:**
- Create: `src/kalshi_weather/spike_config.py`
- Test: `tests/test_spike.py`

**Step 1: Write the failing test**

```python
# tests/test_spike.py
"""Tests for the Spike Monitor system."""

from __future__ import annotations


# ======================================================================
# SpikeConfig Tests
# ======================================================================


class TestSpikeConfig:
    def test_defaults(self):
        from kalshi_weather.spike_config import SpikeConfig

        cfg = SpikeConfig()
        assert cfg.spike_threshold_cents == 20
        assert cfg.window_seconds == 360
        assert cfg.poll_interval_seconds == 30
        assert cfg.burst_count == 5
        assert cfg.burst_interval_seconds == 60
        assert cfg.start_hour_est == 8
        assert cfg.end_hour_est == 23
        assert cfg.cooldown_seconds == 600

    def test_custom_values(self):
        from kalshi_weather.spike_config import SpikeConfig

        cfg = SpikeConfig(spike_threshold_cents=25, window_seconds=300)
        assert cfg.spike_threshold_cents == 25
        assert cfg.window_seconds == 300
```

**Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_spike.py::TestSpikeConfig -v`
Expected: FAIL with "ModuleNotFoundError"

**Step 3: Write minimal implementation**

```python
# src/kalshi_weather/spike_config.py
"""Configuration for the Spike Monitor."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class SpikeConfig:
    """All spike monitor thresholds — configurable via CLI flags."""

    spike_threshold_cents: int = 20
    window_seconds: int = 360          # 6 minutes
    poll_interval_seconds: int = 30
    burst_count: int = 5
    burst_interval_seconds: int = 60
    start_hour_est: int = 8            # 08:00 EST
    end_hour_est: int = 23             # 23:59 EST
    cooldown_seconds: int = 600        # 10 min before same bracket re-triggers
```

**Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_spike.py::TestSpikeConfig -v`
Expected: PASS

**Step 5: Commit**

```bash
git add src/kalshi_weather/spike_config.py tests/test_spike.py
git commit -m "Add SpikeConfig dataclass with configurable thresholds"
```

---

## Task 2: Price History & Spike Detection Logic

**Files:**
- Create: `src/kalshi_weather/spike_monitor.py` (partial — detection only)
- Test: `tests/test_spike.py` (append)

This is the core detection algorithm: maintain a rolling price history per
bracket ticker, and detect when any bracket jumps >= threshold within the
lookback window.

**Step 1: Write the failing tests**

Append to `tests/test_spike.py`:

```python
import time
from collections import deque
from datetime import datetime
from zoneinfo import ZoneInfo


# ======================================================================
# Spike Detection Tests
# ======================================================================


class TestPriceHistory:
    """Test the rolling price snapshot storage."""

    def test_add_snapshot(self):
        from kalshi_weather.spike_monitor import PriceHistory

        ph = PriceHistory(max_age_seconds=360)
        now = time.monotonic()
        ph.record("TICKER-A", 10, now)
        assert len(ph.get_history("TICKER-A")) == 1

    def test_old_entries_pruned(self):
        from kalshi_weather.spike_monitor import PriceHistory

        ph = PriceHistory(max_age_seconds=360)
        old_time = time.monotonic() - 400  # 400s ago, outside 360s window
        ph.record("TICKER-A", 10, old_time)
        ph.record("TICKER-A", 15, time.monotonic())
        ph.prune("TICKER-A")
        assert len(ph.get_history("TICKER-A")) == 1

    def test_unknown_ticker_empty(self):
        from kalshi_weather.spike_monitor import PriceHistory

        ph = PriceHistory(max_age_seconds=360)
        assert ph.get_history("UNKNOWN") == []


class TestSpikeDetection:
    """Test the spike detection algorithm."""

    def test_spike_detected(self):
        from kalshi_weather.spike_monitor import PriceHistory, detect_spike
        from kalshi_weather.spike_config import SpikeConfig

        cfg = SpikeConfig(spike_threshold_cents=20, window_seconds=360)
        ph = PriceHistory(max_age_seconds=360)
        now = time.monotonic()

        # Price was 7 three minutes ago
        ph.record("BRACKET-A", 7, now - 180)
        # Price is now 32
        ph.record("BRACKET-A", 32, now)

        result = detect_spike(ph, cfg, now)
        assert result is not None
        assert result.ticker == "BRACKET-A"
        assert result.old_price == 7
        assert result.new_price == 32
        assert result.delta == 25

    def test_no_spike_below_threshold(self):
        from kalshi_weather.spike_monitor import PriceHistory, detect_spike
        from kalshi_weather.spike_config import SpikeConfig

        cfg = SpikeConfig(spike_threshold_cents=20)
        ph = PriceHistory(max_age_seconds=360)
        now = time.monotonic()

        ph.record("BRACKET-A", 10, now - 180)
        ph.record("BRACKET-A", 25, now)  # +15, below 20 threshold

        result = detect_spike(ph, cfg, now)
        assert result is None

    def test_no_spike_outside_window(self):
        from kalshi_weather.spike_monitor import PriceHistory, detect_spike
        from kalshi_weather.spike_config import SpikeConfig

        cfg = SpikeConfig(spike_threshold_cents=20, window_seconds=360)
        ph = PriceHistory(max_age_seconds=600)
        now = time.monotonic()

        # Price was 7 but 7 minutes ago (outside 6-min window)
        ph.record("BRACKET-A", 7, now - 420)
        ph.record("BRACKET-A", 32, now)

        result = detect_spike(ph, cfg, now)
        assert result is None

    def test_largest_spike_wins(self):
        from kalshi_weather.spike_monitor import PriceHistory, detect_spike
        from kalshi_weather.spike_config import SpikeConfig

        cfg = SpikeConfig(spike_threshold_cents=20)
        ph = PriceHistory(max_age_seconds=360)
        now = time.monotonic()

        ph.record("BRACKET-A", 10, now - 180)
        ph.record("BRACKET-A", 35, now)  # +25

        ph.record("BRACKET-B", 5, now - 180)
        ph.record("BRACKET-B", 50, now)  # +45

        result = detect_spike(ph, cfg, now)
        assert result is not None
        assert result.ticker == "BRACKET-B"
        assert result.delta == 45

    def test_cooldown_prevents_retrigger(self):
        from kalshi_weather.spike_monitor import PriceHistory, detect_spike
        from kalshi_weather.spike_config import SpikeConfig

        cfg = SpikeConfig(spike_threshold_cents=20, cooldown_seconds=600)
        ph = PriceHistory(max_age_seconds=360)
        now = time.monotonic()

        ph.record("BRACKET-A", 7, now - 180)
        ph.record("BRACKET-A", 32, now)

        # First detection works
        result = detect_spike(ph, cfg, now)
        assert result is not None

        # Same bracket in cooldown
        cooldowns = {"BRACKET-A": now}
        result = detect_spike(ph, cfg, now, cooldowns=cooldowns)
        assert result is None

        # After cooldown expires
        result = detect_spike(ph, cfg, now + 601, cooldowns=cooldowns)
        assert result is not None
```

**Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_spike.py::TestPriceHistory tests/test_spike.py::TestSpikeDetection -v`
Expected: FAIL with import errors

**Step 3: Write minimal implementation**

```python
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
from dataclasses import dataclass, field
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
```

**Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_spike.py -v`
Expected: ALL PASS

**Step 5: Commit**

```bash
git add src/kalshi_weather/spike_monitor.py tests/test_spike.py
git commit -m "Add spike detection: PriceHistory and detect_spike algorithm"
```

---

## Task 3: HTML Email Builder (spike_alerter.py)

**Files:**
- Create: `src/kalshi_weather/spike_alerter.py`
- Test: `tests/test_spike.py` (append)

Builds HTML emails with inline CSS, color-coded signal banners,
and conviction trend tables. Sends via existing Gmail SMTP pattern.

**Step 1: Write the failing tests**

Append to `tests/test_spike.py`:

```python
# ======================================================================
# HTML Email Builder Tests
# ======================================================================


class TestSignalColor:
    def test_strong_buy_green(self):
        from kalshi_weather.spike_alerter import signal_to_color

        color, label = signal_to_color("STRONG_BUY")
        assert color == "#22c55e"
        assert label == "STRONG_BUY"

    def test_buy_green(self):
        from kalshi_weather.spike_alerter import signal_to_color

        color, _ = signal_to_color("BUY")
        assert color == "#22c55e"

    def test_hold_yellow(self):
        from kalshi_weather.spike_alerter import signal_to_color

        color, _ = signal_to_color("HOLD")
        assert color == "#eab308"

    def test_caution_red(self):
        from kalshi_weather.spike_alerter import signal_to_color

        color, _ = signal_to_color("CAUTION")
        assert color == "#ef4444"

    def test_no_edge_red(self):
        from kalshi_weather.spike_alerter import signal_to_color

        color, _ = signal_to_color("NO_EDGE")
        assert color == "#ef4444"


class TestBuildConvictionRow:
    def test_completed_row(self):
        from kalshi_weather.spike_alerter import build_conviction_row

        row = build_conviction_row(
            index=1, total=5, time_str="3:39 PM",
            signal="STRONG_BUY", temp_f=39.9, market_price=32,
            is_current=False,
        )
        assert "3:39 PM" in row
        assert "STRONG_BUY" in row
        assert "39.9" in row
        assert "32" in row

    def test_pending_row(self):
        from kalshi_weather.spike_alerter import build_conviction_row

        row = build_conviction_row(
            index=3, total=5, time_str="3:41 PM",
            signal=None, temp_f=None, market_price=None,
            is_current=False,
        )
        assert "(pending)" in row

    def test_current_row_marker(self):
        from kalshi_weather.spike_alerter import build_conviction_row

        row = build_conviction_row(
            index=2, total=5, time_str="3:40 PM",
            signal="BUY", temp_f=39.9, market_price=29,
            is_current=True,
        )
        assert "\u2190" in row or "here" in row.lower()


class TestBuildSpikeEmailHtml:
    def test_contains_key_elements(self):
        from kalshi_weather.spike_alerter import build_spike_email_html

        html = build_spike_email_html(
            city="Chicago",
            bracket="40-41\u00b0F",
            email_number=2,
            email_total=5,
            time_str="3:42 PM EST",
            old_price=7,
            new_price=32,
            current_price=29,
            spike_delta=25,
            metar_f=39,
            precise_f=39.9,
            precise_c=4.4,
            precise_source="NWS Current Conditions",
            running_max_f=40,
            margin_c=0.23,
            margin_status="COMFORTABLE",
            signal="STRONG_BUY",
            signal_reason="Precise data shows 40F with COMFORTABLE margin.",
            time_risk="PAST_PEAK",
            conviction_rows=["row1", "row2"],
        )
        assert "Chicago" in html
        assert "40-41" in html
        assert "#22c55e" in html  # green for STRONG_BUY
        assert "STRONG_BUY" in html
        assert "2 of 5" in html or "2/5" in html
        assert "39.9" in html

    def test_caution_uses_red(self):
        from kalshi_weather.spike_alerter import build_spike_email_html

        html = build_spike_email_html(
            city="Miami",
            bracket="78-79\u00b0F",
            email_number=1,
            email_total=5,
            time_str="4:00 PM EST",
            old_price=10,
            new_price=35,
            current_price=35,
            spike_delta=25,
            metar_f=78,
            precise_f=78.1,
            precise_c=25.6,
            precise_source="METAR T-group",
            running_max_f=78,
            margin_c=0.05,
            margin_status="RAZOR_THIN",
            signal="CAUTION",
            signal_reason="Razor thin margin.",
            time_risk="STILL_RISING",
            conviction_rows=[],
        )
        assert "#ef4444" in html  # red for CAUTION


class TestSendSpikeEmail:
    def test_send_constructs_html_message(self):
        """Verify email is constructed with HTML content type."""
        from unittest.mock import patch, MagicMock
        from kalshi_weather.spike_alerter import send_spike_email

        with patch("kalshi_weather.spike_alerter.smtplib.SMTP") as mock_smtp:
            mock_server = MagicMock()
            mock_smtp.return_value.__enter__ = MagicMock(return_value=mock_server)
            mock_smtp.return_value.__exit__ = MagicMock(return_value=False)

            send_spike_email(
                subject="Test",
                html_body="<html><body>Test</body></html>",
                gmail_address="test@gmail.com",
                gmail_app_password="password",
            )

            mock_server.send_message.assert_called_once()
            msg = mock_server.send_message.call_args[0][0]
            assert msg["To"] == "test@gmail.com"
            assert msg["From"] == "test@gmail.com"
```

**Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_spike.py::TestSignalColor tests/test_spike.py::TestBuildConvictionRow tests/test_spike.py::TestBuildSpikeEmailHtml tests/test_spike.py::TestSendSpikeEmail -v`
Expected: FAIL with import errors

**Step 3: Write minimal implementation**

```python
# src/kalshi_weather/spike_alerter.py
"""Spike Alert Emailer — HTML emails with color-coded trading signals.

Sends to self (Gmail to Gmail) with inline CSS for mobile compatibility.
"""

from __future__ import annotations

import logging
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Optional

logger = logging.getLogger(__name__)

SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 587

# ── Signal color mapping ─────────────────────────────────────────────

_SIGNAL_COLORS: dict[str, str] = {
    "STRONG_BUY": "#22c55e",
    "BUY": "#22c55e",
    "HOLD": "#eab308",
    "CAUTION": "#ef4444",
    "NO_EDGE": "#ef4444",
}


def signal_to_color(signal: str) -> tuple[str, str]:
    """Return (hex_color, label) for a signal string."""
    color = _SIGNAL_COLORS.get(signal, "#6b7280")
    return color, signal


# ── Conviction trend rows ────────────────────────────────────────────


def build_conviction_row(
    index: int,
    total: int,
    time_str: str,
    signal: Optional[str],
    temp_f: Optional[float],
    market_price: Optional[int],
    is_current: bool,
) -> str:
    """Build one row of the conviction trend table."""
    marker = " \u2190 you are here" if is_current else ""
    if signal is None:
        return (
            f'<tr style="color:#9ca3af;">'
            f"<td>[{index}/{total}]</td>"
            f"<td>{time_str}</td>"
            f"<td>(pending)</td>"
            f"<td></td>"
            f"<td></td>"
            f"</tr>"
        )
    color, _ = signal_to_color(signal)
    return (
        f"<tr>"
        f"<td>[{index}/{total}]</td>"
        f"<td>{time_str}</td>"
        f'<td style="color:{color};font-weight:bold;">{signal}</td>'
        f"<td>{temp_f:.1f}\u00b0F</td>"
        f"<td>{market_price}\u00a2{marker}</td>"
        f"</tr>"
    )


# ── Full HTML email builder ──────────────────────────────────────────


def build_spike_email_html(
    city: str,
    bracket: str,
    email_number: int,
    email_total: int,
    time_str: str,
    old_price: int,
    new_price: int,
    current_price: int,
    spike_delta: int,
    metar_f: Optional[int],
    precise_f: Optional[float],
    precise_c: Optional[float],
    precise_source: str,
    running_max_f: Optional[int],
    margin_c: Optional[float],
    margin_status: str,
    signal: str,
    signal_reason: str,
    time_risk: str,
    conviction_rows: list[str],
) -> str:
    """Build the full HTML email body."""
    color, label = signal_to_color(signal)

    metar_str = f"{metar_f}\u00b0F" if metar_f is not None else "\u2014"
    precise_f_str = f"{precise_f:.1f}\u00b0F" if precise_f is not None else "\u2014"
    precise_c_str = f"({precise_c:.1f}\u00b0C)" if precise_c is not None else ""
    max_str = f"{running_max_f}\u00b0F" if running_max_f is not None else "\u2014"
    margin_str = f"{margin_c:+.2f}\u00b0C ({margin_status})" if margin_c is not None else "\u2014"

    conviction_html = "\n".join(conviction_rows) if conviction_rows else ""

    return f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"></head>
<body style="font-family:Consolas,monospace;background:#1a1a2e;color:#e0e0e0;padding:20px;">
<div style="max-width:600px;margin:0 auto;">

<h2 style="color:#fff;margin-bottom:4px;">SPIKE ALERT: {city} {bracket}</h2>
<p style="color:#9ca3af;margin-top:0;">Email {email_number} of {email_total} &mdash; {time_str}</p>

<div style="background:#16213e;border-radius:8px;padding:16px;margin:12px 0;">
<h3 style="color:#9ca3af;margin:0 0 8px 0;font-size:13px;">MARKET</h3>
<p style="font-size:18px;margin:0;">
{old_price}&cent; &rarr; {new_price}&cent; (+{spike_delta}&cent;) &mdash; now at {current_price}&cent;
</p>
</div>

<div style="background:#16213e;border-radius:8px;padding:16px;margin:12px 0;">
<h3 style="color:#9ca3af;margin:0 0 8px 0;font-size:13px;">EDGE ANALYSIS</h3>
<table style="width:100%;color:#e0e0e0;font-size:14px;">
<tr><td style="color:#9ca3af;">METAR (rounded):</td><td>{metar_str}</td></tr>
<tr><td style="color:#9ca3af;">Precise ({precise_source}):</td><td>{precise_f_str} {precise_c_str}</td></tr>
<tr><td style="color:#9ca3af;">Running max:</td><td>{max_str}</td></tr>
<tr><td style="color:#9ca3af;">Margin:</td><td>{margin_str}</td></tr>
</table>
</div>

<div style="background:{color};border-radius:8px;padding:20px;margin:12px 0;text-align:center;">
<span style="font-size:24px;font-weight:bold;color:#fff;">{label}</span>
<br>
<span style="font-size:13px;color:rgba(255,255,255,0.8);">Time risk: {time_risk}</span>
</div>

<p style="color:#d1d5db;font-size:13px;margin:8px 0;">{signal_reason}</p>

<div style="background:#16213e;border-radius:8px;padding:16px;margin:12px 0;">
<h3 style="color:#9ca3af;margin:0 0 8px 0;font-size:13px;">CONVICTION TREND</h3>
<table style="width:100%;color:#e0e0e0;font-size:13px;">
{conviction_html}
</table>
</div>

</div>
</body>
</html>"""


# ── Send email ───────────────────────────────────────────────────────


def send_spike_email(
    subject: str,
    html_body: str,
    gmail_address: str,
    gmail_app_password: str,
) -> None:
    """Send an HTML spike alert email to self via Gmail SMTP."""
    msg = MIMEMultipart("alternative")
    msg["From"] = gmail_address
    msg["To"] = gmail_address  # send to self
    msg["Subject"] = subject
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    logger.info("Sending spike alert: %s", subject)
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
        server.starttls()
        server.login(gmail_address, gmail_app_password)
        server.send_message(msg)
    logger.info("Spike alert sent")
```

**Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_spike.py -v`
Expected: ALL PASS

**Step 5: Commit**

```bash
git add src/kalshi_weather/spike_alerter.py tests/test_spike.py
git commit -m "Add HTML spike alert emailer with color-coded signals"
```

---

## Task 4: Market Polling & Burst Orchestration (spike_monitor.py)

**Files:**
- Modify: `src/kalshi_weather/spike_monitor.py` (add monitoring loop, burst logic)
- Test: `tests/test_spike.py` (append)

This adds the MONITORING and BURST state machine to spike_monitor.py.
The monitoring loop uses KalshiClient.get_all_events() to poll prices,
and the burst phase uses get_orderbook() + analyze_city().

**Step 1: Write the failing tests**

Append to `tests/test_spike.py`:

```python
from unittest.mock import MagicMock, patch


# ======================================================================
# Market Polling Tests
# ======================================================================


class TestExtractBracketPrices:
    """Test extracting yes_price from nested market objects."""

    def test_extract_prices_from_event(self):
        from kalshi_weather.spike_monitor import extract_bracket_prices

        event = {
            "event_ticker": "KXHIGHCHI-26FEB25",
            "title": "Highest temperature in Chicago on February 26",
            "markets": [
                {"ticker": "KXHIGHCHI-26FEB25-B40", "yes_bid": 7, "yes_ask": 12},
                {"ticker": "KXHIGHCHI-26FEB25-B45", "yes_bid": 50, "yes_ask": 55},
            ],
        }
        prices = extract_bracket_prices(event)
        assert len(prices) == 2
        assert prices["KXHIGHCHI-26FEB25-B40"] == 7
        assert prices["KXHIGHCHI-26FEB25-B45"] == 50

    def test_missing_yes_bid_skipped(self):
        from kalshi_weather.spike_monitor import extract_bracket_prices

        event = {
            "event_ticker": "KXHIGHCHI-26FEB25",
            "markets": [
                {"ticker": "KXHIGHCHI-26FEB25-B40"},
            ],
        }
        prices = extract_bracket_prices(event)
        assert len(prices) == 0


class TestIsInOperatingWindow:
    def test_inside_window(self):
        from kalshi_weather.spike_monitor import is_in_operating_window
        from kalshi_weather.spike_config import SpikeConfig

        cfg = SpikeConfig(start_hour_est=8, end_hour_est=23)
        dt = datetime(2026, 2, 25, 15, 0, tzinfo=ZoneInfo("US/Eastern"))
        assert is_in_operating_window(dt, cfg) is True

    def test_before_window(self):
        from kalshi_weather.spike_monitor import is_in_operating_window
        from kalshi_weather.spike_config import SpikeConfig

        cfg = SpikeConfig(start_hour_est=8, end_hour_est=23)
        dt = datetime(2026, 2, 25, 6, 0, tzinfo=ZoneInfo("US/Eastern"))
        assert is_in_operating_window(dt, cfg) is False

    def test_after_window(self):
        from kalshi_weather.spike_monitor import is_in_operating_window
        from kalshi_weather.spike_config import SpikeConfig

        cfg = SpikeConfig(start_hour_est=8, end_hour_est=23)
        # 23:59 is still inside (end_hour=23 means through 23:59)
        dt = datetime(2026, 2, 25, 23, 30, tzinfo=ZoneInfo("US/Eastern"))
        assert is_in_operating_window(dt, cfg) is True

    def test_midnight_outside(self):
        from kalshi_weather.spike_monitor import is_in_operating_window
        from kalshi_weather.spike_config import SpikeConfig

        cfg = SpikeConfig(start_hour_est=8, end_hour_est=23)
        dt = datetime(2026, 2, 26, 0, 5, tzinfo=ZoneInfo("US/Eastern"))
        assert is_in_operating_window(dt, cfg) is False


class TestBurstCollectData:
    """Test that burst data collection calls edge analysis correctly."""

    def test_collect_burst_data(self):
        from kalshi_weather.spike_monitor import collect_burst_data

        mock_scraper = MagicMock()
        mock_client = MagicMock()

        # Mock edge analysis
        mock_report = MagicMock()
        mock_report.running_max_f_precise = 39.9
        mock_report.running_max_c = 4.4
        mock_report.running_max_cli_f = 40
        mock_report.running_max_source = "Current Conditions"
        mock_report.metar_temp_f = 39
        mock_report.bracket = MagicMock()
        mock_report.bracket.margin_below_c = 0.23
        mock_report.bracket.margin_status.value = "COMFORTABLE"
        mock_report.signal.value = "STRONG_BUY"
        mock_report.signal_reason = "Test reason"
        mock_report.time_risk.value = "PAST_PEAK"

        with patch("kalshi_weather.spike_monitor.analyze_city", return_value=mock_report):
            data = collect_burst_data(
                city="Chicago",
                ticker="KXHIGHCHI-26FEB25-B40",
                client=mock_client,
                scraper=mock_scraper,
            )

        assert data is not None
        assert data["signal"] == "STRONG_BUY"
        assert data["precise_f"] == 39.9
```

**Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_spike.py::TestExtractBracketPrices tests/test_spike.py::TestIsInOperatingWindow tests/test_spike.py::TestBurstCollectData -v`
Expected: FAIL with import errors

**Step 3: Add functions to spike_monitor.py**

Append to `src/kalshi_weather/spike_monitor.py`:

```python
from datetime import datetime
from zoneinfo import ZoneInfo

from kalshi_weather.edge import analyze_city
from kalshi_weather.kalshi_client import KalshiClient
from kalshi_weather.nws_scraper import NWSScraper
from kalshi_weather.scanner import _classify_series, _extract_city_from_event, _is_today_event


# ── Market polling helpers ───────────────────────────────────────────


def extract_bracket_prices(event: dict) -> dict[str, int]:
    """Extract {ticker: yes_bid_cents} from an event's nested markets."""
    prices: dict[str, int] = {}
    for mkt in event.get("markets", []):
        ticker = mkt.get("ticker", "")
        yes_bid = mkt.get("yes_bid")
        if ticker and yes_bid is not None:
            prices[ticker] = int(yes_bid)
    return prices


def is_in_operating_window(now_est: datetime, config: SpikeConfig) -> bool:
    """Check if current EST time is within operating hours."""
    return config.start_hour_est <= now_est.hour <= config.end_hour_est


# ── Burst data collection ───────────────────────────────────────────


def collect_burst_data(
    city: str,
    ticker: str,
    client: KalshiClient,
    scraper: NWSScraper,
) -> Optional[dict]:
    """Collect edge analysis + orderbook data for a burst email.

    Returns a dict with all fields needed by build_spike_email_html,
    or None if analysis fails.
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
        logger.warning("Failed to fetch orderbook for %s", ticker, exc_info=True)

    bracket_obj = report.bracket
    return {
        "signal": report.signal.value,
        "signal_reason": report.signal_reason,
        "time_risk": report.time_risk.value,
        "metar_f": report.metar_temp_f,
        "precise_f": report.running_max_f_precise,
        "precise_c": report.running_max_c,
        "precise_source": report.running_max_source or "unknown",
        "running_max_f": report.running_max_cli_f,
        "margin_c": bracket_obj.margin_below_c if bracket_obj else None,
        "margin_status": bracket_obj.margin_status.value if bracket_obj else "UNKNOWN",
        "current_price": orderbook_price,
    }


# ── Main monitoring loop ────────────────────────────────────────────


def run_spike_monitor(
    client: KalshiClient,
    config: SpikeConfig | None = None,
    gmail_address: str = "",
    gmail_app_password: str = "",
) -> None:
    """Run the spike monitor state machine.

    This is the main entry point. Runs until operating window closes
    or interrupted with Ctrl+C.
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

    history = PriceHistory(max_age_seconds=config.window_seconds + 120)
    cooldowns: dict[str, float] = {}
    # Map ticker -> (city, bracket_definition, event_ticker)
    ticker_meta: dict[str, tuple[str, str, str]] = {}
    et = ZoneInfo("US/Eastern")

    logger.info(
        "Spike monitor starting (threshold=%d¢, window=%ds, poll=%ds)",
        config.spike_threshold_cents,
        config.window_seconds,
        config.poll_interval_seconds,
    )

    try:
        while True:
            now_est = datetime.now(et)

            # Check operating window
            if not is_in_operating_window(now_est, config):
                if now_est.hour > config.end_hour_est:
                    logger.info("Operating window closed. Shutting down.")
                    break
                # Before start hour — wait
                logger.info(
                    "Outside operating window (%02d:00-%02d:59 EST). Waiting...",
                    config.start_hour_est, config.end_hour_est,
                )
                _time.sleep(60)
                continue

            # ── MONITORING phase ─────────────────────────────────
            mono_now = _time.monotonic()
            today_str = now_est.strftime("%Y-%m-%d")

            # Fetch all HIGH_TEMP events with nested markets
            try:
                all_events = client.get_all_events(status="open", with_nested_markets=True)
            except Exception:
                logger.warning("Failed to fetch events", exc_info=True)
                _time.sleep(config.poll_interval_seconds)
                continue

            # Filter to today's HIGH_TEMP events and record prices
            for event in all_events:
                series_ticker = event.get("series_ticker", "")
                if _classify_series(series_ticker) != MarketType.HIGH_TEMP:
                    continue
                if not _is_today_event(event, today_str):
                    continue

                city = _extract_city_from_event(event)
                event_ticker = event.get("event_ticker", "")
                prices = extract_bracket_prices(event)

                for ticker, price in prices.items():
                    history.record(ticker, price, mono_now)
                    bracket_def = ""
                    for mkt in event.get("markets", []):
                        if mkt.get("ticker") == ticker:
                            bracket_def = mkt.get("yes_sub_title", mkt.get("title", ""))
                            break
                    ticker_meta[ticker] = (city, bracket_def, event_ticker)

            history.prune_all(mono_now)

            # Check for spike
            spike = detect_spike(history, config, mono_now, cooldowns)

            if spike is None:
                logger.debug(
                    "No spike detected (%d tickers tracked)",
                    len(ticker_meta),
                )
                _time.sleep(config.poll_interval_seconds)
                continue

            # ── BURST phase ──────────────────────────────────────
            city, bracket_def, event_ticker = ticker_meta.get(
                spike.ticker, ("Unknown", "Unknown", "")
            )
            logger.info(
                "SPIKE DETECTED: %s %s — %d¢ → %d¢ (+%d¢)",
                city, bracket_def, spike.old_price, spike.new_price, spike.delta,
            )

            # Record cooldown
            cooldowns[spike.ticker] = _time.monotonic()

            conviction_history: list[dict] = []

            with NWSScraper() as scraper:
                for burst_idx in range(1, config.burst_count + 1):
                    burst_time = datetime.now(et)
                    time_str = burst_time.strftime("%-I:%M %p EST").replace(" 0", " ")

                    # Collect data
                    data = collect_burst_data(city, spike.ticker, client, scraper)

                    if data is not None:
                        conviction_history.append({
                            "time_str": time_str,
                            "signal": data["signal"],
                            "temp_f": data["precise_f"],
                            "market_price": data.get("current_price") or spike.new_price,
                        })
                    else:
                        conviction_history.append({
                            "time_str": time_str,
                            "signal": "NO_EDGE",
                            "temp_f": None,
                            "market_price": None,
                        })

                    # Build conviction rows
                    rows: list[str] = []
                    for i, entry in enumerate(conviction_history, 1):
                        rows.append(build_conviction_row(
                            index=i,
                            total=config.burst_count,
                            time_str=entry["time_str"],
                            signal=entry["signal"],
                            temp_f=entry["temp_f"],
                            market_price=entry["market_price"],
                            is_current=(i == burst_idx),
                        ))
                    # Add pending rows
                    for j in range(burst_idx + 1, config.burst_count + 1):
                        future_time = burst_time.strftime("%-I:%M %p EST").replace(" 0", " ")
                        rows.append(build_conviction_row(
                            index=j,
                            total=config.burst_count,
                            time_str=future_time,
                            signal=None,
                            temp_f=None,
                            market_price=None,
                            is_current=False,
                        ))

                    # Build and send email
                    d = data or {}
                    html = build_spike_email_html(
                        city=city,
                        bracket=bracket_def,
                        email_number=burst_idx,
                        email_total=config.burst_count,
                        time_str=time_str,
                        old_price=spike.old_price,
                        new_price=spike.new_price,
                        current_price=d.get("current_price") or spike.new_price,
                        spike_delta=spike.delta,
                        metar_f=d.get("metar_f"),
                        precise_f=d.get("precise_f"),
                        precise_c=d.get("precise_c"),
                        precise_source=d.get("precise_source", ""),
                        running_max_f=d.get("running_max_f"),
                        margin_c=d.get("margin_c"),
                        margin_status=d.get("margin_status", "UNKNOWN"),
                        signal=d.get("signal", "NO_EDGE"),
                        signal_reason=d.get("signal_reason", "Analysis unavailable."),
                        time_risk=d.get("time_risk", "UNKNOWN"),
                        conviction_rows=rows,
                    )

                    subject = f"SPIKE: {city} {bracket_def} [{burst_idx}/{config.burst_count}]"
                    if gmail_address and gmail_app_password:
                        try:
                            send_spike_email(subject, html, gmail_address, gmail_app_password)
                        except Exception:
                            logger.exception("Failed to send spike email %d/%d", burst_idx, config.burst_count)
                    else:
                        logger.warning("Email not configured — spike alert not sent")

                    logger.info(
                        "Burst email %d/%d sent: %s %s — signal=%s",
                        burst_idx, config.burst_count, city, bracket_def,
                        d.get("signal", "N/A"),
                    )

                    if burst_idx < config.burst_count:
                        _time.sleep(config.burst_interval_seconds)

            logger.info("Burst complete. Returning to monitoring.")

    except KeyboardInterrupt:
        logger.info("Spike monitor stopped by user.")
```

**Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_spike.py -v`
Expected: ALL PASS

**Step 5: Commit**

```bash
git add src/kalshi_weather/spike_monitor.py tests/test_spike.py
git commit -m "Add monitoring loop, burst orchestration, and market polling"
```

---

## Task 5: CLI Integration (__main__.py)

**Files:**
- Modify: `src/kalshi_weather/__main__.py:183-215`
- Test: `tests/test_spike.py` (append)

Add the `spike` subcommand to argparse with all configurable flags.

**Step 1: Write the failing test**

Append to `tests/test_spike.py`:

```python
# ======================================================================
# CLI Integration Tests
# ======================================================================


class TestSpikeSubcommand:
    def test_spike_args_parsed(self):
        """Verify argparse recognizes the spike subcommand."""
        from kalshi_weather.__main__ import main

        # Should fail on missing credentials, not on arg parsing
        with patch.dict("os.environ", {}, clear=True):
            result = main(["spike", "--threshold", "25", "--interval", "20"])
        assert result == 1  # fails on missing creds, not argparse

    def test_spike_default_runs_scan(self):
        """No args still defaults to scan."""
        from kalshi_weather.__main__ import main

        with patch.dict("os.environ", {}, clear=True):
            result = main([])
        assert result == 1  # missing creds for scan
```

**Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_spike.py::TestSpikeSubcommand -v`
Expected: FAIL (spike not recognized)

**Step 3: Modify __main__.py**

Add after the edge subcommand block (after line ~203), before `args = parser.parse_args(argv)`:

```python
    # spike subcommand
    spike_parser = subparsers.add_parser(
        "spike", help="Monitor markets for price spikes and send alerts"
    )
    spike_parser.add_argument(
        "--threshold", type=int, default=20,
        help="Spike threshold in cents (default: 20)",
    )
    spike_parser.add_argument(
        "--window", type=int, default=360,
        help="Lookback window in seconds (default: 360)",
    )
    spike_parser.add_argument(
        "--interval", type=int, default=30,
        help="Polling interval in seconds (default: 30)",
    )
    spike_parser.add_argument(
        "--burst-count", type=int, default=5,
        help="Number of emails per burst (default: 5)",
    )
    spike_parser.add_argument(
        "--burst-interval", type=int, default=60,
        help="Seconds between burst emails (default: 60)",
    )
    spike_parser.add_argument(
        "--start-hour", type=int, default=8,
        help="Start monitoring hour EST (default: 8)",
    )
    spike_parser.add_argument(
        "--end-hour", type=int, default=23,
        help="End monitoring hour EST (default: 23)",
    )
```

Add the `_run_spike` function and dispatch:

```python
def _run_spike(args) -> int:
    """Run the spike monitor."""
    from kalshi_weather.spike_config import SpikeConfig
    from kalshi_weather.spike_monitor import run_spike_monitor

    api_key_id = os.environ.get("KALSHI_API_KEY_ID", "")
    private_key_path = os.environ.get("KALSHI_PRIVATE_KEY_PATH", "")
    gmail_address = os.environ.get("GMAIL_ADDRESS", "")
    gmail_app_password = os.environ.get("GMAIL_APP_PASSWORD", "")

    if not api_key_id or not private_key_path:
        logger.error(
            "Missing Kalshi credentials. Set KALSHI_API_KEY_ID and "
            "KALSHI_PRIVATE_KEY_PATH environment variables."
        )
        return 1

    from kalshi_weather.kalshi_client import KalshiClient

    config = SpikeConfig(
        spike_threshold_cents=args.threshold,
        window_seconds=args.window,
        poll_interval_seconds=args.interval,
        burst_count=args.burst_count,
        burst_interval_seconds=args.burst_interval,
        start_hour_est=args.start_hour,
        end_hour_est=args.end_hour,
    )

    client = KalshiClient(
        api_key_id=api_key_id,
        private_key_path=private_key_path,
    )

    try:
        run_spike_monitor(
            client=client,
            config=config,
            gmail_address=gmail_address,
            gmail_app_password=gmail_app_password,
        )
    except Exception:
        logger.exception("Spike monitor failed")
        return 1
    finally:
        client.close()

    return 0
```

In the dispatch block, add:

```python
    elif args.command == "spike":
        return _run_spike(args)
```

**Step 4: Run full test suite**

Run: `uv run pytest tests/ -q`
Expected: ALL PASS (no regressions)

**Step 5: Commit**

```bash
git add src/kalshi_weather/__main__.py tests/test_spike.py
git commit -m "Add spike subcommand with configurable thresholds"
```

---

## Task 6: Lint, Full Test Suite, Smoke Test

**Step 1: Run lint on all new files**

Run: `uv run ruff check src/kalshi_weather/spike_config.py src/kalshi_weather/spike_monitor.py src/kalshi_weather/spike_alerter.py`
Expected: All checks passed

**Step 2: Run full test suite**

Run: `uv run pytest tests/ -v`
Expected: ALL PASS (471 existing + ~30 new)

**Step 3: Verify CLI help**

Run: `uv run python -m kalshi_weather spike --help`
Expected: Shows all flags with defaults

**Step 4: Final commit and push**

```bash
git push origin main
```
