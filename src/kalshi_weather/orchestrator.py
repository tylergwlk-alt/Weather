"""Multi-Run Orchestrator & Scheduler — Phase 10.

Coordinates the full scan-to-output pipeline, manages artifact
persistence with timestamps, and enforces multi-run stability.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from kalshi_weather.config import DEFAULT_CONFIG, Config
from kalshi_weather.output import (
    build_daily_slate,
    compute_delta,
    generate_outputs,
    load_prior_slate,
    should_suppress_change,
)
from kalshi_weather.schemas import DailySlate, UnifiedCandidate
from kalshi_weather.team_lead import run_bucket_pipeline

logger = logging.getLogger(__name__)

# ── Paths ────────────────────────────────────────────────────────────

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
OUTPUT_DIR = _PROJECT_ROOT / "output"
DATA_DIR = _PROJECT_ROOT / "data"


# ── Task #42: Artifact persistence ──────────────────────────────────

def _run_tag(run_time_et: str) -> str:
    """Convert a run_time_et string to a filesystem-safe tag."""
    return run_time_et.replace(":", "").replace(" ", "_")


def get_artifact_dir(
    target_date: str,
    base_dir: Optional[Path] = None,
) -> Path:
    """Get the artifact directory for a given target date.

    Structure: output/{target_date}/
    """
    d = (base_dir or OUTPUT_DIR) / target_date
    d.mkdir(parents=True, exist_ok=True)
    return d


def get_slate_path(
    target_date: str,
    run_time_et: str,
    base_dir: Optional[Path] = None,
) -> Path:
    """Get the JSON slate path for a specific run."""
    d = get_artifact_dir(target_date, base_dir)
    return d / f"DAILY_SLATE_{_run_tag(run_time_et)}.json"


def get_report_path(
    target_date: str,
    run_time_et: str,
    base_dir: Optional[Path] = None,
) -> Path:
    """Get the report path for a specific run."""
    d = get_artifact_dir(target_date, base_dir)
    return d / f"REPORT_{_run_tag(run_time_et)}.md"


def find_prior_slate(
    target_date: str,
    current_run_time_et: str,
    base_dir: Optional[Path] = None,
) -> Optional[DailySlate]:
    """Find the most recent prior slate for the same target date.

    Looks for DAILY_SLATE_*.json files in the target date directory,
    sorted by name (timestamp), and returns the latest one that is
    older than the current run.
    """
    d = (base_dir or OUTPUT_DIR) / target_date
    if not d.exists():
        return None

    current_tag = _run_tag(current_run_time_et)
    candidates = sorted(d.glob("DAILY_SLATE_*.json"))

    prior_path: Optional[Path] = None
    for p in candidates:
        # Extract tag from filename.
        tag = p.stem.replace("DAILY_SLATE_", "")
        if tag < current_tag:
            prior_path = p

    if prior_path is None:
        return None

    return load_prior_slate(prior_path)


def save_run_artifacts(
    slate: DailySlate,
    delta_notes: Optional[list[str]] = None,
    base_dir: Optional[Path] = None,
) -> tuple[Path, Path]:
    """Save run artifacts with proper timestamped paths.

    Returns (report_path, json_path).
    """
    d = get_artifact_dir(slate.target_date_local, base_dir)
    return generate_outputs(slate, delta_notes=delta_notes, output_dir=d)


# ── Task #43: Multi-run stability enforcement ───────────────────────

def apply_stability_rules(
    current_candidates: list[UnifiedCandidate],
    prior_slate: Optional[DailySlate],
    config: Config = DEFAULT_CONFIG,
) -> list[UnifiedCandidate]:
    """Apply multi-run stability rules.

    If a candidate existed in the prior run and the change doesn't
    meet stability thresholds, preserve the prior bucket.
    """
    if prior_slate is None:
        return current_candidates

    # Build prior lookup.
    prior_map: dict[str, UnifiedCandidate] = {}
    for bucket_list in [
        prior_slate.picks_primary, prior_slate.picks_tight,
        prior_slate.picks_near_miss, prior_slate.rejected,
    ]:
        for c in bucket_list:
            prior_map[c.market_ticker] = c

    for curr in current_candidates:
        prev = prior_map.get(curr.market_ticker)
        if prev is None:
            continue

        # If bucket changed but thresholds not met, revert to prior bucket.
        if curr.bucket != prev.bucket:
            if should_suppress_change(curr, prev, config):
                logger.info(
                    "Stability: suppressing %s bucket change %s -> %s",
                    curr.market_ticker, prev.bucket.value, curr.bucket.value,
                )
                curr.bucket = prev.bucket
                curr.bucket_reason = (
                    f"Stability: kept {prev.bucket.value} "
                    f"(change suppressed — thresholds not met)"
                )

    return current_candidates


# ── Task #41: Run schedule helpers ──────────────────────────────────

def get_current_run_time_et() -> str:
    """Get the current time formatted as a run_time_et string.

    Returns format like '2026-02-12 07:00 ET'.
    """
    # Use UTC and convert to ET (UTC-5 standard, UTC-4 DST).
    # For simplicity, we use the system clock. In production,
    # this would use pytz for accurate ET.
    now = datetime.now(timezone.utc)

    try:
        import pytz
        et = pytz.timezone("US/Eastern")
        now_et = now.astimezone(et)
    except ImportError:
        # Fallback: approximate ET as UTC-5.
        from datetime import timedelta
        now_et = now - timedelta(hours=5)

    return now_et.strftime("%Y-%m-%d %H:%M ET")


def get_target_date() -> str:
    """Get today's date in ET as YYYY-MM-DD."""
    now = datetime.now(timezone.utc)

    try:
        import pytz
        et = pytz.timezone("US/Eastern")
        now_et = now.astimezone(et)
    except ImportError:
        from datetime import timedelta
        now_et = now - timedelta(hours=5)

    return now_et.strftime("%Y-%m-%d")


