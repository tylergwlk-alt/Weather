"""Phase 10 tests — Multi-Run Orchestration & Scheduling."""

from __future__ import annotations

import json

from kalshi_weather.orchestrator import (
    apply_stability_rules,
    find_prior_slate,
    get_artifact_dir,
    get_current_run_time_et,
    get_report_path,
    get_slate_path,
    get_target_date,
    is_scheduled_run_time,
    run_pipeline,
    save_run_artifacts,
)
from kalshi_weather.output import build_daily_slate
from kalshi_weather.schemas import (
    Accounting,
    Bucket,
    DailySlate,
    KnifeEdgeRisk,
    MappingConfidence,
    MarketType,
    ModelOutput,
    OrderbookSnapshot,
    SettlementSpec,
    UncertaintyLevel,
    UnifiedCandidate,
)

# ── Helpers ────────────────────────────────────────────────────────────

def _uc(
    ticker="T1",
    city="Chicago",
    ask=91,
    bid=88,
    room=3,
    bucket=Bucket.PRIMARY,
    rank=1,
    ev=5.0,
    confidence=MappingConfidence.HIGH,
) -> UnifiedCandidate:
    return UnifiedCandidate(
        run_time_et="2026-02-12 07:00 ET",
        target_date_local="2026-02-12",
        city=city,
        market_type=MarketType.HIGH_TEMP,
        event_name="KXHIGHCHI",
        market_ticker=ticker,
        market_url=f"https://kalshi.com/markets/{ticker}",
        bracket_definition="50°F or above",
        settlement_spec=SettlementSpec(
            city=city,
            market_type=MarketType.HIGH_TEMP,
            issuedby="ORD",
            cli_url="https://x",
            what_to_read_in_cli="MAX TEMP",
            day_window_note="CST",
            mapping_confidence=confidence,
        ),
        orderbook_snapshot=OrderbookSnapshot(
            best_yes_bid_cents=100 - ask if ask else None,
            best_no_bid_cents=bid,
            implied_best_no_ask_cents=ask,
            implied_best_yes_ask_cents=100 - bid if bid else None,
            bid_room_cents=room,
            top3_yes_bids=[[9, 30]],
            top3_no_bids=[[88, 40]],
        ),
        model=ModelOutput(
            market_ticker=ticker,
            p_yes=0.05,
            p_no=0.95,
            method="test",
            uncertainty_level=UncertaintyLevel.LOW,
            local_time_at_station="2026-02-12 07:00 CST",
            hours_remaining_until_cli_day_close=17.0,
            hours_remaining_in_meaningful_volatility_window=6.0,
            knife_edge_risk=KnifeEdgeRisk.LOW,
        ),
        fees_ev=Accounting(
            market_ticker=ticker,
            implied_p_no_from_implied_ask=0.91,
            fee_est_cents_per_contract=1,
            ev_net_est_cents_at_recommended_limit=ev,
            max_buy_price_no_cents=93,
            edge_vs_implied_pct=4.0,
        ),
        bucket=bucket,
        bucket_reason="test",
        rank=rank,
    )


def _slate(
    primary=None,
    tight=None,
    near_miss=None,
    rejected=None,
    run_time="2026-02-12 07:00 ET",
) -> DailySlate:
    return build_daily_slate(
        run_time_et=run_time,
        target_date_local="2026-02-12",
        primary=primary or [],
        tight=tight or [],
        near_miss=near_miss or [],
        rejected=rejected or [],
        events_scanned=10,
        brackets_scanned=50,
        candidates_in_window=5,
    )


def _save_slate(slate: DailySlate, base_dir):
    """Helper to save a slate to disk for testing."""
    d = base_dir / slate.target_date_local
    d.mkdir(parents=True, exist_ok=True)
    tag = slate.run_time_et.replace(":", "").replace(" ", "_")
    path = d / f"DAILY_SLATE_{tag}.json"
    path.write_text(
        json.dumps(slate.model_dump(mode="json"), indent=2, default=str),
        encoding="utf-8",
    )
    return path


# ── Artifact persistence (Task #42) ─────────────────────────────────

