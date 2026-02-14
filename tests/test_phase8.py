"""Phase 8 tests — Team Lead Merge & Bucket Logic."""

from __future__ import annotations

from kalshi_weather.schemas import (
    Accounting,
    Bucket,
    CandidateRaw,
    KnifeEdgeRisk,
    LockInFlag,
    MappingConfidence,
    MarketType,
    ModelOutput,
    OrderbookSnapshot,
    SettlementSpec,
    UncertaintyLevel,
    UnifiedCandidate,
)
from kalshi_weather.team_lead import (
    apply_hard_rejects,
    classify_bucket,
    enforce_pick_counts,
    merge_candidate,
    rank_candidates,
    run_bucket_pipeline,
)

# ── Helpers ────────────────────────────────────────────────────────────

def _raw(
    ticker="TEST-T50",
    city="Chicago",
    ask=91,
    bid=88,
    room=3,
) -> CandidateRaw:
    return CandidateRaw(
        run_time_et="2026-02-12 07:00 ET",
        target_date_local="2026-02-12",
        city=city,
        market_type=MarketType.HIGH_TEMP,
        event_name="KXHIGHCHI",
        market_ticker=ticker,
        market_url=f"https://kalshi.com/markets/{ticker}",
        bracket_definition="50°F or above",
        orderbook_snapshot=OrderbookSnapshot(
            best_yes_bid_cents=100 - ask if ask else None,
            best_no_bid_cents=bid,
            implied_best_no_ask_cents=ask,
            implied_best_yes_ask_cents=100 - bid if bid else None,
            bid_room_cents=room,
            top3_yes_bids=[[9, 30], [8, 20]],
            top3_no_bids=[[88, 40], [87, 20]],
        ),
    )


def _settlement(confidence=MappingConfidence.HIGH) -> SettlementSpec:
    return SettlementSpec(
        city="Chicago",
        market_type=MarketType.HIGH_TEMP,
        issuedby="ORD",
        cli_url="https://forecast.weather.gov/product.php?issuedby=ORD&product=CLI",
        what_to_read_in_cli="MAXIMUM TEMPERATURE",
        day_window_note="CST midnight-midnight",
        mapping_confidence=confidence,
    )


def _model(
    uncertainty=UncertaintyLevel.LOW,
    knife_edge=KnifeEdgeRisk.LOW,
    hours_vol=6.0,
    lock_low=None,
    lock_high=None,
    p_low=None,
    p_high=None,
) -> ModelOutput:
    return ModelOutput(
        market_ticker="TEST-T50",
        p_yes=0.05,
        p_no=0.95,
        method="test",
        uncertainty_level=uncertainty,
        local_time_at_station="2026-02-12 07:00 CST",
        hours_remaining_until_cli_day_close=17.0,
        hours_remaining_in_meaningful_volatility_window=hours_vol,
        knife_edge_risk=knife_edge,
        lock_in_flag_if_low=lock_low,
        high_lock_in_flag=lock_high,
        p_new_lower_low_after_now=p_low,
        p_new_higher_high_after_now=p_high,
    )


def _accounting(ev=5.0, edge=4.0, no_trade_reason=None) -> Accounting:
    return Accounting(
        market_ticker="TEST-T50",
        implied_p_no_from_implied_ask=0.91,
        fee_est_cents_per_contract=1,
        ev_net_est_cents_at_recommended_limit=ev,
        max_buy_price_no_cents=93,
        edge_vs_implied_pct=edge,
        no_trade_reason_if_any=no_trade_reason,
    )


def _unified(
    ticker="TEST-T50",
    city="Chicago",
    ask=91,
    bid=88,
    room=3,
    confidence=MappingConfidence.HIGH,
    ev=5.0,
    uncertainty=UncertaintyLevel.LOW,
    knife_edge=KnifeEdgeRisk.LOW,
    hours_vol=6.0,
    no_trade_reason=None,
    lock_low=None,
    lock_high=None,
    p_low=None,
    p_high=None,
) -> UnifiedCandidate:
    raw = _raw(ticker, city, ask, bid, room)
    return merge_candidate(
        raw,
        settlement=_settlement(confidence),
        model=_model(uncertainty, knife_edge, hours_vol, lock_low, lock_high, p_low, p_high),
        accounting=_accounting(ev, no_trade_reason=no_trade_reason),
    )


# ── Merge (Task #33) ────────────────────────────────────────────────