def is_scheduled_run_time(
    config: Config = DEFAULT_CONFIG,
) -> bool:
    """Check if the current ET hour matches a scheduled run time."""
    now = datetime.now(timezone.utc)

    try:
        import pytz
        et = pytz.timezone("US/Eastern")
        now_et = now.astimezone(et)
    except ImportError:
        from datetime import timedelta
        now_et = now - timedelta(hours=5)

    return now_et.hour in config.schedule.run_hours_et


# ── Full orchestration pipeline ─────────────────────────────────────

def run_pipeline(
    candidates: list[UnifiedCandidate],
    run_time_et: Optional[str] = None,
    target_date: Optional[str] = None,
    events_scanned: int = 0,
    brackets_scanned: int = 0,
    candidates_in_window: int = 0,
    output_dir: Optional[Path] = None,
    config: Config = DEFAULT_CONFIG,
) -> DailySlate:
    """Run the full orchestration pipeline.

    1. Bucket classification + ranking
    2. Load prior slate for delta
    3. Apply stability rules
    4. Re-bucket after stability
    5. Build DailySlate
    6. Compute delta notes
    7. Save artifacts

    Returns the completed DailySlate.
    """
    run_time = run_time_et or get_current_run_time_et()
    date = target_date or get_target_date()

    # Step 1: Bucket pipeline.
    primary, tight, near_miss, rejected = run_bucket_pipeline(
        candidates, config
    )

    # Step 2: Load prior slate.
    prior = find_prior_slate(date, run_time, output_dir)

    # Step 3: Apply stability rules.
    all_classified = primary + tight + near_miss
    all_classified = apply_stability_rules(all_classified, prior, config)

    # Step 4: Re-sort into buckets after stability adjustments.
    from kalshi_weather.schemas import Bucket
    primary = [c for c in all_classified if c.bucket == Bucket.PRIMARY]
    tight = [c for c in all_classified if c.bucket == Bucket.TIGHT]
    near_miss = [c for c in all_classified if c.bucket == Bucket.NEAR_MISS]

    # Step 5: Build slate.
    slate = build_daily_slate(
        run_time_et=run_time,
        target_date_local=date,
        primary=primary,
        tight=tight,
        near_miss=near_miss,
        rejected=rejected,
        events_scanned=events_scanned,
        brackets_scanned=brackets_scanned,
        candidates_in_window=candidates_in_window,
        config=config,
    )

    # Step 6: Compute delta.
    delta_notes = None
    if prior is not None:
        delta_notes = compute_delta(slate, prior, config)
        slate.notes.extend(delta_notes)

    # Step 7: Save artifacts.
    save_run_artifacts(slate, delta_notes=delta_notes, base_dir=output_dir)

    logger.info(
        "Pipeline complete: %d PRIMARY, %d TIGHT, %d NEAR-MISS, %d REJECTED",
        len(primary), len(tight), len(near_miss), len(rejected),
    )

    return slate
