# Kalshi Weather Temperature "Unlikely NO" Scanner — Project Roadmap

> **Living document** — Updated as features are implemented and changes are made.
> **Language:** Python
> **Status:** Complete — All 12 Phases Done

---

## Project Overview

A daily scanner for Kalshi U.S. weather temperature markets (Daily High / Daily Low) that identifies "unlikely NO" opportunities where NO is priced ~90-93%. Runs at 7/8/9 AM ET, outputs recommendations only (no execution). Bankroll: $52.

---

## Phase 1 — Project Foundation & Configuration
**Status:** COMPLETE

| # | Task | Status | Notes |
|---|------|--------|-------|
| 1 | Initialize project structure (dirs, `pyproject.toml`, linting, Git) | Done | `src/kalshi_weather/`, `tests/`, `output/`, `data/`; Git init; Python 3.11+; ruff + pytest |
| 2 | Define configuration module — bankroll ($52), fee schedule, price windows ([90,93] primary, [88,95] scan), run times, correlation groups | Done | `src/kalshi_weather/config.py` — frozen dataclasses, `DEFAULT_CONFIG` singleton |
| 3 | Define shared data schemas — Pydantic models for all 6 output schemas (`SETTLEMENT_SPECS`, `CANDIDATES_RAW`, `MODEL_OUTPUTS`, `ACCOUNTING`, `EXECUTION_PLANS`, `RISK_RECOMMENDATIONS`, `NO_TRADE_LIST`) plus unified merged object + `DailySlate` | Done | `src/kalshi_weather/schemas.py` — 17 models/enums |
| 4 | Set up artifact output layer — writers for `REPORT.md` (Jinja2 templating) and `DAILY_SLATE.json` | Done | `src/kalshi_weather/artifacts.py` — `write_report_md()`, `write_daily_slate_json()` |

---

## Phase 2 — Kalshi API Integration (Scanner)
**Status:** COMPLETE

| # | Task | Status | Notes |
|---|------|--------|-------|
| 5 | Kalshi API client — authenticated read-only client (no order placement enforced at code level) | Done | `kalshi_client.py` — RSA-PSS auth, path allowlist, no POST/PUT/DELETE methods. Uses httpx. |
| 6 | Market discovery — query all TODAY U.S. weather temperature events (HIGH_TEMP / LOW_TEMP) | Done | `scanner.py` — discovers KXHIGH*/KXLOW* series, filters events by `strike_date` or close time |
| 7 | Bracket enumeration — for each event, fetch every bracket sub-market | Done | Iterates `event.markets[]` from nested-market API response |
| 8 | Orderbook snapshot extraction — compute `best_yes_bid`, `best_no_bid`, `implied_best_no_ask` (100 - best_yes_bid), `implied_best_yes_ask` (100 - best_no_bid), `bid_room`, top-3 depth | Done | `_parse_orderbook()` — handles empty books, ascending sort, top-3 highest-first |
| 9 | Market status check — detect closed/halted/resolved markets (hard reject) | Done | `_market_is_tradable()` — only "active"/"open" pass |
| 10 | Candidate filtering — pass through brackets with `implied_best_no_ask` in [88, 95] | Done | Configurable via `config.price_window.scan_low/scan_high` |

---

## Phase 3 — Rules & Settlement Specialist
**Status:** COMPLETE

| # | Task | Status | Notes |
|---|------|--------|-------|
| 11 | City to NWS station mapping — Kalshi city names to NWS CLI station IDs, CLI URLs, `issuedby` codes | Done | `rules.py` — 26 stations mapped with ICAO codes, CLI issuedby codes, timezones, aliases; `lookup_station()` with case-insensitive + substring matching |
| 12 | Settlement day-window logic — CLI day definition accounting for local standard time vs DST | Done | `get_cli_day_window()` — always uses LST (not LDT), handles Phoenix no-DST correctly |
| 13 | Mapping confidence scoring — HIGH/MED/LOW per city; hard reject != HIGH | Done | 25 cities HIGH confidence, 1 MED (LaGuardia); unmapped cities auto-LOW |
| 14 | Special risk flags — station outages, unusual CLI formatting, known edge cases | Done | Phoenix DST exception flagged; per-station notes (airport name, alt stations) |