class TestMergeCandidate:
    def test_basic_merge(self):
        raw = _raw()
        uc = merge_candidate(raw, settlement=_settlement(), model=_model())
        assert uc.market_ticker == "TEST-T50"
        assert uc.city == "Chicago"
        assert uc.settlement_spec is not None
        assert uc.model is not None
        assert uc.bucket == Bucket.REJECTED  # default before classification

    def test_merge_without_optional_fields(self):
        raw = _raw()
        uc = merge_candidate(raw)
        assert uc.settlement_spec is None
        assert uc.model is None
        assert uc.fees_ev is None
        assert uc.manual_trade_plan is None
        assert uc.allocation is None

    def test_merge_preserves_raw_fields(self):
        raw = _raw(ticker="ABC-T99", city="Miami")
        uc = merge_candidate(raw)
        assert uc.market_ticker == "ABC-T99"
        assert uc.city == "Miami"
        assert uc.run_time_et == raw.run_time_et
        assert uc.bracket_definition == raw.bracket_definition


# ── Hard rejects (Task #34) ─────────────────────────────────────────

class TestHardRejects:
    def test_clean_candidate_passes(self):
        uc = _unified()
        rejected, reason = apply_hard_rejects(uc)
        assert not rejected
        assert reason == ""

    def test_low_mapping_confidence(self):
        uc = _unified(confidence=MappingConfidence.LOW)
        rejected, reason = apply_hard_rejects(uc)
        assert rejected
        assert "Mapping confidence" in reason

    def test_med_mapping_confidence(self):
        uc = _unified(confidence=MappingConfidence.MED)
        rejected, reason = apply_hard_rejects(uc)
        assert rejected

    def test_missing_implied_ask(self):
        uc = _unified(ask=None, bid=None, room=None)
        rejected, reason = apply_hard_rejects(uc)
        assert rejected
        assert "implied_best_no_ask" in reason

    def test_negative_ev(self):
        uc = _unified(ev=-2.0, no_trade_reason="Negative EV")
        rejected, reason = apply_hard_rejects(uc)
        assert rejected
        assert "EV reject" in reason

    def test_spread_reject(self):
        uc = _unified(room=8)
        rejected, reason = apply_hard_rejects(uc)
        assert rejected
        assert "Spread reject" in reason

    def test_low_lock_in(self):
        uc = _unified(lock_low=LockInFlag.LOCKING, p_low=0.02)
        rejected, reason = apply_hard_rejects(uc)
        assert rejected
        assert "LOW lock-in" in reason

    def test_high_lock_in(self):
        uc = _unified(lock_high=LockInFlag.LOCKING, p_high=0.03)
        rejected, reason = apply_hard_rejects(uc)
        assert rejected
        assert "HIGH lock-in" in reason

    def test_lock_in_not_triggered_above_threshold(self):
        uc = _unified(lock_low=LockInFlag.LOCKING, p_low=0.10)
        rejected, reason = apply_hard_rejects(uc)
        assert not rejected

    def test_lock_in_not_triggered_when_not_locking(self):
        uc = _unified(lock_low=LockInFlag.NOT_LOCKED, p_low=0.01)
        rejected, reason = apply_hard_rejects(uc)
        assert not rejected


# ── Bucket classification (Task #35) ────────────────────────────────

class TestBucketClassifier:
    def test_primary(self):
        uc = _unified(ask=91, room=3)
        bucket, reason = classify_bucket(uc)
        assert bucket == Bucket.PRIMARY
        assert "91" in reason

    def test_primary_at_boundaries(self):
        for ask in [90, 93]:
            uc = _unified(ask=ask, room=2)
            bucket, _ = classify_bucket(uc)
            assert bucket == Bucket.PRIMARY

    def test_tight(self):
        uc = _unified(ask=91, room=1)
        bucket, reason = classify_bucket(uc)
        assert bucket == Bucket.TIGHT
        assert "room=1" in reason

    def test_tight_zero_room(self):
        uc = _unified(ask=92, room=0)
        bucket, _ = classify_bucket(uc)
        assert bucket == Bucket.TIGHT

    def test_near_miss_low_band(self):
        for ask in [88, 89]:
            uc = _unified(ask=ask, room=3)
            bucket, reason = classify_bucket(uc)
            assert bucket == Bucket.NEAR_MISS
            assert "near-miss low" in reason

    def test_near_miss_high_band(self):
        for ask in [94, 95]:
            uc = _unified(ask=ask, room=3)
            bucket, reason = classify_bucket(uc)
            assert bucket == Bucket.NEAR_MISS
            assert "near-miss high" in reason

    def test_rejected_below_scan(self):
        uc = _unified(ask=85)
        bucket, reason = classify_bucket(uc)
        assert bucket == Bucket.REJECTED
        assert "outside" in reason

    def test_rejected_above_scan(self):
        uc = _unified(ask=97)
        bucket, reason = classify_bucket(uc)
        assert bucket == Bucket.REJECTED

    def test_no_ask(self):
        uc = _unified(ask=None)
        bucket, _ = classify_bucket(uc)
        assert bucket == Bucket.REJECTED


# ── Ranking (Task #36) ──────────────────────────────────────────────

