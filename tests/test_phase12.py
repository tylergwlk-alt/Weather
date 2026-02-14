"""Phase 12 — Hardening & Ops tests.

Task 48: No-execution guardrail audit
Task 49: Logging & error handling
Task 50: Rate limiting & retry
Task 51: Documentation (runbook existence)
"""

from __future__ import annotations

import ast
import inspect
import time
from pathlib import Path
from unittest.mock import MagicMock

import httpx
import pytest

from kalshi_weather.config import DEFAULT_CONFIG, RateLimitConfig
from kalshi_weather.rate_limiter import (
    RateLimiter,
    compute_backoff_delay,
    is_retryable_error,
    request_with_retry,
)

SRC_DIR = Path(__file__).resolve().parent.parent / "src" / "kalshi_weather"


# ── Task 48: No-execution guardrail audit ────────────────────────────


class TestNoExecutionGuardrails:
    """Verify no order placement code exists anywhere."""

    def test_kalshi_client_has_no_post_method(self):
        """KalshiClient must not have any POST/PUT/DELETE/PATCH methods."""
        from kalshi_weather.kalshi_client import KalshiClient

        for name in dir(KalshiClient):
            method = getattr(KalshiClient, name)
            if callable(method) and not name.startswith("_"):
                src = inspect.getsource(method)
                for verb in ["POST", "PUT", "DELETE", "PATCH"]:
                    assert verb not in src, (
                        f"KalshiClient.{name} contains HTTP verb {verb}"
                    )

    def test_kalshi_client_only_get(self):
        """Only _get() exists as HTTP method — no _post, _put, etc."""
        from kalshi_weather.kalshi_client import KalshiClient

        http_methods = [
            m for m in dir(KalshiClient)
            if m.startswith("_") and m[1:] in ("post", "put", "delete", "patch")
        ]
        assert http_methods == [], f"Found write methods: {http_methods}"

    def test_path_allowlist_enforced(self):
        """KalshiClient._get() rejects paths not in allowlist."""
        from kalshi_weather.kalshi_client import KalshiClient

        # Create a client with a dummy key — we'll never actually call the API.
        client = object.__new__(KalshiClient)
        client._base_url = "https://example.com/trade-api/v2"
        client._client = MagicMock()
        client._api_key_id = "test"
        client._private_key = MagicMock()
        client._rate_limiter = RateLimiter(100)
        client._config = DEFAULT_CONFIG

        with pytest.raises(PermissionError, match="Path not allowed"):
            client._get("/trade-api/v2/portfolio/orders")

    def test_no_order_endpoints_in_source(self):
        """Scan all source files for order-related endpoint strings."""
        order_patterns = [
            "/orders", "/portfolio", "/positions",
            "/submit", "/cancel",
        ]
        for py_file in SRC_DIR.glob("*.py"):
            source = py_file.read_text(encoding="utf-8")
            for pattern in order_patterns:
                # Skip if it's just in a comment or test reference
                for i, line in enumerate(source.splitlines(), 1):
                    stripped = line.strip()
                    if stripped.startswith("#"):
                        continue
                    assert pattern not in stripped, (
                        f"{py_file.name}:{i} contains '{pattern}'"
                    )

    def test_no_httpx_post_put_delete_calls(self):
        """No source file calls client.post/put/delete/patch."""
        write_calls = [".post(", ".put(", ".delete(", ".patch("]
        for py_file in SRC_DIR.glob("*.py"):
            source = py_file.read_text(encoding="utf-8")
            for call in write_calls:
                for i, line in enumerate(source.splitlines(), 1):
                    stripped = line.strip()
                    if stripped.startswith("#"):
                        continue
                    assert call not in stripped, (
                        f"{py_file.name}:{i} uses {call}"
                    )


# ── Task 49: Logging & error handling ────────────────────────────────