class TestArtifactPersistence:
    def test_artifact_dir_created(self, tmp_path):
        d = get_artifact_dir("2026-02-12", tmp_path)
        assert d.exists()
        assert d.name == "2026-02-12"

    def test_slate_path(self, tmp_path):
        p = get_slate_path("2026-02-12", "2026-02-12 07:00 ET", tmp_path)
        assert "DAILY_SLATE" in p.name
        assert p.suffix == ".json"

    def test_report_path(self, tmp_path):
        p = get_report_path("2026-02-12", "2026-02-12 07:00 ET", tmp_path)
        assert "REPORT" in p.name
        assert p.suffix == ".md"

    def test_save_and_find(self, tmp_path):
        slate = _slate(
            primary=[_uc()],
            run_time="2026-02-12 07:00 ET",
        )
        # save_run_artifacts creates output inside {base_dir}/{target_date}/
        # so pass tmp_path directly — it will create tmp_path/2026-02-12/
        save_run_artifacts(slate, base_dir=tmp_path)

        found = find_prior_slate(
            "2026-02-12", "2026-02-12 08:00 ET", tmp_path
        )
        assert found is not None
        assert found.run_time_et == "2026-02-12 07:00 ET"

    def test_find_no_prior(self, tmp_path):
        found = find_prior_slate("2026-02-12", "2026-02-12 07:00 ET", tmp_path)
        assert found is None

    def test_find_prior_picks_latest(self, tmp_path):
        # Save 7 AM and 8 AM runs.
        slate7 = _slate(run_time="2026-02-12 07:00 ET")
        slate8 = _slate(run_time="2026-02-12 08:00 ET")
        _save_slate(slate7, tmp_path)
        _save_slate(slate8, tmp_path)

        # Looking for prior to 9 AM → should find 8 AM.
        found = find_prior_slate(
            "2026-02-12", "2026-02-12 09:00 ET", tmp_path
        )
        assert found is not None
        assert found.run_time_et == "2026-02-12 08:00 ET"

    def test_find_prior_ignores_current(self, tmp_path):
        slate7 = _slate(run_time="2026-02-12 07:00 ET")
        _save_slate(slate7, tmp_path)

        # Looking for prior to 7 AM itself → no prior.
        found = find_prior_slate(
            "2026-02-12", "2026-02-12 07:00 ET", tmp_path
        )
        assert found is None


# ── Multi-run stability (Task #43) ──────────────────────────────────

class TestStabilityRules:
    def test_no_prior_no_change(self):
        candidates = [_uc(ticker="T1")]
        result = apply_stability_rules(candidates, None)
        assert result[0].bucket == Bucket.PRIMARY

    def test_suppress_tiny_bucket_change(self):
        """1c move should not allow bucket to change."""
        prior = _slate(primary=[_uc(ticker="T1", ask=91)])
        # Current: was PRIMARY (91c), now classified as TIGHT (92c, 1c move)
        curr = _uc(ticker="T1", ask=92, room=1, bucket=Bucket.TIGHT)
        result = apply_stability_rules([curr], prior)
        assert result[0].bucket == Bucket.PRIMARY  # Suppressed back
        assert "Stability" in result[0].bucket_reason

    def test_allow_large_bucket_change(self):
        """3c move should allow bucket change."""
        prior = _slate(primary=[_uc(ticker="T1", ask=91)])
        curr = _uc(ticker="T1", ask=94, room=3, bucket=Bucket.NEAR_MISS)
        result = apply_stability_rules([curr], prior)
        assert result[0].bucket == Bucket.NEAR_MISS  # Allowed

    def test_allow_ev_flip(self):
        """EV sign flip should allow bucket change."""
        prior = _slate(primary=[_uc(ticker="T1", ev=5.0)])
        curr = _uc(ticker="T1", ev=-2.0, bucket=Bucket.REJECTED)
        result = apply_stability_rules([curr], prior)
        assert result[0].bucket == Bucket.REJECTED  # Allowed

    def test_new_candidate_unaffected(self):
        """New candidates not in prior should pass through."""
        prior = _slate(primary=[_uc(ticker="T1")])
        curr = _uc(ticker="T2", bucket=Bucket.TIGHT)
        result = apply_stability_rules([curr], prior)
        assert result[0].bucket == Bucket.TIGHT


