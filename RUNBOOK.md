# Kalshi Weather Scanner — Operational Runbook

## Quick Start

### Prerequisites

- Python 3.11+
- Kalshi API key (RSA key pair)
- Internet access (Kalshi API + NWS api.weather.gov)

### Installation

```bash
cd Weather
pip install -e ".[dev]"
```

### Environment Setup

Create a `.env` file (NOT committed to git):

```
KALSHI_API_KEY_ID=your-api-key-id
KALSHI_PRIVATE_KEY_PATH=/path/to/your/private_key.pem

# Email delivery (optional)
GMAIL_ADDRESS=your-gmail@gmail.com
GMAIL_APP_PASSWORD=xxxx-xxxx-xxxx-xxxx
EMAIL_TO=recipient@example.com
```

**Gmail App Password setup:**
1. Enable 2-Factor Authentication on your Google account
2. Go to https://myaccount.google.com/apppasswords
3. Generate an App Password for "Mail"
4. Use the 16-character password as `GMAIL_APP_PASSWORD`

### First Run

```bash
python -m kalshi_weather
```

This will:
1. Scan all open Kalshi temperature markets
2. Fetch NWS weather data for each city
3. Run the full pipeline (model → account → plan → risk → merge → bucket → rank)
4. Write artifacts to `output/{date}/`

---

## Daily Operations

### Schedule

The scanner runs at **7:00, 8:00, and 9:00 AM ET** each day.

Each run:
- Scans Kalshi for today's HIGH/LOW temperature markets
- Fetches current NWS observations and forecasts
- Computes probabilities, EV, and risk
- Outputs recommendations to `output/{date}/`
- Compares with the prior run and applies stability rules

### Scheduling with cron (Linux/macOS)

```cron
0 7,8,9 * * * cd /path/to/Weather && python -m kalshi_weather >> /var/log/kalshi-weather.log 2>&1
```

### Scheduling with Task Scheduler (Windows)

**Option A: Use the batch wrapper (recommended)**

1. Open Task Scheduler (`taskschd.msc`)
2. Click **Create Task** (not "Basic Task" — we need the repeat option)
3. **General** tab:
   - Name: `Kalshi Weather Scanner`
   - Check "Run whether user is logged on or not"
4. **Triggers** tab → New:
   - Begin: Daily
   - Start at: `7:00 AM`
   - Check "Repeat task every **1 hour** for a duration of **2 hours**"
   - Enabled: checked
5. **Actions** tab → New:
   - Action: Start a Program
   - Program/script: `C:\Users\Tyler\Desktop\Weather\run_scanner.bat`
   - Start in: `C:\Users\Tyler\Desktop\Weather`
6. **Settings** tab:
   - Check "Allow task to be run on demand"
   - Check "Stop the task if it runs longer than **30 minutes**"
7. Click OK, enter your Windows password when prompted

**Option B: Direct python invocation**

1. Action: Start Program
   - Program: `python`
   - Arguments: `-m kalshi_weather`
   - Start in: `C:\Users\Tyler\Desktop\Weather`

**Verify it works:**
- Right-click the task → "Run"
- Check `logs/` directory for output
- Check your email for the report

### Output Artifacts

Each run produces two files in `output/{date}/`:

| File | Contents |
|------|----------|
| `REPORT_{HH}_{MM}.md` | Human-readable markdown report with picks, risks, manual order steps |
| `DAILY_SLATE_{HH}_{MM}.json` | Machine-readable JSON with all candidates, buckets, and metadata |

### Reading the Report

The report contains:
- **PRIMARY picks**: Best opportunities (NO ask 90-93c, room >= 2c, positive EV)
- **TIGHT picks**: Marginal opportunities (90-93c but room < 2c)
- **NEAR-MISS**: Watchlist (ask 88-89c or 94-95c)
- **REJECTED**: Filtered out with reasons

For each pick, review:
1. The **bracket definition** (e.g., "50°F or above")
2. The **EV estimate** (must be positive)
3. The **knife-edge risk** (HIGH = close to boundary)
4. The **manual order steps** (follow exactly)
5. The **cancel/replace rules** (when to exit)

### Safety Rules

- **NEVER execute trades automatically** — this system is recommendation-only
- **ALWAYS follow the manual order steps** in the report
- **ALWAYS check cancel/replace rules** after placing orders
- **Maximum bankroll: $42** — never exceed this

---

## Configuration

### Config File

All configuration lives in `src/kalshi_weather/config.py` as frozen dataclasses.