class TestLogging:
    """Verify logging is set up across modules."""

    @pytest.mark.parametrize("module_name", [
        "kalshi_client", "scanner", "weather_api", "modeler",
        "team_lead", "orchestrator", "output", "risk", "backtest",
        "rate_limiter", "accountant", "planner", "rules", "artifacts",
    ])
    def test_module_has_logger(self, module_name):
        """Each module should have a logger configured."""
        py_file = SRC_DIR / f"{module_name}.py"
        source = py_file.read_text(encoding="utf-8")
        assert "import logging" in source, (
            f"{module_name}.py missing 'import logging'"
        )
        assert "getLogger" in source, (
            f"{module_name}.py missing logger = logging.getLogger()"
        )

    def test_no_print_statements(self):
        """No source file should use print() — use logging instead."""
        for py_file in SRC_DIR.glob("*.py"):
            if py_file.name == "__init__.py":
                continue
            source = py_file.read_text(encoding="utf-8")
            tree = ast.parse(source)
            for node in ast.walk(tree):
                if isinstance(node, ast.Call):
                    func = node.func
                    if isinstance(func, ast.Name) and func.id == "print":
                        pytest.fail(
                            f"{py_file.name}:{node.lineno} uses print()"
                        )

    def test_no_bare_except(self):
        """No bare 'except:' without Exception type."""
        for py_file in SRC_DIR.glob("*.py"):
            source = py_file.read_text(encoding="utf-8")
            tree = ast.parse(source)
            for node in ast.walk(tree):
                if isinstance(node, ast.ExceptHandler):
                    # bare except has type=None
                    if node.type is None:
                        pytest.fail(
                            f"{py_file.name}:{node.lineno} has bare except:"
                        )


# ── Task 50: Rate limiting & retry ──────────────────────────────────


class TestRateLimiter:
    def test_rate_limiter_throttles(self):
        """RateLimiter enforces minimum interval between calls."""
        rl = RateLimiter(requests_per_second=10.0)  # 100ms between
        rl.wait()
        t0 = time.monotonic()
        rl.wait()
        elapsed = time.monotonic() - t0
        assert elapsed >= 0.09  # At least ~100ms

    def test_rate_limiter_zero_rps(self):
        """RateLimiter with 0 rps should not block."""
        rl = RateLimiter(requests_per_second=0)
        t0 = time.monotonic()
        rl.wait()
        rl.wait()
        assert time.monotonic() - t0 < 0.05

    def test_backoff_delay_increases(self):
        """Backoff delay should increase exponentially."""
        cfg = RateLimitConfig(
            retry_base_delay_seconds=1.0,
            retry_jitter_seconds=0.0,
            retry_max_delay_seconds=60.0,
        )
        d0 = compute_backoff_delay(0, cfg)
        d1 = compute_backoff_delay(1, cfg)
        d2 = compute_backoff_delay(2, cfg)
        assert d0 == 1.0
        assert d1 == 2.0
        assert d2 == 4.0

    def test_backoff_delay_capped(self):
        """Backoff delay should not exceed max."""
        cfg = RateLimitConfig(
            retry_base_delay_seconds=1.0,
            retry_jitter_seconds=0.0,
            retry_max_delay_seconds=5.0,
        )
        d10 = compute_backoff_delay(10, cfg)
        assert d10 == 5.0

    def test_backoff_has_jitter(self):
        """With jitter enabled, delays should vary."""
        cfg = RateLimitConfig(
            retry_base_delay_seconds=1.0,
            retry_jitter_seconds=1.0,
            retry_max_delay_seconds=60.0,
        )
        delays = {compute_backoff_delay(0, cfg) for _ in range(20)}
        assert len(delays) > 1  # Should not all be identical

    def test_retryable_timeout(self):
        """Timeout errors should be retryable."""
        exc = httpx.TimeoutException("timed out")
        assert is_retryable_error(exc) is True

    def test_retryable_connect_error(self):
        """Connection errors should be retryable."""
        exc = httpx.ConnectError("connection refused")
        assert is_retryable_error(exc) is True

    def test_retryable_500(self):
        """500 status should be retryable."""
        resp = MagicMock()
        resp.status_code = 500
        exc = httpx.HTTPStatusError("error", request=MagicMock(), response=resp)
        assert is_retryable_error(exc) is True

    def test_not_retryable_400(self):
        """400 status should NOT be retryable."""
        resp = MagicMock()
        resp.status_code = 400
        exc = httpx.HTTPStatusError("error", request=MagicMock(), response=resp)
        assert is_retryable_error(exc) is False

    def test_not_retryable_403(self):
        """403 status should NOT be retryable."""
        resp = MagicMock()
        resp.status_code = 403
        exc = httpx.HTTPStatusError("error", request=MagicMock(), response=resp)
        assert is_retryable_error(exc) is False

    def test_retryable_429(self):
        """429 (rate limited) should be retryable."""
        resp = MagicMock()
        resp.status_code = 429
        exc = httpx.HTTPStatusError("error", request=MagicMock(), response=resp)
        assert is_retryable_error(exc) is True

    def test_request_with_retry_success(self):
        """Successful request on first try returns response."""
        client = MagicMock()
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        client.request.return_value = resp

        result = request_with_retry(client, "GET", "http://example.com")
        assert result is resp
        assert client.request.call_count == 1

    def test_request_with_retry_retries_on_500(self):
        """Should retry on 500 and succeed on second attempt."""
        client = MagicMock()

        bad_resp = MagicMock()
        bad_resp.status_code = 500
        bad_resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            "500", request=MagicMock(), response=bad_resp,
        )

        good_resp = MagicMock()
        good_resp.raise_for_status = MagicMock()

        client.request.side_effect = [bad_resp, good_resp]

        cfg = RateLimitConfig(
            retry_max_attempts=3,
            retry_base_delay_seconds=0.01,
            retry_jitter_seconds=0.0,
            retry_max_delay_seconds=0.1,
        )
        result = request_with_retry(
            client, "GET", "http://example.com", config=cfg,
        )
        assert result is good_resp
        assert client.request.call_count == 2

    def test_request_with_retry_exhausts_retries(self):
        """Should raise after exhausting all retries."""
        client = MagicMock()

        bad_resp = MagicMock()
        bad_resp.status_code = 500
        bad_resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            "500", request=MagicMock(), response=bad_resp,
        )
        client.request.return_value = bad_resp

        cfg = RateLimitConfig(
            retry_max_attempts=2,
            retry_base_delay_seconds=0.01,
            retry_jitter_seconds=0.0,
            retry_max_delay_seconds=0.1,
        )
        with pytest.raises(httpx.HTTPStatusError):
            request_with_retry(
                client, "GET", "http://example.com", config=cfg,
            )
        assert client.request.call_count == 2

    def test_request_no_retry_on_400(self):
        """Should NOT retry on 400 — raises immediately."""
        client = MagicMock()

        bad_resp = MagicMock()
        bad_resp.status_code = 400
        bad_resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            "400", request=MagicMock(), response=bad_resp,
        )
        client.request.return_value = bad_resp

        cfg = RateLimitConfig(retry_max_attempts=3)
        with pytest.raises(httpx.HTTPStatusError):
            request_with_retry(
                client, "GET", "http://example.com", config=cfg,
            )
        assert client.request.call_count == 1  # No retry


