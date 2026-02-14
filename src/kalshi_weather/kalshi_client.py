"""Read-only Kalshi API client with RSA-PSS authentication.

ABSOLUTE RULE: This client must NEVER expose order placement, cancellation,
or any write operation. It is read-only by design.
"""

from __future__ import annotations

import base64
import logging
import time
from pathlib import Path
from typing import Any, Optional

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

from kalshi_weather.config import DEFAULT_CONFIG, Config
from kalshi_weather.rate_limiter import RateLimiter, request_with_retry

logger = logging.getLogger(__name__)

PROD_BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"
DEMO_BASE_URL = "https://demo-api.kalshi.co/trade-api/v2"

# Safety: these path prefixes are the ONLY ones we ever call.
_ALLOWED_PATH_PREFIXES = (
    "/trade-api/v2/events",
    "/trade-api/v2/markets",
    "/trade-api/v2/series",
)


class KalshiClient:
    """Authenticated, read-only Kalshi API client.

    Authentication uses RSA-PSS signed headers per Kalshi docs:
    - KALSHI-ACCESS-KEY: API key ID
    - KALSHI-ACCESS-TIMESTAMP: request time in ms
    - KALSHI-ACCESS-SIGNATURE: base64(RSA-PSS(timestamp + method + path))
    """

    def __init__(
        self,
        api_key_id: str,
        private_key_path: str | Path,
        base_url: str = PROD_BASE_URL,
        timeout: float = 30.0,
        config: Config = DEFAULT_CONFIG,
    ) -> None:
        self._api_key_id = api_key_id
        self._private_key = self._load_private_key(Path(private_key_path))
        self._base_url = base_url.rstrip("/")
        self._client = httpx.Client(timeout=timeout)
        self._config = config
        self._rate_limiter = RateLimiter(config.rate_limit.kalshi_requests_per_second)
        logger.info("KalshiClient initialized (base_url=%s)", base_url)

    @staticmethod
    def _load_private_key(path: Path):
        raw = path.read_bytes()
        return serialization.load_pem_private_key(raw, password=None)

    def _sign(self, timestamp_ms: str, method: str, path: str) -> str:
        """Create RSA-PSS signature for a request."""
        # Strip query params for signing per Kalshi docs.
        path_no_query = path.split("?")[0]
        message = f"{timestamp_ms}{method}{path_no_query}".encode("utf-8")
        signature = self._private_key.sign(
            message,
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.DIGEST_LENGTH,
            ),
            hashes.SHA256(),
        )
        return base64.b64encode(signature).decode("utf-8")

    def _auth_headers(self, method: str, path: str) -> dict[str, str]:
        ts = str(int(time.time() * 1000))
        sig = self._sign(ts, method, path)
        return {
            "KALSHI-ACCESS-KEY": self._api_key_id,
            "KALSHI-ACCESS-TIMESTAMP": ts,
            "KALSHI-ACCESS-SIGNATURE": sig,
        }

    def _get(self, path: str, params: Optional[dict[str, Any]] = None) -> dict:
        """Execute an authenticated GET request. Only GET is allowed."""
        # Safety: block any path that isn't in our allowlist.
        path_no_query = path.split("?")[0]
        if not any(path_no_query.startswith(pfx) for pfx in _ALLOWED_PATH_PREFIXES):
            raise PermissionError(f"Path not allowed: {path_no_query}")

        headers = self._auth_headers("GET", path)
        url = f"{self._base_url.rsplit('/trade-api/v2', 1)[0]}{path}"
        logger.debug("GET %s", path)
        resp = request_with_retry(
            self._client, "GET", url,
            headers=headers, params=params,
            rate_limiter=self._rate_limiter,
            config=self._config.rate_limit,
        )
        return resp.json()

    def close(self) -> None:
        self._client.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # ── Public read-only methods ───────────────────────────────────────

    def get_series_list(
        self,
        category: Optional[str] = None,
        tags: Optional[str] = None,
    ) -> list[dict]:
        """List series, optionally filtered by category or tags."""
        params: dict[str, Any] = {}
        if category:
            params["category"] = category
        if tags:
            params["tags"] = tags
        data = self._get("/trade-api/v2/series", params=params or None)
        return data.get("series", [])

    def get_events(
        self,
        series_ticker: Optional[str] = None,
        status: str = "open",
        with_nested_markets: bool = True,
        limit: int = 200,
        cursor: str = "",
    ) -> tuple[list[dict], str]:
        """Fetch events with pagination. Returns (events, next_cursor)."""
        params: dict[str, Any] = {
            "status": status,
            "with_nested_markets": str(with_nested_markets).lower(),
            "limit": limit,
        }
        if series_ticker:
            params["series_ticker"] = series_ticker
        if cursor:
            params["cursor"] = cursor
        data = self._get("/trade-api/v2/events", params=params)
        return data.get("events", []), data.get("cursor", "")

    def get_all_events(
        self,
        series_ticker: Optional[str] = None,
        status: str = "open",
        with_nested_markets: bool = True,
    ) -> list[dict]:
        """Fetch all events across pages for a given series."""
        all_events: list[dict] = []
        cursor = ""
        while True:
            events, cursor = self.get_events(
                series_ticker=series_ticker,
                status=status,
                with_nested_markets=with_nested_markets,
                cursor=cursor,
            )
            all_events.extend(events)
            if not cursor or not events:
                break
        return all_events

    def get_event(
        self,
        event_ticker: str,
        with_nested_markets: bool = True,
    ) -> dict:
        """Fetch a single event by ticker."""
        params = {"with_nested_markets": str(with_nested_markets).lower()}
        data = self._get(f"/trade-api/v2/events/{event_ticker}", params=params)
        return data

    def get_markets(
        self,
        event_ticker: Optional[str] = None,
        series_ticker: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 200,
        cursor: str = "",
    ) -> tuple[list[dict], str]:
        """Fetch markets with optional filters."""
        params: dict[str, Any] = {"limit": limit}
        if event_ticker:
            params["event_ticker"] = event_ticker
        if series_ticker:
            params["series_ticker"] = series_ticker
        if status:
            params["status"] = status
        if cursor:
            params["cursor"] = cursor
        data = self._get("/trade-api/v2/markets", params=params)
        return data.get("markets", []), data.get("cursor", "")

    def get_orderbook(self, ticker: str, depth: int = 10) -> dict:
        """Fetch the orderbook for a market.

        Returns the raw API response with 'orderbook' containing
        'yes' and 'no' arrays of [price_cents, quantity] pairs.
        """
        params: dict[str, Any] = {"depth": depth}
        data = self._get(f"/trade-api/v2/markets/{ticker}/orderbook", params=params)
        return data