| Setting | Default | Description |
|---------|---------|-------------|
| `bankroll.total_usd` | 42.00 | Maximum bankroll |
| `price_window.primary_low/high` | 90/93 | PRIMARY bucket NO ask range |
| `price_window.scan_low/high` | 88/95 | Full scan range |
| `spread.max_spread_cents` | 6 | Hard reject spread threshold |
| `spread.min_bid_room_primary` | 2 | Minimum room for PRIMARY |
| `correlation.max_picks_per_correlation_group` | 3 | Regional diversification cap |
| `correlation.max_picks_per_metro_cluster` | 2 | Metro area cap |
| `fees.taker_rate` | 0.07 | Kalshi taker fee rate |
| `fees.maker_rate` | 0.0175 | Kalshi maker fee rate |
| `rate_limit.kalshi_requests_per_second` | 5.0 | Kalshi API rate limit |
| `rate_limit.nws_requests_per_second` | 5.0 | NWS API rate limit |
| `rate_limit.retry_max_attempts` | 3 | Max retries on transient failure |

### Logging

Enable verbose logging:

```bash
LOGLEVEL=DEBUG python -m kalshi_weather.orchestrator
```

Log levels:
- `DEBUG`: All API requests, model calculations, bucket decisions
- `INFO`: Run start/end, artifact paths, summary stats
- `WARNING`: API failures (with retry), missing data, fallback behavior
- `ERROR`: Unrecoverable failures

---

## Failure Modes

### Kalshi API Unavailable

**Symptoms:** `httpx.ConnectError` or `httpx.TimeoutException` in logs

**Impact:** No market data — pipeline cannot run

**Recovery:**
1. Check https://status.kalshi.com for outages
2. The scanner will auto-retry up to 3 times with exponential backoff
3. If persistent, wait and re-run manually

### NWS API Unavailable

**Symptoms:** `Failed to fetch obs/forecast for XXXX` warnings in logs

**Impact:** Model falls back to market-implied probabilities (less accurate)

**Recovery:**
1. Check https://api.weather.gov status
2. Auto-retry handles transient failures
3. Pipeline continues without weather data — picks still generated but with higher uncertainty

### Missing Weather Data for a City

**Symptoms:** `model.method = "MARKET_IMPLIED"` in output

**Impact:** Probability estimates rely on market price rather than weather forecast

**Recovery:** No action needed — the model degrades gracefully. Picks with `MARKET_IMPLIED` method have higher uncertainty flags.

### Rate Limited (429)

**Symptoms:** `429` status codes in logs, retry backoff messages

**Impact:** Requests are delayed but will succeed after backoff

**Recovery:** Automatic — the rate limiter and retry logic handle this. If persistent, reduce `rate_limit.kalshi_requests_per_second` in config.

### Stale/Corrupt Artifact Files

**Symptoms:** `Failed to load prior slate` warning in logs

**Impact:** Delta comparison unavailable for this run — "no prior run" in report

**Recovery:** Safe to ignore. The pipeline runs independently each time. Old artifacts can be deleted without impact.

### No Markets Found

**Symptoms:** `events_scanned: 0` in output

**Impact:** Empty slate — no picks generated

**Recovery:** Normal on weekends, holidays, or off-season. Temperature markets are typically available during weather-active periods.

---

## Troubleshooting

### "Path not allowed" PermissionError

**Cause:** Code attempted to access a Kalshi API endpoint outside the read-only allowlist.

**Fix:** This should never happen in normal operation. If it does, it indicates a code bug — do NOT modify the allowlist.

### "Negative EV" on all picks

**Cause:** Market prices don't offer edge after fees.

**Fix:** Normal market condition. The scanner correctly identifies these as no-trade. Wait for better pricing.

### Tests failing after code changes

```bash
python -m pytest tests/ -v
python -m ruff check src/ tests/
```

### Checking rate limit behavior

```bash
LOGLEVEL=DEBUG python -m kalshi_weather.orchestrator 2>&1 | grep -i "retry\|rate\|backoff"
```

### Verifying no-execution guardrails

```bash
python -m pytest tests/test_phase12.py::TestNoExecutionGuardrails -v
```

---

## Architecture Overview

```
Scanner (Kalshi API) → CandidateRaw
                            ↓
Rules (NWS station mapping) → SettlementSpec
Weather API (NWS forecasts) → StationForecast
                            ↓
Modeler (probability model) → ModelOutput
Accountant (fee-aware EV)   → Accounting
Planner (execution plan)    → ExecutionPlan
Risk (portfolio manager)    → RiskRecommendation
                            ↓
Team Lead (merge + bucket)  → UnifiedCandidate[]
                            ↓
Orchestrator (stability + delta) → DailySlate
                            ↓
Output (REPORT.md + JSON)   → output/{date}/
```
