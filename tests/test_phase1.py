"""Phase 1 validation — imports, config defaults, schema construction, artifact output."""


from kalshi_weather.artifacts import write_daily_slate_json, write_report_md
from kalshi_weather.config import DEFAULT_CONFIG, Config
from kalshi_weather.schemas import (
    CandidateRaw,
    DailySlate,
    MarketType,
    OrderbookSnapshot,
    ScanStats,
)


def test_config_defaults():
    c = DEFAULT_CONFIG
    assert c.bankroll.total_usd == 42.0
    assert c.price_window.primary_low == 90
    assert c.price_window.primary_high == 93
    assert c.price_window.scan_low == 88
    assert c.price_window.scan_high == 95
    assert c.spread.max_spread_cents == 6
    assert c.spread.min_bid_room_primary == 2
    assert c.correlation.max_picks_per_correlation_group == 3
    assert c.correlation.max_picks_per_metro_cluster == 2
    assert c.fees.taker_rate == 0.07
    assert c.fees.maker_rate == 0.0175
    assert c.schedule.run_hours_et == (7, 8, 9)
    assert c.picks.max_primary_picks == 10


def test_config_frozen():
    c = Config()
    try:
        c.bankroll = None
        assert False, "Should be frozen"
    except AttributeError:
        pass


def test_candidate_raw_construction():
    c = CandidateRaw(
        run_time_et="2026-02-12T07:00:00-05:00",
        target_date_local="2026-02-12",
        city="Chicago",
        market_type=MarketType.HIGH_TEMP,
        event_name="KTEMP-26FEB12-CHI",
        market_ticker="KTEMP-26FEB12-CHI-T50-H",
        market_url="https://kalshi.com/markets/KTEMP-26FEB12-CHI-T50-H",
        bracket_definition="Will the high be 50F or above?",
        orderbook_snapshot=OrderbookSnapshot(
            best_yes_bid_cents=8,
            best_no_bid_cents=89,
            implied_best_no_ask_cents=92,
            implied_best_yes_ask_cents=11,
            bid_room_cents=3,
        ),
    )
    assert c.city == "Chicago"
    assert c.orderbook_snapshot.implied_best_no_ask_cents == 92
    assert c.orderbook_snapshot.bid_room_cents == 3


def test_daily_slate_empty():
    slate = DailySlate(
        run_time_et="2026-02-12T07:00:00-05:00",
        target_date_local="2026-02-12",
    )
    assert slate.bankroll_usd == 42.0
    assert slate.scan_stats.primary_count == 0
    assert slate.picks_primary == []


def test_write_daily_slate_json(tmp_path):
    slate = DailySlate(
        run_time_et="2026-02-12T07:00:00-05:00",
        target_date_local="2026-02-12",
        scan_stats=ScanStats(events_scanned=5, bracket_markets_scanned=40),
    )
    out = write_daily_slate_json(slate, path=tmp_path / "test_slate.json")
    assert out.exists()
    import json
    data = json.loads(out.read_text())
    assert data["bankroll_usd"] == 42.0
    assert data["scan_stats"]["events_scanned"] == 5


def test_write_report_md(tmp_path):
    slate = DailySlate(
        run_time_et="2026-02-12T07:00:00-05:00",
        target_date_local="2026-02-12",
        scan_stats=ScanStats(
            events_scanned=5,
            bracket_markets_scanned=40,
            primary_count=0,
        ),
    )
    out = write_report_md(slate, path=tmp_path / "test_report.md")
    assert out.exists()
    content = out.read_text()
    assert "Unlikely NO" in content
    assert "No PRIMARY picks" in content
