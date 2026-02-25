"""Tests for the Spike Monitor system."""

from __future__ import annotations

# ======================================================================
# SpikeConfig Tests
# ======================================================================


class TestSpikeConfig:
    def test_defaults(self):
        from kalshi_weather.spike_config import SpikeConfig

        cfg = SpikeConfig()
        assert cfg.spike_threshold_cents == 20
        assert cfg.window_seconds == 360
        assert cfg.poll_interval_seconds == 30
        assert cfg.burst_count == 5
        assert cfg.burst_interval_seconds == 60
        assert cfg.start_hour_est == 8
        assert cfg.end_hour_est == 23
        assert cfg.cooldown_seconds == 600

    def test_custom_values(self):
        from kalshi_weather.spike_config import SpikeConfig

        cfg = SpikeConfig(spike_threshold_cents=25, window_seconds=300)
        assert cfg.spike_threshold_cents == 25
        assert cfg.window_seconds == 300
