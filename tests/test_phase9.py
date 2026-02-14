"""Phase 9 tests — Output Generation."""

from __future__ import annotations

import json

from kalshi_weather.output import (
    build_daily_slate,
    compute_delta,
    generate_outputs,
    load_prior_slate,
    should_suppress_change,
)
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
    p_no=0.95,
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
            p_yes=1 - p_no,
            p_no=p_no,
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


# ── Build DailySlate (Task #38/#39) ─────────────────────────────────

class TestBuildDailySlate:
    def test_basic_build(self):
        p1 = _uc(ticker="P1")
        slate = _slate(primary=[p1])
        assert slate.run_time_et == "2026-02-12 07:00 ET"
        assert slate.bankroll_usd == 42.0
        assert len(slate.picks_primary) == 1
        assert slate.scan_stats.primary_count == 1
        assert slate.scan_stats.events_scanned == 10

    def test_all_buckets(self):
        slate = _slate(
            primary=[_uc(ticker="P1")],
            tight=[_uc(ticker="T1", bucket=Bucket.TIGHT)],
            near_miss=[_uc(ticker="NM1", bucket=Bucket.NEAR_MISS)],
            rejected=[_uc(ticker="R1", bucket=Bucket.REJECTED)],
        )
        assert slate.scan_stats.primary_count == 1
        assert slate.scan_stats.tight_count == 1
        assert slate.scan_stats.near_miss_count == 1
        assert slate.scan_stats.rejected_count == 1

    def test_empty_slate(self):
        slate = _slate()
        assert slate.scan_stats.primary_count == 0
        assert len(slate.picks_primary) == 0

    def test_custom_notes(self):
        slate = build_daily_slate(
            "2026-02-12 07:00 ET", "2026-02-12",
            [], [], [], [],
            notes=["Test note"],
        )
        assert "Test note" in slate.notes


# ── Generate outputs (Task #38/#39) ──────────────────────────────────

class TestGenerateOutputs:
    def test_writes_both_files(self, tmp_path):
        slate = _slate(primary=[_uc()])
        report, json_out = generate_outputs(slate, output_dir=tmp_path)
        assert report.exists()
        assert json_out.exists()
        assert report.suffix == ".md"
        assert json_out.suffix == ".json"

    def test_report_content(self, tmp_path):
        slate = _slate(primary=[_uc(ticker="KXHIGHCHI-T50", city="Chicago")])
        report, _ = generate_outputs(slate, output_dir=tmp_path)
        content = report.read_text(encoding="utf-8")
        assert "Unlikely NO" in content
        assert "Chicago" in content
        assert "PRIMARY" in content

    def test_json_content(self, tmp_path):
        slate = _slate(primary=[_uc(ticker="KXHIGHCHI-T50")])
        _, json_out = generate_outputs(slate, output_dir=tmp_path)
        data = json.loads(json_out.read_text(encoding="utf-8"))
        assert data["run_time_et"] == "2026-02-12 07:00 ET"
        assert len(data["picks_primary"]) == 1
        assert data["picks_primary"][0]["market_ticker"] == "KXHIGHCHI-T50"

    def test_json_roundtrip(self, tmp_path):
        slate = _slate(primary=[_uc()])
        _, json_out = generate_outputs(slate, output_dir=tmp_path)
        loaded = load_prior_slate(json_out)
        assert loaded is not None
        assert loaded.run_time_et == slate.run_time_et
        assert len(loaded.picks_primary) == len(slate.picks_primary)

    def test_report_with_delta(self, tmp_path):
        slate = _slate(primary=[_uc()])
        delta = ["P1: ask moved 90c -> 92c (+2c)"]
        report, _ = generate_outputs(slate, delta_notes=delta, output_dir=tmp_path)
        content = report.read_text(encoding="utf-8")
        assert "ask moved" in content

    def test_report_empty_picks(self, tmp_path):
        slate = _slate()
        report, _ = generate_outputs(slate, output_dir=tmp_path)
        content = report.read_text(encoding="utf-8")
        assert "No PRIMARY picks" in content


# ── Delta computation (Task #40) ─────────────────────────────────────