---

## Phase 4 — Probability Modeler
**Status:** COMPLETE

| # | Task | Status | Notes |
|---|------|--------|-------|
| 15 | Weather data ingestion — METAR/NWS API for current obs, today's observed high/low, forecast | Done | `weather_api.py` — NWS api.weather.gov client: latest obs, hourly gridpoint forecast; station→coords→/points→forecastHourly pipeline |
| 16 | Time computation module — `local_time_at_station`, `hours_remaining_until_cli_day_close`, `sunrise_estimate_local`, `typical_peak_time_estimate_local`, `hours_remaining_in_meaningful_volatility_window` | Done | `modeler.py` — astral sunrise, 3PM peak default, CLI day window integration, vol-window calc |
| 17 | LOW-temp lock-in gate — `P_new_lower_low_after_now`; hard reject if past sunrise+2h AND P < 0.05; set `lock_in_flag_if_low` | Done | `model_candidate()` — time-decay + room-based P estimate; LOCKING/NOT_LOCKED flag |
| 18 | HIGH-temp lock-in gate — `P_new_higher_high_after_now`; hard reject if past peak+2h AND P < 0.05; set `high_lock_in_flag` | Done | Same model, mirrored for highs |
| 19 | Bracket probability model — estimate `p_yes`/`p_no` using forecast distribution vs bracket boundaries | Done | Normal CDF model with time-adaptive sigma (3→2→1 as vol window shrinks); parses "X°F or above/below/between" brackets |
| 20 | Knife-edge risk scoring — penalize brackets where distribution mass near boundary is high | Done | Distance-based: ≤1°F=HIGH, ≤sigma=MED, >sigma=LOW |

---

## Phase 5 — Fees & EV Accountant
**Status:** COMPLETE

| # | Task | Status | Notes |
|---|------|--------|-------|
| 21 | Fee model — implement Kalshi fee schedule (per-contract) | Done | `accountant.py` — taker fee: `ceil(0.07 * C * P * (1-P) * 100)`, maker fee: `ceil(0.0175 * C * P * (1-P) * 100)`; updated config to match Feb 2026 schedule |
| 22 | EV computation — `ev_net_est_cents_at_recommended_limit`, `max_buy_price_no_cents`, `edge_vs_implied_pct` | Done | `compute_ev_no()`, `compute_max_buy_price_no()`, `compute_edge_vs_implied()`; uses maker fees for limit orders |
| 23 | No-trade gate — hard reject if `EV_net <= 0` at recommended limit; record reason | Done | `compute_accounting()` sets `no_trade_reason_if_any` with explicit message when EV ≤ 0 |

---

## Phase 6 — Microstructure & Manual Execution Planner
**Status:** COMPLETE

| # | Task | Status | Notes |
|---|------|--------|-------|
| 24 | Liquidity assessment — top-of-book + top-3 depth; reject/demote near-zero liquidity | Done | `planner.py` — OK/THIN/REJECT verdicts; REJECT if top-of-book=0 or top3<5; THIN if top3<20 |
| 25 | Spread sanity check — hard reject if spread > 6c (unless justified wide-spread exception) | Done | `assess_spread()` — WIDE_EXCEPTION if depth strong AND edge>3%; missing data = REJECT |
| 26 | Bid improvement logic — recommended limit: bid_room >= 2 → 2-6c improvement; bid_room < 2 → 1-3c (TIGHT); > 6c → LOW FILL PROBABILITY | Done | `compute_recommended_limit()` — midpoint targeting, TIGHT labeling, fill probability notes |
| 27 | Manual order steps generation — human-readable step-by-step placement instructions | Done | `generate_manual_steps()` — 8-step checklist with contract count calc |
| 28 | Cancel/replace rules — conditions for human to cancel or revise orders | Done | `generate_cancel_replace_rules()` — cancel thresholds, adjust +1c rule, no-chase cap |

---

## Phase 7 — Risk & Portfolio Manager
**Status:** COMPLETE

