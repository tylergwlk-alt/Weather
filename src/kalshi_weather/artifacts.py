"""Artifact output layer — generates REPORT.md and DAILY_SLATE.json."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Optional

from jinja2 import Template

from kalshi_weather.schemas import DailySlate, UnifiedCandidate

logger = logging.getLogger(__name__)

# ── Paths ──────────────────────────────────────────────────────────────

OUTPUT_DIR = Path(__file__).resolve().parent.parent.parent / "output"


def _ensure_output_dir() -> Path:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    return OUTPUT_DIR


# ── JSON Writer ────────────────────────────────────────────────────────

def write_daily_slate_json(slate: DailySlate, path: Optional[Path] = None) -> Path:
    """Serialize DailySlate to JSON and write to disk."""
    out = path or _ensure_output_dir() / f"DAILY_SLATE_{slate.run_time_et[:10]}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(slate.model_dump(mode="json"), indent=2, default=str),
        encoding="utf-8",
    )
    return out


# ── Markdown Report Writer ─────────────────────────────────────────────

_REPORT_TEMPLATE = Template("""\
# Kalshi Temperature "Unlikely NO" Slate — {{ slate.target_date_local }}

## Run Metadata
- **run_time_et:** {{ slate.run_time_et }}
- **target_date_local:** {{ slate.target_date_local }}
- **bankroll_usd:** ${{ "%.2f"|format(slate.bankroll_usd) }}

## Scan Coverage
- **events_scanned:** {{ stats.events_scanned }}
- **bracket_markets_scanned:** {{ stats.bracket_markets_scanned }}
- **candidates_in_88_95_window:** {{ stats.candidates_in_88_95_window }}
- **primary_count:** {{ stats.primary_count }}
- **tight_count:** {{ stats.tight_count }}
- **near_miss_count:** {{ stats.near_miss_count }}
- **rejected_count:** {{ stats.rejected_count }}

## PRIMARY Picks (Recommended)
{% if slate.picks_primary %}
| Rank | City | High/Low | Bracket | impl NO ask | best NO bid | bid room | p(NO) | Edge % | Rec Limit | Max Buy | Stake | Notes |
|------|------|----------|---------|-------------|-------------|----------|-------|--------|-----------|---------|-------|-------|
{% for p in slate.picks_primary -%}
| {{ p.rank or loop.index }} | {{ p.city }} | {{ p.market_type.value }} | {{ p.bracket_definition }} | {{ ob(p).implied_best_no_ask_cents }} | {{ ob(p).best_no_bid_cents }} | {{ ob(p).bid_room_cents }} | {{ mdl_pno(p) }} | {{ edge(p) }} | {{ rec_limit(p) }} | {{ max_buy(p) }} | {{ stake(p) }} | {{ notes_short(p) }} |
{% endfor -%}
{% else %}
_No PRIMARY picks this run._
{% endif %}

## TIGHT Picks
{% if slate.picks_tight %}
| Rank | City | High/Low | Bracket | impl NO ask | best NO bid | bid room | p(NO) | Edge % | Rec Limit | Max Buy | Stake | Notes |
|------|------|----------|---------|-------------|-------------|----------|-------|--------|-----------|---------|-------|-------|
{% for p in slate.picks_tight -%}
| {{ p.rank or loop.index }} | {{ p.city }} | {{ p.market_type.value }} | {{ p.bracket_definition }} | {{ ob(p).implied_best_no_ask_cents }} | {{ ob(p).best_no_bid_cents }} | {{ ob(p).bid_room_cents }} | {{ mdl_pno(p) }} | {{ edge(p) }} | {{ rec_limit(p) }} | {{ max_buy(p) }} | {{ stake(p) }} | {{ notes_short(p) }} |
{% endfor -%}
{% else %}
_No TIGHT picks this run._
{% endif %}

