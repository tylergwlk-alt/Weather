"""Rate limiting and retry logic for API clients.

Provides a token-bucket rate limiter and exponential backoff with jitter
for transient HTTP failures (5xx, timeouts, connection errors).
"""

from __future__ import annotations

import logging
import random
import time
from typing import Optional

import httpx

from kalshi_weather.config import DEFAULT_CONFIG, RateLimitConfig

logger = logging.getLogger(__name__)

# HTTP status codes that warrant a retry.
_RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}


class RateLimiter:
    """Token-bucket rate limiter for API requests."""

    def __init__(self, requests_per_second: float) -> None:
        self._min_interval = 1.0 / requests_per_second if requests_per_second > 0 else 0
        self._last_request_time: float = 0.0

    def wait(self) -> None:
        """Block until we're allowed to make the next request."""
        if self._min_interval <= 0:
            return
        now = time.monotonic()
        elapsed = now - self._last_request_time
        if elapsed < self._min_interval:
            sleep_time = self._min_interval - elapsed
            time.sleep(sleep_time)
        self._last_request_time = time.monotonic()


def compute_backoff_delay(
    attempt: int,
    config: RateLimitConfig = DEFAULT_CONFIG.rate_limit,
) -> float:
    """Compute exponential backoff delay with jitter.

    delay = min(base * 2^attempt + jitter, max_delay)
    """
    base = config.retry_base_delay_seconds * (2 ** attempt)
    jitter = random.uniform(0, config.retry_jitter_seconds)
    return min(base + jitter, config.retry_max_delay_seconds)


def is_retryable_error(exc: Exception) -> bool:
    """Check if an exception is retryable (transient)."""
    if isinstance(exc, httpx.TimeoutException):
        return True
    if isinstance(exc, httpx.ConnectError):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in _RETRYABLE_STATUS_CODES
    return False


def request_with_retry(
    client: httpx.Client,
    method: str,
    url: str,
    *,
    headers: Optional[dict] = None,
    params: Optional[dict] = None,
    rate_limiter: Optional[RateLimiter] = None,
    config: RateLimitConfig = DEFAULT_CONFIG.rate_limit,
) -> httpx.Response:
    """Make an HTTP request with rate limiting and retry logic.

    Parameters
    ----------
    client : httpx.Client
    method : HTTP method (GET, etc.)
    url : full URL
    headers : optional extra headers
    params : optional query parameters
    rate_limiter : RateLimiter instance (applied before each attempt)
    config : retry configuration

    Returns
    -------
    httpx.Response on success

    Raises
    ------
    The last exception if all retries are exhausted.
    """
    last_exc: Optional[Exception] = None

    for attempt in range(config.retry_max_attempts):
        if rate_limiter is not None:
            rate_limiter.wait()

        try:
            resp = client.request(method, url, headers=headers, params=params)
            resp.raise_for_status()
            return resp
        except Exception as exc:
            last_exc = exc
            if not is_retryable_error(exc) or attempt >= config.retry_max_attempts - 1:
                raise

            delay = compute_backoff_delay(attempt, config)
            logger.warning(
                "Request to %s failed (attempt %d/%d): %s — retrying in %.1fs",
                url, attempt + 1, config.retry_max_attempts, exc, delay,
            )
            time.sleep(delay)

    raise last_exc  # type: ignore[misc]  # pragma: no cover
