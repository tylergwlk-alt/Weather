"""Phase 4 tests — Probability Modeler."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from kalshi_weather.modeler import (
    _compute_knife_edge,
    _estimate_p_bracket,
    _estimate_p_new_extreme,
    _get_sunrise,
    _normal_cdf,
    _parse_bracket_threshold,
    model_candidate,
)
from kalshi_weather.schemas import (
    CandidateRaw,
    KnifeEdgeRisk,
    LockInFlag,
    MarketType,
    OrderbookSnapshot,
    UncertaintyLevel,
)
from kalshi_weather.weather_api import StationForecast

# ── Helper factories ───────────────────────────────────────────────────

def _make_candidate(
    city="Chicago",
    market_type=MarketType.HIGH_TEMP,
    bracket="50°F or above",
    ticker="KXHIGHCHI-T50",
) -> CandidateRaw:
    return CandidateRaw(
        run_time_et="2026-02-12T07:00:00-05:00",
        target_date_local="2026-02-12",
        city=city,
        market_type=market_type,
        event_name="KXHIGHCHI-26FEB12",
        market_ticker=ticker,
        market_url="https://kalshi.com/markets/" + ticker,
        bracket_definition=bracket,
        orderbook_snapshot=OrderbookSnapshot(
            best_yes_bid_cents=8,
            best_no_bid_cents=89,
            implied_best_no_ask_cents=92,
        ),
    )


def _make_forecast(high_f=45.0, low_f=25.0) -> StationForecast:
    return StationForecast(
        station_icao="KMDW",
        periods=[],
        forecast_high_f=high_f,
        forecast_low_f=low_f,
    )


# ── Bracket parsing ───────────────────────────────────────────────────

class TestParseBracketThreshold:
    def test_above(self):
        assert _parse_bracket_threshold("50°F or above") == 50.0

    def test_below(self):
        assert _parse_bracket_threshold("30°F or below") == 30.0

    def test_between(self):
        assert _parse_bracket_threshold("Between 45°F and 49°F") == 47.0

    def test_simple_number(self):
        assert _parse_bracket_threshold("Temperature 60") == 60.0

    def test_no_number(self):
        assert _parse_bracket_threshold("unknown format") is None


# ── Normal CDF ─────────────────────────────────────────────────────────

class TestNormalCdf:
    def test_at_mean(self):
        assert abs(_normal_cdf(50, 50, 3) - 0.5) < 0.001

    def test_far_above(self):
        assert _normal_cdf(60, 50, 3) > 0.99

    def test_far_below(self):
        assert _normal_cdf(40, 50, 3) < 0.01

    def test_zero_sigma(self):
        assert _normal_cdf(50, 50, 0) == 1.0
        assert _normal_cdf(49, 50, 0) == 0.0


# ── Bracket probability ───────────────────────────────────────────────

class TestEstimatePBracket:
    def test_above_well_below_forecast(self):
        """Bracket '30°F or above' with forecast 50F → P(YES) very high."""
        p_yes, p_no = _estimate_p_bracket("30°F or above", 50.0, uncertainty_sigma=3.0)
        assert p_yes > 0.99
        assert p_no < 0.01

    def test_above_well_above_forecast(self):
        """Bracket '70°F or above' with forecast 50F → P(YES) very low."""
        p_yes, p_no = _estimate_p_bracket("70°F or above", 50.0, uncertainty_sigma=3.0)
        assert p_yes < 0.01
        assert p_no > 0.99

    def test_below_well_above_forecast(self):
        """Bracket '70°F or below' with forecast 50F → P(YES) very high."""
        p_yes, p_no = _estimate_p_bracket("70°F or below", 50.0, uncertainty_sigma=3.0)
        assert p_yes > 0.99

    def test_near_boundary(self):
        """Bracket at forecast → ~50%."""
        p_yes, p_no = _estimate_p_bracket("50°F or above", 50.0, uncertainty_sigma=3.0)
        assert 0.4 < p_yes < 0.6

    def test_probabilities_sum_to_one(self):
        p_yes, p_no = _estimate_p_bracket("45°F or above", 50.0, uncertainty_sigma=3.0)
        assert abs(p_yes + p_no - 1.0) < 0.001


# ── New extreme probability ───────────────────────────────────────────

class TestEstimatePNewExtreme:
    def test_no_time_remaining(self):
        p = _estimate_p_new_extreme(30.0, 25.0, 0.0, is_low=True)
        assert p == 0.0

    def test_lots_of_room_and_time(self):
        p = _estimate_p_new_extreme(35.0, 25.0, 6.0, is_low=True)
        assert p > 0.7

    def test_past_forecast_low(self):
        """Current low already below forecast → unlikely to go lower."""
        p = _estimate_p_new_extreme(20.0, 25.0, 4.0, is_low=True)
        assert p < 0.2

    def test_high_with_room(self):
        p = _estimate_p_new_extreme(40.0, 50.0, 6.0, is_low=False)
        assert p > 0.7


# ── Knife-edge risk ───────────────────────────────────────────────────

class TestKnifeEdge:
    def test_high_risk(self):
        assert _compute_knife_edge("50°F or above", 50.5) == KnifeEdgeRisk.HIGH

    def test_med_risk(self):
        assert _compute_knife_edge("50°F or above", 52.0) == KnifeEdgeRisk.MED

    def test_low_risk(self):
        assert _compute_knife_edge("50°F or above", 60.0) == KnifeEdgeRisk.LOW


# ── Sunrise computation ───────────────────────────────────────────────

class TestSunrise:
    def test_nyc_sunrise_winter(self):
        sunrise = _get_sunrise("KNYC", datetime(2026, 1, 15), "America/New_York")
        assert sunrise is not None
        # NYC sunrise in mid-January is roughly 7:15 AM EST.
        assert 6 <= sunrise.hour <= 8

    def test_unknown_station(self):
        sunrise = _get_sunrise("KXXX", datetime(2026, 1, 15), "America/New_York")
        assert sunrise is None


# ── Full model_candidate ──────────────────────────────────────────────

class TestModelCandidate:
    def test_high_temp_basic(self):
        """Model a HIGH_TEMP candidate with forecast."""
        candidate = _make_candidate(
            city="Chicago",
            market_type=MarketType.HIGH_TEMP,
            bracket="50°F or above",
        )
        forecast = _make_forecast(high_f=45.0)
        # Run at 7 AM ET (12:00 UTC) on Feb 12 — morning, well before peak.
        now = datetime(2026, 2, 12, 12, 0, 0, tzinfo=ZoneInfo("UTC"))

        result = model_candidate(candidate, forecast, current_obs_temp_f=30.0, now_utc=now)

        assert result.market_ticker == "KXHIGHCHI-T50"
        assert 0 < result.p_yes < 1
        assert 0 < result.p_no < 1
        assert abs(result.p_yes + result.p_no - 1.0) < 0.001
        assert result.hours_remaining_until_cli_day_close > 0
        assert result.hours_remaining_in_meaningful_volatility_window > 0
        assert result.high_lock_in_flag == LockInFlag.NOT_LOCKED
        assert result.typical_peak_time_estimate_local is not None
        assert result.knife_edge_risk in KnifeEdgeRisk
        assert "Chicago" in result.local_time_at_station or "CST" in result.local_time_at_station

    def test_low_temp_basic(self):
        """Model a LOW_TEMP candidate with forecast."""
        candidate = _make_candidate(
            city="Chicago",
            market_type=MarketType.LOW_TEMP,
            bracket="20°F or below",
            ticker="KXLOWCHI-T20",
        )
        forecast = _make_forecast(low_f=15.0)
        now = datetime(2026, 2, 12, 12, 0, 0, tzinfo=ZoneInfo("UTC"))

        result = model_candidate(candidate, forecast, current_obs_temp_f=22.0, now_utc=now)

        assert result.lock_in_flag_if_low in (LockInFlag.LOCKING, LockInFlag.NOT_LOCKED)
        assert result.sunrise_estimate_local is not None
        assert result.p_new_lower_low_after_now is not None

    def test_high_lock_in_triggers(self):
        """Past peak+2h with no room → LOCKING."""
        candidate = _make_candidate(
            city="Chicago",
            market_type=MarketType.HIGH_TEMP,
            bracket="50°F or above",
        )
        forecast = _make_forecast(high_f=45.0)
        # 11 PM local (already past peak+2h).
        now = datetime(2026, 2, 13, 5, 0, 0, tzinfo=ZoneInfo("UTC"))

        result = model_candidate(candidate, forecast, current_obs_temp_f=44.0, now_utc=now)

        assert result.high_lock_in_flag == LockInFlag.LOCKING

    def test_low_lock_in_triggers(self):
        """Past sunrise+2h with low already established → LOCKING."""
        candidate = _make_candidate(
            city="New York",
            market_type=MarketType.LOW_TEMP,
            bracket="20°F or below",
            ticker="KXLOWNY-T20",
        )
        forecast = _make_forecast(low_f=22.0)
        # 3 PM ET — well past sunrise+2h.
        now = datetime(2026, 2, 12, 20, 0, 0, tzinfo=ZoneInfo("UTC"))

        result = model_candidate(candidate, forecast, current_obs_temp_f=25.0, now_utc=now)

        assert result.lock_in_flag_if_low == LockInFlag.LOCKING

    def test_no_forecast(self):
        """Without forecast, model should still return valid output."""
        candidate = _make_candidate()
        now = datetime(2026, 2, 12, 12, 0, 0, tzinfo=ZoneInfo("UTC"))

        result = model_candidate(candidate, forecast=None, current_obs_temp_f=None, now_utc=now)

        assert result.p_yes == 0.5
        assert result.p_no == 0.5
        assert result.uncertainty_level == UncertaintyLevel.HIGH
        assert "No-forecast" in result.method

    def test_vol_window_shrinks_sigma(self):
        """Late in the day → sigma shrinks → more confident."""
        candidate = _make_candidate(bracket="45°F or above")
        forecast = _make_forecast(high_f=45.0)

        # Morning — wide sigma.
        morning = datetime(2026, 2, 12, 12, 0, 0, tzinfo=ZoneInfo("UTC"))
        r_morning = model_candidate(candidate, forecast, 30.0, morning)

        # Late afternoon — narrow sigma, past most volatility.
        late = datetime(2026, 2, 12, 23, 0, 0, tzinfo=ZoneInfo("UTC"))
        r_late = model_candidate(candidate, forecast, 44.0, late)

        # Late result should have less volatility window.
        assert r_late.hours_remaining_in_meaningful_volatility_window < \
               r_morning.hours_remaining_in_meaningful_volatility_window
