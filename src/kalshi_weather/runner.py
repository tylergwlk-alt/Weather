"""End-to-end runner — wires all 6 modules together.

Converts CandidateRaw objects from the scanner into UnifiedCandidate
objects for the orchestrator, then runs the full pipeline.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from kalshi_weather.accountant import compute_accounting
from kalshi_weather.config import DEFAULT_CONFIG, Config
from kalshi_weather.kalshi_client import KalshiClient
from kalshi_weather.modeler import model_candidate
from kalshi_weather.orchestrator import run_pipeline
from kalshi_weather.planner import build_execution_plan
from kalshi_weather.risk import build_risk_recommendation
from kalshi_weather.rules import build_settlement_spec, get_station_icao
from kalshi_weather.scanner import scan_today_markets
from kalshi_weather.schemas import CandidateRaw, DailySlate, UnifiedCandidate
from kalshi_weather.team_lead import merge_candidate
from kalshi_weather.weather_api import WeatherAPI

logger = logging.getLogger(__name__)


def enrich_candidate(
    raw: CandidateRaw,
    weather: WeatherAPI,
    config: Config = DEFAULT_CONFIG,
) -> UnifiedCandidate:
    """Enrich a single CandidateRaw through all 6 modules.

    Returns a fully-populated UnifiedCandidate ready for orchestration.
    """
    city = raw.city
    ob = raw.orderbook_snapshot

    # 1. Station lookup
    icao = get_station_icao(city)
    if icao is None:
        logger.warning("No ICAO station for %s — skipping weather enrichment", city)

    # 2. Weather data (obs + forecast)
    obs = None
    forecast = None
    current_temp_f: Optional[float] = None
    if icao:
        obs = weather.get_current_obs(icao)
        forecast = weather.get_hourly_forecast(icao)
        if obs:
            current_temp_f = obs.temp_f

    # 3. Settlement spec
    spec = build_settlement_spec(city, raw.market_type)

    # 4. Model
    model = model_candidate(raw, forecast, current_temp_f, config=config)

    # 5. Execution plan
    plan = build_execution_plan(raw.market_ticker, raw.market_url, ob, config=config)

    # 6. Accounting
    acct = compute_accounting(
        raw.market_ticker, ob, model, plan.recommended_limit_no_cents, config=config,
    )

    # 7. Risk
    risk = build_risk_recommendation(raw.market_ticker, city, model, acct, config=config)

    # 8. Merge
    return merge_candidate(raw, spec, model, acct, plan, risk)


def run_full_scan(
    client: KalshiClient,
    weather: WeatherAPI,
    output_dir: Optional[Path] = None,
    config: Config = DEFAULT_CONFIG,
) -> tuple[DailySlate, Path]:
    """Run the complete scan-to-slate pipeline.

    1. Scan Kalshi for today's temperature markets
    2. Enrich each candidate through all modules
    3. Run the orchestrator pipeline (bucket, rank, stability, artifacts)

    Returns (DailySlate, output_directory).
    """
    logger.info("Starting full scan...")

    # Scan markets
    candidates_raw = scan_today_markets(client, config)
    logger.info("Scanner found %d candidates in price window", len(candidates_raw))

    # Enrich each candidate
    unified: list[UnifiedCandidate] = []
    for raw in candidates_raw:
        try:
            uc = enrich_candidate(raw, weather, config)
            unified.append(uc)
        except Exception:
            logger.exception("Failed to enrich %s — skipping", raw.market_ticker)

    logger.info("Enriched %d / %d candidates", len(unified), len(candidates_raw))

    # Run orchestrator pipeline
    slate = run_pipeline(
        unified,
        events_scanned=0,
        brackets_scanned=0,
        candidates_in_window=len(candidates_raw),
        output_dir=output_dir,
        config=config,
    )

    # Resolve output dir for return
    from kalshi_weather.orchestrator import get_artifact_dir
    out = get_artifact_dir(slate.target_date_local, output_dir)

    logger.info("Full scan complete. Artifacts in %s", out)
    return slate, out