# ── Schedule helpers (Task #41) ──────────────────────────────────────

class TestScheduleHelpers:
    def test_run_time_format(self):
        rt = get_current_run_time_et()
        assert "ET" in rt
        assert len(rt) >= 16  # "YYYY-MM-DD HH:MM ET"

    def test_target_date_format(self):
        td = get_target_date()
        assert len(td) == 10  # "YYYY-MM-DD"
        assert td[4] == "-"
        assert td[7] == "-"

    def test_is_scheduled_returns_bool(self):
        result = is_scheduled_run_time()
        assert isinstance(result, bool)


# ── Full pipeline (Task #41/#42/#43) ────────────────────────────────

class TestRunPipeline:
    def test_basic_pipeline(self, tmp_path):
        candidates = [
            _uc(ticker="P1", ask=91, room=3, ev=8.0),
            _uc(ticker="T1", ask=92, room=1, ev=4.0),
            _uc(ticker="NM1", ask=89, room=3, ev=3.0),
        ]
        slate = run_pipeline(
            candidates,
            run_time_et="2026-02-12 07:00 ET",
            target_date="2026-02-12",
            events_scanned=10,
            brackets_scanned=50,
            candidates_in_window=3,
            output_dir=tmp_path,
        )
        assert len(slate.picks_primary) == 1
        assert len(slate.picks_tight) == 1
        assert len(slate.picks_near_miss) == 1

    def test_pipeline_saves_artifacts(self, tmp_path):
        candidates = [_uc(ticker="P1", ask=91, room=3)]
        run_pipeline(
            candidates,
            run_time_et="2026-02-12 07:00 ET",
            target_date="2026-02-12",
            output_dir=tmp_path,
        )
        # Check files were created.
        date_dir = tmp_path / "2026-02-12"
        assert date_dir.exists()
        jsons = list(date_dir.glob("DAILY_SLATE_*.json"))
        reports = list(date_dir.glob("REPORT_*.md"))
        assert len(jsons) == 1
        assert len(reports) == 1

    def test_pipeline_with_prior(self, tmp_path):
        # Run 1: 7 AM.
        candidates1 = [_uc(ticker="P1", ask=91, room=3, ev=8.0)]
        run_pipeline(
            candidates1,
            run_time_et="2026-02-12 07:00 ET",
            target_date="2026-02-12",
            output_dir=tmp_path,
        )

        # Run 2: 8 AM with same candidate but different EV.
        candidates2 = [_uc(ticker="P1", ask=91, room=3, ev=6.0)]
        slate2 = run_pipeline(
            candidates2,
            run_time_et="2026-02-12 08:00 ET",
            target_date="2026-02-12",
            output_dir=tmp_path,
        )
        # Should have delta notes since prior exists.
        assert len(slate2.notes) > 0

    def test_pipeline_empty(self, tmp_path):
        slate = run_pipeline(
            [],
            run_time_et="2026-02-12 07:00 ET",
            target_date="2026-02-12",
            output_dir=tmp_path,
        )
        assert len(slate.picks_primary) == 0
        assert len(slate.rejected) == 0

    def test_multi_run_stability_in_pipeline(self, tmp_path):
        """Run 7 AM, then 8 AM with small change — bucket should be stable."""
        # Run 1: P1 is PRIMARY at 91c.
        candidates1 = [_uc(ticker="P1", ask=91, room=3, ev=5.0)]
        run_pipeline(
            candidates1,
            run_time_et="2026-02-12 07:00 ET",
            target_date="2026-02-12",
            output_dir=tmp_path,
        )

        # Run 2: P1 at 92c with room=1 → would be TIGHT normally.
        # But 1c move < threshold → should stay PRIMARY.
        candidates2 = [_uc(ticker="P1", ask=92, room=1, ev=5.0)]
        slate2 = run_pipeline(
            candidates2,
            run_time_et="2026-02-12 08:00 ET",
            target_date="2026-02-12",
            output_dir=tmp_path,
        )
        assert len(slate2.picks_primary) == 1
        assert slate2.picks_primary[0].market_ticker == "P1"