class TestComputeDelta:
    def test_no_changes(self):
        prior = _slate(primary=[_uc(ticker="T1", ask=91)])
        current = _slate(primary=[_uc(ticker="T1", ask=91)])
        notes = compute_delta(current, prior)
        assert any("No material changes" in n for n in notes)

    def test_new_candidate(self):
        prior = _slate(primary=[_uc(ticker="T1")])
        current = _slate(primary=[_uc(ticker="T1"), _uc(ticker="T2")])
        notes = compute_delta(current, prior)
        assert any("NEW" in n and "T2" in n for n in notes)

    def test_removed_candidate(self):
        prior = _slate(primary=[_uc(ticker="T1"), _uc(ticker="T2")])
        current = _slate(primary=[_uc(ticker="T1")])
        notes = compute_delta(current, prior)
        assert any("REMOVED" in n and "T2" in n for n in notes)

    def test_price_movement(self):
        prior = _slate(primary=[_uc(ticker="T1", ask=90)])
        current = _slate(primary=[_uc(ticker="T1", ask=93)])
        notes = compute_delta(current, prior)
        assert any("ask moved" in n for n in notes)

    def test_small_price_movement_ignored(self):
        prior = _slate(primary=[_uc(ticker="T1", ask=91)])
        current = _slate(primary=[_uc(ticker="T1", ask=92)])
        notes = compute_delta(current, prior)
        # 1c move is below min_price_move_cents=2, so no ask note
        assert not any("ask moved" in n for n in notes)

    def test_ev_flip(self):
        prior = _slate(primary=[_uc(ticker="T1", ev=3.0)])
        current = _slate(primary=[_uc(ticker="T1", ev=-2.0)])
        notes = compute_delta(current, prior)
        assert any("EV flipped" in n for n in notes)

    def test_bucket_change(self):
        prior = _slate(primary=[_uc(ticker="T1", bucket=Bucket.PRIMARY)])
        current = _slate(tight=[_uc(ticker="T1", bucket=Bucket.TIGHT)])
        notes = compute_delta(current, prior)
        assert any("bucket" in n and "PRIMARY" in n and "TIGHT" in n for n in notes)

    def test_rank_change(self):
        prior = _slate(primary=[_uc(ticker="T1", rank=1)])
        current = _slate(primary=[_uc(ticker="T1", rank=3)])
        notes = compute_delta(current, prior)
        assert any("rank" in n for n in notes)

    def test_primary_count_change(self):
        prior = _slate(primary=[_uc(ticker="T1")])
        current = _slate(
            primary=[_uc(ticker="T1"), _uc(ticker="T2"), _uc(ticker="T3")]
        )
        notes = compute_delta(current, prior)
        assert any("PRIMARY count" in n for n in notes)


# ── Stability suppression ───────────────────────────────────────────

class TestShouldSuppressChange:
    def test_suppress_tiny_move(self):
        prev = _uc(ticker="T1", ask=91)
        curr = _uc(ticker="T1", ask=92)
        assert should_suppress_change(curr, prev) is True

    def test_allow_large_move(self):
        prev = _uc(ticker="T1", ask=90)
        curr = _uc(ticker="T1", ask=93)
        assert should_suppress_change(curr, prev) is False

    def test_allow_ev_flip(self):
        prev = _uc(ticker="T1", ev=3.0)
        curr = _uc(ticker="T1", ev=-2.0)
        assert should_suppress_change(curr, prev) is False

    def test_allow_confidence_change(self):
        prev = _uc(ticker="T1", confidence=MappingConfidence.HIGH)
        curr = _uc(ticker="T1", confidence=MappingConfidence.MED)
        assert should_suppress_change(curr, prev) is False

    def test_suppress_when_nothing_changed(self):
        prev = _uc(ticker="T1", ask=91, ev=5.0)
        curr = _uc(ticker="T1", ask=91, ev=5.0)
        assert should_suppress_change(curr, prev) is True


# ── Load prior slate ────────────────────────────────────────────────

class TestLoadPriorSlate:
    def test_load_valid(self, tmp_path):
        slate = _slate(primary=[_uc()])
        json_path = tmp_path / "prior.json"
        json_path.write_text(
            json.dumps(slate.model_dump(mode="json"), indent=2, default=str),
            encoding="utf-8",
        )
        loaded = load_prior_slate(json_path)
        assert loaded is not None
        assert loaded.run_time_et == slate.run_time_et

    def test_load_missing_file(self, tmp_path):
        result = load_prior_slate(tmp_path / "nonexistent.json")
        assert result is None

    def test_load_invalid_json(self, tmp_path):
        bad_path = tmp_path / "bad.json"
        bad_path.write_text("not json", encoding="utf-8")
        result = load_prior_slate(bad_path)
        assert result is None