## NEAR-MISS Watchlist
{% if slate.picks_near_miss %}
| Rank | City | High/Low | Bracket | impl NO ask | best NO bid | bid room | p(NO) | Edge % | Rec Limit | Max Buy | Stake | Notes |
|------|------|----------|---------|-------------|-------------|----------|-------|--------|-----------|---------|-------|-------|
{% for p in slate.picks_near_miss -%}
| {{ p.rank or loop.index }} | {{ p.city }} | {{ p.market_type.value }} | {{ p.bracket_definition }} | {{ ob(p).implied_best_no_ask_cents }} | {{ ob(p).best_no_bid_cents }} | {{ ob(p).bid_room_cents }} | {{ mdl_pno(p) }} | {{ edge(p) }} | {{ rec_limit(p) }} | {{ max_buy(p) }} | {{ stake(p) }} | {{ notes_short(p) }} |
{% endfor -%}
{% else %}
_No near-miss candidates this run._
{% endif %}

## REJECTED Summary
- **Total rejected:** {{ stats.rejected_count }}
{% if slate.rejected %}
{% for r in slate.rejected -%}
- `{{ r.market_ticker }}` — {{ r.bucket_reason }}
{% endfor -%}
{% else %}
_None._
{% endif %}

## Manual Placement Checklist
1. Log in to [Kalshi](https://kalshi.com) (do NOT use API for orders).
2. Navigate to each recommended market via the provided URL.
3. Select **NO** side.
4. Set limit price to the **Rec Limit** value shown above.
5. Set quantity based on the **Stake** column (contracts = stake / limit price).
6. Review order details, then submit.
7. Set a timer to check fills after 5-10 minutes.
8. If not filled within 15 min, consider adjusting limit by 1c toward the ask.
9. Do NOT chase — cancel if price moves outside your edge window.

## Delta vs Previous Run
{% if delta_notes %}
{% for note in delta_notes -%}
- {{ note }}
{% endfor -%}
{% else %}
_No prior run available for comparison._
{% endif %}
""", keep_trailing_newline=True)


def _safe_val(val, default="-"):
    """Return val if not None, else default."""
    return val if val is not None else default


def _ob(candidate: UnifiedCandidate):
    """Helper to access orderbook with safe defaults."""
    return candidate.orderbook_snapshot


def _mdl_pno(candidate: UnifiedCandidate) -> str:
    if candidate.model:
        return f"{candidate.model.p_no:.2%}"
    return "-"


def _edge(candidate: UnifiedCandidate) -> str:
    if candidate.fees_ev:
        return f"{candidate.fees_ev.edge_vs_implied_pct:.1f}%"
    return "-"


def _rec_limit(candidate: UnifiedCandidate) -> str:
    if candidate.manual_trade_plan:
        return str(candidate.manual_trade_plan.recommended_limit_no_cents)
    return "-"


def _max_buy(candidate: UnifiedCandidate) -> str:
    if candidate.fees_ev:
        return str(candidate.fees_ev.max_buy_price_no_cents)
    return "-"


def _stake(candidate: UnifiedCandidate) -> str:
    if candidate.allocation:
        return f"${candidate.allocation.suggested_stake_usd:.2f}"
    return "-"


def _notes_short(candidate: UnifiedCandidate) -> str:
    parts = []
    if candidate.model:
        if candidate.model.lock_in_flag_if_low:
            parts.append(f"low:{candidate.model.lock_in_flag_if_low.value}")
        if candidate.model.high_lock_in_flag:
            parts.append(f"high:{candidate.model.high_lock_in_flag.value}")
        vol_hrs = candidate.model.hours_remaining_in_meaningful_volatility_window
        parts.append(f"{vol_hrs:.1f}h vol")
    if candidate.warnings:
        parts.append("; ".join(candidate.warnings[:2]))
    return ", ".join(parts) if parts else "-"


def write_report_md(
    slate: DailySlate,
    delta_notes: Optional[list[str]] = None,
    path: Optional[Path] = None,
) -> Path:
    """Render REPORT.md from a DailySlate and write to disk."""
    out = path or _ensure_output_dir() / f"REPORT_{slate.run_time_et[:10]}.md"
    out.parent.mkdir(parents=True, exist_ok=True)

    rendered = _REPORT_TEMPLATE.render(
        slate=slate,
        stats=slate.scan_stats,
        delta_notes=delta_notes or [],
        ob=_ob,
        mdl_pno=_mdl_pno,
        edge=_edge,
        rec_limit=_rec_limit,
        max_buy=_max_buy,
        stake=_stake,
        notes_short=_notes_short,
    )
    out.write_text(rendered, encoding="utf-8")
    return out