| # | Task | Status | Notes |
|---|------|--------|-------|
| 29 | Correlation group definitions — cities to region/weather-regime groups | Done | `risk.py` — 7 regional groups (Northeast, Mid-Atlantic, Southeast, Great Lakes, South Central, Mountain, Pacific), 7 metro clusters, reverse lookup with safe substring matching |
| 30 | Correlation caps enforcement — max 3 per correlation group, max 2 per metro/station cluster | Done | `enforce_correlation_caps()` — priority sorting by rank_score, NoTradeEntry for rejected picks |
| 31 | Stake allocation — distribute $52 bankroll across picks with `max_loss_usd` | Done | `allocate_stakes()` — equal-weight with risk_multiplier adjustments, clamped to bankroll |
| 32 | Risk flag aggregation — combine uncertainty, knife-edge, liquidity, time-remaining | Done | `compute_risk_multiplier()` (0.1-1.0), `aggregate_risk_flags()`, `build_risk_recommendation()` — full RiskRecommendation output |

---

## Phase 8 — Team Lead Merge & Bucket Logic
**Status:** COMPLETE

| # | Task | Status | Notes |
|---|------|--------|-------|
| 33 | Unified object merger — combine all 6 module outputs into single object per candidate | Done | `team_lead.py` — `merge_candidate()` combines CandidateRaw + 5 optional module outputs into UnifiedCandidate |
| 34 | Hard reject pipeline — apply all hard reject gates in sequence | Done | `apply_hard_rejects()` — 6 gates: mapping confidence, missing ask, spread, EV, LOW lock-in, HIGH lock-in |
| 35 | Bucket classifier — PRIMARY / TIGHT / NEAR-MISS / REJECTED with explicit reasons | Done | `classify_bucket()` — PRIMARY [90,93]+room≥2, TIGHT [90,93]+room<2, NEAR-MISS [88,89]∪[94,95], REJECTED otherwise |
| 36 | Ranking engine — rank by: EV > uncertainty > knife-edge > liquidity > diversification > time remaining | Done | `rank_candidates()` — 5-key sort tuple, assigns rank numbers |
| 37 | Pick count enforcement — up to 10 PRIMARY; supplement with TIGHT if < 10; NEAR-MISS = watchlist only | Done | `enforce_pick_counts()` — excess PRIMARY demoted to TIGHT with reason; `run_bucket_pipeline()` full pipeline |

---

## Phase 9 — Output Generation
**Status:** COMPLETE

| # | Task | Status | Notes |
|---|------|--------|-------|
| 38 | REPORT.md generator — full markdown report with all sections | Done | `output.py` — `build_daily_slate()` assembles DailySlate from pipeline output; `generate_outputs()` writes both artifacts |
| 39 | DAILY_SLATE.json generator — structured JSON with all picks and scan stats | Done | JSON roundtrip verified via `load_prior_slate()` for delta comparison |
| 40 | Delta computation — compare current run to prior (7>8>9 AM), stability rule (no thrashing unless >= 2c move or EV flip) | Done | `compute_delta()` detects NEW/REMOVED candidates, price moves, EV flips, bucket/rank changes; `should_suppress_change()` enforces stability thresholds |

---

## Phase 10 — Multi-Run Orchestration & Scheduling
**Status:** COMPLETE

| # | Task | Status | Notes |
|---|------|--------|-------|
| 41 | Scheduler/cron setup — trigger at 7:00, 8:00, 9:00 AM ET | Done | `orchestrator.py` — `get_current_run_time_et()`, `get_target_date()`, `is_scheduled_run_time()` with pytz ET conversion; `run_pipeline()` full orchestration |
| 42 | Artifact persistence — store each run's outputs with timestamps for delta comparison | Done | `output/{date}/` directory structure; `save_run_artifacts()`, `find_prior_slate()` picks latest prior by filename sort; `load_prior_slate()` JSON roundtrip |
| 43 | Multi-run stability enforcement — suppress bucket changes unless thresholds met | Done | `apply_stability_rules()` reverts bucket changes when ≥2c move, EV flip, or confidence change thresholds not met; integrated into `run_pipeline()` |