class TestRateLimitConfig:
    def test_default_config_has_rate_limit(self):
        """DEFAULT_CONFIG should include rate_limit settings."""
        assert hasattr(DEFAULT_CONFIG, "rate_limit")
        assert DEFAULT_CONFIG.rate_limit.kalshi_requests_per_second > 0
        assert DEFAULT_CONFIG.rate_limit.nws_requests_per_second > 0
        assert DEFAULT_CONFIG.rate_limit.retry_max_attempts >= 1

    def test_kalshi_client_has_rate_limiter(self):
        """KalshiClient should accept config and create rate limiter."""
        from kalshi_weather.kalshi_client import KalshiClient

        # Check the __init__ signature accepts config
        sig = inspect.signature(KalshiClient.__init__)
        assert "config" in sig.parameters

    def test_weather_api_has_rate_limiter(self):
        """WeatherAPI should accept config and create rate limiter."""
        from kalshi_weather.weather_api import WeatherAPI

        sig = inspect.signature(WeatherAPI.__init__)
        assert "config" in sig.parameters


# ── Task 51: Documentation ──────────────────────────────────────────


PROJECT_ROOT = Path(__file__).resolve().parent.parent


class TestDocumentation:
    def test_roadmap_exists(self):
        """ROADMAP.md should exist at project root."""
        assert (PROJECT_ROOT / "ROADMAP.md").is_file()

    def test_runbook_exists(self):
        """RUNBOOK.md should exist at project root."""
        assert (PROJECT_ROOT / "RUNBOOK.md").is_file()

    def test_runbook_has_required_sections(self):
        """RUNBOOK.md should cover key operational topics."""
        content = (PROJECT_ROOT / "RUNBOOK.md").read_text(encoding="utf-8")
        required = [
            "Quick Start",
            "Daily Operations",
            "Failure Modes",
            "Configuration",
            "Troubleshooting",
        ]
        for section in required:
            assert section in content, (
                f"RUNBOOK.md missing section: {section}"
            )

    def test_pyproject_has_metadata(self):
        """pyproject.toml should have name, version, description."""
        content = (PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8")
        assert 'name = "kalshi-weather"' in content
        assert "version" in content
        assert "description" in content

    def test_roadmap_phase12_documented(self):
        """ROADMAP.md should show Phase 12 as complete."""
        content = (PROJECT_ROOT / "ROADMAP.md").read_text(encoding="utf-8")
        assert "Phase 12" in content