class TestRanking:
    def test_higher_ev_ranks_first(self):
        c1 = _unified(ticker="HIGH_EV", ev=10.0)
        c2 = _unified(ticker="LOW_EV", ev=2.0)
        ranked = rank_candidates([c2, c1])
        assert ranked[0].market_ticker == "HIGH_EV"
        assert ranked[0].rank == 1
        assert ranked[1].rank == 2

    def test_uncertainty_breaks_tie(self):
        c1 = _unified(ticker="LOW_UNC", ev=5.0, uncertainty=UncertaintyLevel.LOW)
        c2 = _unified(ticker="HIGH_UNC", ev=5.0, uncertainty=UncertaintyLevel.HIGH)
        ranked = rank_candidates([c2, c1])
        assert ranked[0].market_ticker == "LOW_UNC"

    def test_knife_edge_breaks_tie(self):
        c1 = _unified(ticker="LOW_KE", ev=5.0, knife_edge=KnifeEdgeRisk.LOW)
        c2 = _unified(ticker="HIGH_KE", ev=5.0, knife_edge=KnifeEdgeRisk.HIGH)
        ranked = rank_candidates([c2, c1])
        assert ranked[0].market_ticker == "LOW_KE"

    def test_rank_assignment(self):
        candidates = [_unified(ticker=f"T{i}", ev=float(10 - i)) for i in range(5)]
        ranked = rank_candidates(candidates)
        for i, c in enumerate(ranked, 1):
            assert c.rank == i


# ── Pick count enforcement (Task #37) ───────────────────────────────

class TestPickCounts:
    def test_under_limit(self):
        primary = [_unified(ticker=f"P{i}") for i in range(5)]
        tight = [_unified(ticker="T1")]
        near_miss = [_unified(ticker="NM1")]
        p, t, nm = enforce_pick_counts(primary, tight, near_miss)
        assert len(p) == 5
        assert len(t) == 1

    def test_over_limit_demotes(self):
        primary = [_unified(ticker=f"P{i}") for i in range(12)]
        tight = []
        near_miss = []
        p, t, nm = enforce_pick_counts(primary, tight, near_miss)
        assert len(p) == 10
        assert len(t) == 2
        assert t[0].bucket == Bucket.TIGHT
        assert "demoted" in t[0].bucket_reason

    def test_exact_limit(self):
        primary = [_unified(ticker=f"P{i}") for i in range(10)]
        p, t, nm = enforce_pick_counts(primary, [], [])
        assert len(p) == 10
        assert len(t) == 0


# ── Full pipeline ───────────────────────────────────────────────────

class TestFullPipeline:
    def test_mixed_candidates(self):
        candidates = [
            _unified(ticker="PRIMARY1", ask=91, room=3, ev=8.0),
            _unified(ticker="TIGHT1", ask=92, room=1, ev=4.0),
            _unified(ticker="NEARMISS1", ask=89, room=3, ev=3.0),
            _unified(ticker="REJECTED1", ask=85, room=3, ev=1.0),
        ]
        primary, tight, near_miss, rejected = run_bucket_pipeline(candidates)
        assert len(primary) == 1
        assert primary[0].market_ticker == "PRIMARY1"
        assert primary[0].bucket == Bucket.PRIMARY

        assert len(tight) == 1
        assert tight[0].market_ticker == "TIGHT1"

        assert len(near_miss) == 1
        assert near_miss[0].market_ticker == "NEARMISS1"

        assert len(rejected) == 1
        assert rejected[0].market_ticker == "REJECTED1"

    def test_hard_reject_removes_from_primary(self):
        """A candidate in primary window but with negative EV should be rejected."""
        candidates = [
            _unified(ticker="NEG_EV", ask=91, room=3, ev=-2.0, no_trade_reason="Negative EV"),
        ]
        primary, tight, near_miss, rejected = run_bucket_pipeline(candidates)
        assert len(primary) == 0
        assert len(rejected) == 1
        assert "EV reject" in rejected[0].bucket_reason

    def test_ranking_within_primary(self):
        candidates = [
            _unified(ticker="LOW_EV", ask=91, room=3, ev=2.0),
            _unified(ticker="HIGH_EV", ask=92, room=4, ev=10.0),
            _unified(ticker="MED_EV", ask=90, room=2, ev=5.0),
        ]
        primary, _, _, _ = run_bucket_pipeline(candidates)
        assert len(primary) == 3
        assert primary[0].market_ticker == "HIGH_EV"
        assert primary[0].rank == 1

    def test_empty_input(self):
        primary, tight, near_miss, rejected = run_bucket_pipeline([])
        assert primary == []
        assert tight == []
        assert near_miss == []
        assert rejected == []

    def test_all_rejected(self):
        candidates = [
            _unified(ticker="BAD1", ask=91, room=3, ev=-1.0, no_trade_reason="neg"),
            _unified(ticker="BAD2", ask=91, room=3, confidence=MappingConfidence.LOW),
        ]
        primary, tight, near_miss, rejected = run_bucket_pipeline(candidates)
        assert len(primary) == 0
        assert len(rejected) == 2