---

## Phase 11 — Testing & Validation
**Status:** COMPLETE

| # | Task | Status | Notes |
|---|------|--------|-------|
| 44 | Unit tests — fee math, EV calc, lock-in gates, bucket logic, spread checks | Done | `test_phase11_unit.py` — 38 tests: fee math edges (8), EV boundaries (7), lock-in thresholds (5), knife-edge boundaries (5), risk multiplier (4), spread edges (4), liquidity edges (3), recommended limit edges (2) |
| 45 | Integration test with mock data — full pipeline with synthetic orderbook + weather data | Done | `test_phase11_integration.py` — 11 tests: full 6-module chain (6), end-to-end pipeline with artifact persistence and delta (5) |
| 46 | Backtesting harness — replay historical market/weather data | Done | `backtest.py` — `BacktestResult`/`BacktestSummary` dataclasses, `backtest_from_slates()` loads saved JSON, `backtest_candidates()` runs through pipeline; `test_phase11_backtest.py` — 10 tests |
| 47 | Edge case coverage — DST transitions, missing orderbooks, station outages, empty brackets | Done | `test_phase11_edge_cases.py` — 22 tests: DST transitions (5), missing orderbook data (4), missing weather data (3), station outages (2), correlation cap edges (4), None submodule outputs (3), None ask bucket (1) |

---

## Phase 12 — Hardening & Ops
**Status:** COMPLETE

| # | Task | Status | Notes |
|---|------|--------|-------|
| 48 | No-execution guardrail audit — verify no order placement code exists at any layer | Done | 5 automated tests: no POST/PUT/DELETE/PATCH methods, no write HTTP calls, no order endpoints, path allowlist enforced; all pass |
| 49 | Logging & error handling — structured logs per module with failure mode docs | Done | All 14 source modules now have `logging.getLogger(__name__)`; automated tests verify no `print()` statements and no bare `except:` clauses |
| 50 | Rate limiting — respect Kalshi and NWS API rate limits | Done | `rate_limiter.py` — token-bucket `RateLimiter`, exponential backoff with jitter, `request_with_retry()` for transient errors (429/5xx/timeout/connect); `RateLimitConfig` in config (5 req/s default, 3 retries); integrated into both `KalshiClient` and `WeatherAPI` |
| 51 | Documentation — operational runbook for daily use | Done | `RUNBOOK.md` — Quick Start, Daily Operations (cron/Task Scheduler), Configuration reference, Failure Modes (6 scenarios with recovery), Troubleshooting guide, Architecture overview |

---

## Changelog

