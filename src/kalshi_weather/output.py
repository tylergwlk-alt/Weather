"""Output Generation — Phase 9.

Builds DailySlate from pipeline results, computes deltas between runs,
and orchestrates writing REPORT.md and DAILY_SLATE.json.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Optional

from kalshi_weather.artifacts import write_daily_slate_json, write_report_md
from kalshi_weather.config import DEFAULT_CONFIG, Config
from kalshi_weather.schemas import DailySlate, ScanStats, UnifiedCandidate

logger = logging.getLogger(__name__)


# ── Task #38 / #39: Build DailySlate from pipeline results ──────────

def build_daily_slate(
    run_time_et: str,
    target_date_local: str,
    primary: list[UnifiedCandidate],
    tight: list[UnifiedCandidate],
    near_miss: list[UnifiedCandidate],
    rejected: list[UnifiedCandidate],
    events_scanned: int = 0,
    brackets_scanned: int = 0,
    candidates_in_window: int = 0,
    notes: Optional[list[str]] = None,
    config: Config = DEFAULT_CONFIG,
) -> DailySlate:
    """Assemble a DailySlate from bucket pipeline output."""
    stats = ScanStats(
        events_scanned=events_scanned,
        bracket_markets_scanned=brackets_scanned,
        candidates_in_88_95_window=candidates_in_window,
        primary_count=len(primary),
        tight_count=len(tight),
        near_miss_count=len(near_miss),
        rejected_count=len(rejected),
    )

    return DailySlate(
        run_time_et=run_time_et,
        target_date_local=target_date_local,
        bankroll_usd=config.bankroll.total_usd,
        scan_stats=stats,
        picks_primary=primary,
        picks_tight=tight,
        picks_near_miss=near_miss,
        rejected=rejected,
        notes=notes or [],
    )


def generate_outputs(
    slate: DailySlate,
    delta_notes: Optional[list[str]] = None,
    output_dir: Optional[Path] = None,
) -> tuple[Path, Path]:
    """Write both REPORT.md and DAILY_SLATE.json.

    Returns (report_path, json_path).
    """
    run_tag = slate.run_time_et.replace(":", "").replace(" ", "_")
    base_dir = output_dir or Path(__file__).resolve().parent.parent.parent / "output"
    base_dir.mkdir(parents=True, exist_ok=True)

    report_path = base_dir / f"REPORT_{run_tag}.md"
    json_path = base_dir / f"DAILY_SLATE_{run_tag}.json"

    report_path = write_report_md(slate, delta_notes=delta_notes, path=report_path)
    json_path = write_daily_slate_json(slate, path=json_path)

    return report_path, json_path


# ── Task #40: Delta computation ─────────────────────────────────────

def _get_candidate_key(c: UnifiedCandidate) -> str:
    """Unique key for matching candidates across runs."""
    return c.market_ticker


def compute_delta(
    current: DailySlate,
    prior: DailySlate,
    config: Config = DEFAULT_CONFIG,
) -> list[str]:
    """Compare current run to a prior run and generate delta notes.

    Stability rule: suppress bucket changes unless:
    - implied_best_no_ask moves >= 2c
    - EV net flips sign
    - liquidity/spread changes materially
    - mapping confidence changes
    """
    notes: list[str] = []
    min_move = config.stability.min_price_move_cents

    # Build lookup of prior candidates by ticker.
    prior_map: dict[str, UnifiedCandidate] = {}
    for bucket_list in [
        prior.picks_primary, prior.picks_tight,
        prior.picks_near_miss, prior.rejected,
    ]:
        for c in bucket_list:
            prior_map[_get_candidate_key(c)] = c

    # Build lookup of current candidates.
    current_map: dict[str, UnifiedCandidate] = {}
    for bucket_list in [
        current.picks_primary, current.picks_tight,
        current.picks_near_miss, current.rejected,
    ]:
        for c in bucket_list:
            current_map[_get_candidate_key(c)] = c

    # Compare each current candidate to its prior version.
    for ticker, curr in current_map.items():
        prev = prior_map.get(ticker)
        if prev is None:
            notes.append(f"NEW: {ticker} appeared (bucket: {curr.bucket.value})")
            continue

        changes = _compare_candidates(curr, prev, min_move)
        for change in changes:
            notes.append(f"{ticker}: {change}")

    # Check for candidates that disappeared.
    for ticker in prior_map:
        if ticker not in current_map:
            prev = prior_map[ticker]
            notes.append(
                f"REMOVED: {ticker} (was {prev.bucket.value})"
            )

    # Summary stats delta.
    cs, ps = current.scan_stats, prior.scan_stats
    if cs.primary_count != ps.primary_count:
        notes.append(
            f"PRIMARY count: {ps.primary_count} -> {cs.primary_count}"
        )
    if cs.tight_count != ps.tight_count:
        notes.append(
            f"TIGHT count: {ps.tight_count} -> {cs.tight_count}"
        )

    if not notes:
        notes.append("No material changes from prior run.")

    return notes


def _compare_candidates(
    curr: UnifiedCandidate,
    prev: UnifiedCandidate,
    min_move: int,
) -> list[str]:
    """Compare two versions of the same candidate."""
    changes: list[str] = []

    # Bucket change.
    if curr.bucket != prev.bucket:
        changes.append(
            f"bucket {prev.bucket.value} -> {curr.bucket.value}"
        )

    # Price movement.
    curr_ask = curr.orderbook_snapshot.implied_best_no_ask_cents
    prev_ask = prev.orderbook_snapshot.implied_best_no_ask_cents
    if curr_ask is not None and prev_ask is not None:
        move = abs(curr_ask - prev_ask)
        if move >= min_move:
            changes.append(f"ask moved {prev_ask}c -> {curr_ask}c ({move:+d}c)")

    # EV sign flip.
    curr_ev = curr.fees_ev.ev_net_est_cents_at_recommended_limit if curr.fees_ev else None
    prev_ev = prev.fees_ev.ev_net_est_cents_at_recommended_limit if prev.fees_ev else None
    if curr_ev is not None and prev_ev is not None:
        if (curr_ev > 0) != (prev_ev > 0):
            changes.append(
                f"EV flipped: {prev_ev:.1f}c -> {curr_ev:.1f}c"
            )

    # Rank change.
    if curr.rank is not None and prev.rank is not None:
        if curr.rank != prev.rank:
            changes.append(f"rank {prev.rank} -> {curr.rank}")

    return changes


def should_suppress_change(
    curr: UnifiedCandidate,
    prev: UnifiedCandidate,
    config: Config = DEFAULT_CONFIG,
) -> bool:
    """Check if a bucket change should be suppressed per stability rules.

    Returns True if the change is too small to warrant a bucket change.
    """
    min_move = config.stability.min_price_move_cents

    curr_ask = curr.orderbook_snapshot.implied_best_no_ask_cents
    prev_ask = prev.orderbook_snapshot.implied_best_no_ask_cents

    # Price moved enough?
    if curr_ask is not None and prev_ask is not None:
        if abs(curr_ask - prev_ask) >= min_move:
            return False

    # EV sign flip?
    curr_ev = curr.fees_ev.ev_net_est_cents_at_recommended_limit if curr.fees_ev else None
    prev_ev = prev.fees_ev.ev_net_est_cents_at_recommended_limit if prev.fees_ev else None
    if curr_ev is not None and prev_ev is not None:
        if (curr_ev > 0) != (prev_ev > 0):
            return False

    # Mapping confidence change?
    curr_conf = curr.settlement_spec.mapping_confidence if curr.settlement_spec else None
    prev_conf = prev.settlement_spec.mapping_confidence if prev.settlement_spec else None
    if curr_conf != prev_conf:
        return False

    # None of the thresholds met — suppress.
    return True


def load_prior_slate(path: Path) -> Optional[DailySlate]:
    """Load a prior DailySlate from JSON for delta comparison."""
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return DailySlate.model_validate(data)
    except Exception:
        logger.warning("Failed to load prior slate from %s", path, exc_info=True)
        return None
