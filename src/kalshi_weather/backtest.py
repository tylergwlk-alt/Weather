"""Backtesting Harness — Phase 11, Task #46.

Replays historical market/weather data through the pipeline
and produces performance metrics.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

from kalshi_weather.config import DEFAULT_CONFIG, Config
from kalshi_weather.schemas import DailySlate, UnifiedCandidate

logger = logging.getLogger(__name__)


@dataclass
class BacktestResult:
    """Results from a single backtested day."""

    date: str
    total_picks: int = 0
    primary_picks: int = 0
    tight_picks: int = 0
    near_miss_picks: int = 0
    rejected_picks: int = 0
    total_suggested_stake: float = 0.0
    notes: list[str] = field(default_factory=list)


@dataclass
class BacktestSummary:
    """Aggregated backtest results across multiple days."""

    days_tested: int = 0
    total_primary: int = 0
    total_tight: int = 0
    total_near_miss: int = 0
    total_rejected: int = 0
    avg_primary_per_day: float = 0.0
    avg_ev_primary: float = 0.0
    day_results: list[BacktestResult] = field(default_factory=list)


def backtest_from_slates(
    slate_paths: list[Path],
) -> BacktestSummary:
    """Run backtest analysis on a set of saved DailySlate JSON files.

    Loads each slate and computes aggregate statistics.
    """
    summary = BacktestSummary()
    all_primary_ev: list[float] = []

    for path in sorted(slate_paths):
        if not path.exists():
            logger.warning("Slate file not found: %s", path)
            continue

        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            slate = DailySlate.model_validate(data)
        except Exception:
            logger.warning("Failed to load slate: %s", path, exc_info=True)
            continue

        result = _analyze_slate(slate)
        summary.day_results.append(result)
        summary.days_tested += 1
        summary.total_primary += result.primary_picks
        summary.total_tight += result.tight_picks
        summary.total_near_miss += result.near_miss_picks
        summary.total_rejected += result.rejected_picks

        # Collect EV values for averaging.
        for pick in slate.picks_primary:
            if pick.fees_ev is not None:
                all_primary_ev.append(
                    pick.fees_ev.ev_net_est_cents_at_recommended_limit
                )

    if summary.days_tested > 0:
        summary.avg_primary_per_day = summary.total_primary / summary.days_tested
    if all_primary_ev:
        summary.avg_ev_primary = sum(all_primary_ev) / len(all_primary_ev)

    return summary


def _analyze_slate(slate: DailySlate) -> BacktestResult:
    """Analyze a single slate for backtest metrics."""
    result = BacktestResult(date=slate.target_date_local)
    result.primary_picks = len(slate.picks_primary)
    result.tight_picks = len(slate.picks_tight)
    result.near_miss_picks = len(slate.picks_near_miss)
    result.rejected_picks = len(slate.rejected)
    result.total_picks = (
        result.primary_picks + result.tight_picks
        + result.near_miss_picks + result.rejected_picks
    )

    for pick in slate.picks_primary + slate.picks_tight:
        if pick.allocation is not None:
            result.total_suggested_stake += pick.allocation.suggested_stake_usd

    return result


def backtest_candidates(
    candidates_by_date: dict[str, list[UnifiedCandidate]],
    config: Config = DEFAULT_CONFIG,
) -> BacktestSummary:
    """Run backtest on pre-built candidates organized by date.

    Parameters
    ----------
    candidates_by_date : dict mapping date strings to lists of UnifiedCandidates.
    """
    from kalshi_weather.orchestrator import run_pipeline

    summary = BacktestSummary()
    all_primary_ev: list[float] = []

    for date_str in sorted(candidates_by_date.keys()):
        candidates = candidates_by_date[date_str]

        slate = run_pipeline(
            candidates,
            run_time_et=f"{date_str} 07:00 ET",
            target_date=date_str,
            candidates_in_window=len(candidates),
            config=config,
        )

        result = _analyze_slate(slate)
        summary.day_results.append(result)
        summary.days_tested += 1
        summary.total_primary += result.primary_picks
        summary.total_tight += result.tight_picks
        summary.total_near_miss += result.near_miss_picks
        summary.total_rejected += result.rejected_picks

        for pick in slate.picks_primary:
            if pick.fees_ev is not None:
                all_primary_ev.append(
                    pick.fees_ev.ev_net_est_cents_at_recommended_limit
                )

    if summary.days_tested > 0:
        summary.avg_primary_per_day = summary.total_primary / summary.days_tested
    if all_primary_ev:
        summary.avg_ev_primary = sum(all_primary_ev) / len(all_primary_ev)

    return summary