| Date | Change | Phase | Details |
|------|--------|-------|---------|
| 2026-02-13 | Phase 12 complete | 12 | Hardening & ops: 5 automated guardrail audit tests (no write methods, no order endpoints, path allowlist), logging added to all 14 modules with AST-based verification (no print, no bare except), `rate_limiter.py` with token-bucket + exponential backoff + jitter + retry for transient errors, `RateLimitConfig` integrated into both API clients, `RUNBOOK.md` operational runbook; 44 new tests (377 total) |
| 2026-02-12 | Phase 11 complete | 11 | Testing & validation: `test_phase11_unit.py` (38 tests — fee math, EV boundaries, lock-in thresholds, knife-edge, risk multiplier, spread/liquidity edges), `test_phase11_integration.py` (11 tests — full 6-module chain + end-to-end pipeline), `test_phase11_backtest.py` (10 tests — backtest from slates + candidates), `test_phase11_edge_cases.py` (22 tests — DST, missing data, None propagation, correlation caps); `backtest.py` new source module; 81 new tests (333 total) |
| 2026-02-12 | Phase 10 complete | 10 | Orchestrator: `run_pipeline()` full end-to-end orchestration, `output/{date}/` artifact persistence with timestamped filenames, `find_prior_slate()` picks latest prior run, `apply_stability_rules()` suppresses bucket changes below thresholds, schedule helpers (ET time, scheduled hour check); 20 new tests (242 total) |
| 2026-02-12 | Phase 9 complete | 9 | Output generation: `build_daily_slate()` assembles pipeline results, `generate_outputs()` writes REPORT.md + DAILY_SLATE.json, `compute_delta()` compares runs (NEW/REMOVED/price/EV/bucket/rank changes), `should_suppress_change()` stability rules (≥2c move or EV flip or confidence change), `load_prior_slate()` for JSON roundtrip; 27 new tests (232 total) |
| 2026-02-12 | Phase 8 complete | 8 | Team lead: unified object merger, 6-gate hard reject pipeline (mapping confidence, missing ask, spread, EV, LOW/HIGH lock-in), bucket classifier (PRIMARY/TIGHT/NEAR-MISS/REJECTED), 5-key ranking engine, pick count enforcement with demotion; full `run_bucket_pipeline()`; 34 new tests (205 total) |
| 2026-02-12 | Phase 7 complete | 7 | Risk module: 7 correlation groups, 7 metro clusters, safe substring matching (min 4 chars), correlation/metro cap enforcement with priority sorting, equal-weight stake allocation with risk multiplier, risk flag aggregation (10 flag types), full RiskRecommendation builder; 35 new tests (171 total) |
| 2026-02-12 | Phase 6 complete | 6 | Planner: liquidity assessment (OK/THIN/REJECT), spread sanity with wide-spread exception, bid improvement logic (normal/tight), 8-step manual order checklist, cancel/replace rules; 28 new tests (136 total) |
| 2026-02-12 | Phase 5 complete | 5 | Accountant: Kalshi fee model (taker/maker), EV computation with maker fees for limit orders, max buy price search, edge vs implied, no-trade gate; config updated to Feb 2026 fee schedule; 25 new tests (108 total) |
| 2026-02-12 | Phase 4 complete | 4 | Modeler: NWS weather API client (obs + hourly forecast), Normal CDF probability model with adaptive sigma, lock-in gates (LOW sunrise+2h, HIGH peak+2h), knife-edge scoring, 27 station coords; 29 new tests (83 total) |
| 2026-02-12 | Phase 3 complete | 3 | Rules module: 26-station DB (ICAO, CLI issuedby, timezone, aliases), CLI day-window with LST/DST logic, mapping confidence, special risk flags; 22 new tests (54 total) |
| 2026-02-12 | Phase 2 complete | 2 | Kalshi API client (RSA-PSS auth, read-only enforced), scanner (series discovery, event filtering, orderbook parsing, candidate filtering), 26 new tests (32 total) |
| 2026-02-12 | Phase 1 complete | 1 | Project structure, config, schemas (17 Pydantic models), artifact writers (REPORT.md + JSON), 6 tests passing, lint clean |
| 2026-02-12 | Initial roadmap created | All | Full 12-phase roadmap defined from design document; Python selected as language |

---

## Architecture Decisions

| Decision | Choice | Rationale | Date |
|----------|--------|-----------|------|
| Language | Python | Best ecosystem for data work, NWS/Kalshi API libraries, numerical computation | 2026-02-12 |
| Kalshi auth | RSA-PSS signed headers (no SDK) | Direct httpx client with path allowlist is simpler and keeps read-only guarantee explicit | 2026-02-12 |
| Orderbook format | Legacy `orderbook.yes`/`orderbook.no` (cents) | Simpler integer math; will migrate to `orderbook_fp` if Kalshi deprecates legacy | 2026-02-12 |

---

## Key Constants Reference

| Constant | Value | Source |
|----------|-------|--------|
| Bankroll | $52.00 | Design doc |
| PRIMARY window | implied_best_no_ask in [90, 93] | Design doc |
| Scan window | implied_best_no_ask in [88, 95] | Design doc |
| Spread reject threshold | > 6 cents | Design doc |
| Bid room PRIMARY minimum | >= 2 cents | Design doc |
| Max picks per correlation group | 3 | Design doc |
| Max picks per metro cluster | 2 | Design doc |
| Run times | 7:00, 8:00, 9:00 AM ET | Design doc |
| Stability threshold | >= 2c move or EV sign flip | Design doc |
