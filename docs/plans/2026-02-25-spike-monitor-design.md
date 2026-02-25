# Spike Monitor — Design Document

## Problem

Kalshi temperature markets can spike dramatically when precise NWS data
reveals a temperature that differs from what hourly METARs show. Example:
Chicago's 40-41F bracket sat at 7c for hours, then jumped to 32c in 5
minutes when the real temperature crossed a rounding boundary. By the time
a trader notices, the edge has narrowed.

## Solution

A dormant monitoring bot that watches all HIGH_TEMP Kalshi markets for
sudden price spikes, then sends a 5-email burst with minute-by-minute
edge analysis so the trader can watch conviction build or fade in real
time before committing.

## Architecture: Single-Loop State Machine

```
MONITORING (every 30s)
  Fetch all HIGH_TEMP events (event-level prices)
  Record price snapshot per bracket
  Compare against 6-minute-old snapshot
  If any bracket moved >= 20c --> BURST

BURST (5 iterations, 60s apart)
  Fetch orderbook for spiking bracket
  Run full edge analysis on that city
  Send HTML email with color-coded signal + conviction trend
  After email 5/5 --> back to MONITORING
```

## Decisions

| Decision            | Choice                                           |
|---------------------|--------------------------------------------------|
| Architecture        | Single-loop state machine (MONITORING / BURST)   |
| Spike threshold     | >= 20c absolute in 6-minute window (configurable) |
| Polling cadence     | 30s, using event-level prices (not orderbooks)    |
| Burst behavior      | 5 emails, 1/minute, with orderbook + edge analysis|
| Multi-city          | Pause scanning during burst, focus on one city    |
| Markets             | HIGH_TEMP only                                    |
| Operating hours     | 08:00 - 23:59 EST                                 |
| Email format        | HTML, inline CSS, color-coded signal banner        |
| Cooldown            | 10 min after burst before re-triggering same bracket |
| Email recipient     | Self (tylergwlk@gmail.com to tylergwlk@gmail.com) |
| API strategy        | Event-level prices for monitoring, orderbooks for burst |

## Configuration

```python
@dataclass
class SpikeConfig:
    spike_threshold_cents: int = 20
    window_seconds: int = 360        # 6 minutes
    poll_interval_seconds: int = 30
    burst_count: int = 5
    burst_interval_seconds: int = 60
    start_hour_est: int = 8
    end_hour_est: int = 23
```

All configurable via CLI flags:
  --threshold, --window, --interval, --burst-count,
  --burst-interval, --start-hour, --end-hour

## Spike Detection Algorithm

1. Each 30s cycle: snapshot all HIGH_TEMP bracket prices
2. Per ticker: rolling deque of (timestamp, price_cents), ~15 entries
3. Compare current price against oldest entry within 6-min window
4. If delta >= 20c --> spike detected
5. Pick largest delta if multiple brackets spike simultaneously
6. Cooldown: same bracket cannot re-trigger for 10 min after burst ends

## Burst Phase

1. Extract city from spiking event
2. Fetch orderbook for the specific bracket (precise data)
3. Run analyze_city() from edge.py (all 4 NWS sources)
4. Build HTML email with:
   - Market price movement summary
   - Full edge analysis (METAR, T-group, current conditions, CLI)
   - Color-coded signal banner (green/yellow/red)
   - Conviction trend table showing all emails so far
5. Send via Gmail SMTP (to self)
6. Repeat 4 more times at 60s intervals
7. Return to MONITORING

## Signal Color Mapping

| Signal     | Color              | Hex     |
|------------|--------------------|---------|
| STRONG_BUY | Green              | #22c55e |
| BUY        | Green              | #22c55e |
| HOLD       | Yellow             | #eab308 |
| CAUTION    | Orange/Red         | #ef4444 |
| NO_EDGE    | Red                | #ef4444 |

## New Files

| File                          | Purpose                                    |
|-------------------------------|--------------------------------------------|
| spike_monitor.py              | State machine, spike detection, burst loop  |
| spike_alerter.py              | HTML email builder with color-coded signals |
| spike_config.py               | SpikeConfig dataclass                       |
| tests/test_spike.py           | Unit tests                                  |
| __main__.py (modified)        | Add 'spike' subcommand                      |

## Existing Code Reused

- KalshiClient.get_all_events() -- monitoring phase
- KalshiClient.get_orderbook() -- burst phase
- scanner._extract_city_from_event(), _classify_series() -- market identification
- NWSScraper + edge.analyze_city() -- edge analysis during burst
- emailer.py SMTP pattern -- Gmail connection

## CLI Usage

```
python -m kalshi_weather spike
python -m kalshi_weather spike --threshold 25 --window 300 --interval 20
```

Requires env vars: KALSHI_API_KEY_ID, KALSHI_PRIVATE_KEY_PATH,
GMAIL_ADDRESS, GMAIL_APP_PASSWORD
