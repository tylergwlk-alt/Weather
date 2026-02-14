"""Market & Orderbook Scanner — Phase 2 teammate B.

Discovers TODAY's U.S. temperature events, enumerates all bracket sub-markets,
fetches orderbooks, computes implied prices, and filters candidates.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Optional

import pytz

from kalshi_weather.config import DEFAULT_CONFIG, Config
from kalshi_weather.kalshi_client import KalshiClient
from kalshi_weather.schemas import CandidateRaw, MarketType, OrderbookSnapshot

logger = logging.getLogger(__name__)

# Known Kalshi series prefixes for temperature markets.
# KXHIGH* = daily high, KXLOW* = daily low.
_HIGH_SERIES_RE = re.compile(r"^KXHIGH", re.IGNORECASE)
_LOW_SERIES_RE = re.compile(r"^KXLOW", re.IGNORECASE)

# Kalshi market URL template.
_MARKET_URL = "https://kalshi.com/markets/{ticker}"

# Market statuses considered tradable.
_TRADABLE_STATUSES = {"active", "open"}


def _classify_series(series_ticker: str) -> Optional[MarketType]:
    """Determine if a series is HIGH_TEMP or LOW_TEMP from its ticker."""
    if _HIGH_SERIES_RE.match(series_ticker):
        return MarketType.HIGH_TEMP
    if _LOW_SERIES_RE.match(series_ticker):
        return MarketType.LOW_TEMP
    return None


def _extract_city_from_event(event: dict) -> str:
    """Best-effort city extraction from event title or ticker."""
    title = event.get("title", "")
    # Common pattern: "Highest temperature in <City> on ..."
    for pattern in [
        r"(?:Highest|Lowest)\s+temperature\s+in\s+(.+?)\s+(?:on|today)",
        r"(?:Highest|Lowest)\s+temperature\s+in\s+(.+?)$",
    ]:
        m = re.search(pattern, title, re.IGNORECASE)
        if m:
            return m.group(1).strip()
    # Fallback: use event ticker suffix
    ticker = event.get("event_ticker", "")
    return ticker


def _is_today_event(event: dict, today_str: str) -> bool:
    """Check if an event targets today's date.

    Uses strike_date if available, otherwise checks market close times.
    """
    strike = event.get("strike_date")
    if strike:
        # strike_date may be ISO datetime or YYYY-MM-DD
        return strike[:10] == today_str

    # Fallback: check if any nested market closes today or tomorrow
    for mkt in event.get("markets", []):
        close = mkt.get("close_time", "")
        if close and close[:10] >= today_str:
            return True
    return False


def _parse_orderbook(raw_ob: dict) -> OrderbookSnapshot:
    """Parse the Kalshi orderbook response into our schema.

    The API returns:
        orderbook.yes = [[price_cents, qty], ...] sorted ascending (best bid = last)
        orderbook.no  = [[price_cents, qty], ...] sorted ascending (best bid = last)
    """
    ob = raw_ob.get("orderbook", {})
    yes_bids = ob.get("yes") or []
    no_bids = ob.get("no") or []

    best_yes_bid = yes_bids[-1][0] if yes_bids else None
    best_no_bid = no_bids[-1][0] if no_bids else None

    implied_no_ask = (100 - best_yes_bid) if best_yes_bid is not None else None
    implied_yes_ask = (100 - best_no_bid) if best_no_bid is not None else None

    bid_room = None
    if implied_no_ask is not None and best_no_bid is not None:
        bid_room = implied_no_ask - best_no_bid

    # Top-3 bids (highest first)
    top3_yes = [[p, q] for p, q in reversed(yes_bids[-3:])] if yes_bids else []
    top3_no = [[p, q] for p, q in reversed(no_bids[-3:])] if no_bids else []

    depth_parts = []
    if not yes_bids:
        depth_parts.append("NO YES BIDS")
    if not no_bids:
        depth_parts.append("NO NO BIDS")
    total_yes_depth = sum(q for _, q in yes_bids)
    total_no_depth = sum(q for _, q in no_bids)
    depth_parts.append(f"yes_depth={total_yes_depth}, no_depth={total_no_depth}")

    return OrderbookSnapshot(
        best_yes_bid_cents=best_yes_bid,
        best_no_bid_cents=best_no_bid,
        implied_best_no_ask_cents=implied_no_ask,
        implied_best_yes_ask_cents=implied_yes_ask,
        bid_room_cents=bid_room,
        top3_yes_bids=top3_yes,
        top3_no_bids=top3_no,
        depth_notes="; ".join(depth_parts),
    )


def _market_is_tradable(market: dict) -> bool:
    """Check if a market is in a tradable state."""
    status = (market.get("status") or "").lower()
    return status in _TRADABLE_STATUSES


def _bracket_definition(market: dict) -> str:
    """Extract the human-readable bracket definition."""
    # Kalshi uses yes_sub_title / no_sub_title or title.
    yes_sub = market.get("yes_sub_title", "")
    if yes_sub:
        return yes_sub
    return market.get("title", market.get("ticker", ""))


def discover_temperature_series(client: KalshiClient) -> list[dict]:
    """Find all series that are temperature HIGH or LOW."""
    all_series = client.get_series_list()
    temp_series = []
    for s in all_series:
        ticker = s.get("ticker", "")
        if _classify_series(ticker) is not None:
            temp_series.append(s)
    logger.info("Discovered %d temperature series", len(temp_series))
    return temp_series


def scan_today_markets(
    client: KalshiClient,
    config: Config = DEFAULT_CONFIG,
    run_time: Optional[datetime] = None,
) -> list[CandidateRaw]:
    """Full scan: discover series -> events -> brackets -> orderbooks -> filter.

    Returns CandidateRaw objects for all brackets with implied_best_no_ask
    in [scan_low, scan_high].
    """
    if run_time is None:
        run_time = datetime.now(timezone.utc)

    et = pytz.timezone("US/Eastern")
    run_time_et = run_time.astimezone(et)
    run_time_et_str = run_time_et.isoformat()
    today_str = run_time_et.strftime("%Y-%m-%d")

    # Step 1: Discover temperature series
    temp_series = discover_temperature_series(client)
    if not temp_series:
        logger.warning("No temperature series found")
        return []

    candidates: list[CandidateRaw] = []
    events_scanned = 0
    brackets_scanned = 0

    # Step 2: For each series, fetch today's events with nested markets
    for series in temp_series:
        series_ticker = series["ticker"]
        market_type = _classify_series(series_ticker)
        if market_type is None:
            continue

        events = client.get_all_events(
            series_ticker=series_ticker,
            status="open",
            with_nested_markets=True,
        )

        for event in events:
            if not _is_today_event(event, today_str):
                continue

            events_scanned += 1
            city = _extract_city_from_event(event)
            event_name = event.get("event_ticker", "")

            # Step 3: Enumerate all bracket sub-markets
            markets = event.get("markets", [])
            for market in markets:
                brackets_scanned += 1
                ticker = market.get("ticker", "")

                # Step 4: Market status check (Task #9)
                if not _market_is_tradable(market):
                    status_note = f"non-tradable status: {market.get('status')}"
                    logger.debug("Skipping %s — %s", ticker, status_note)
                    continue

                # Step 5: Fetch orderbook
                try:
                    raw_ob = client.get_orderbook(ticker, depth=10)
                except Exception:
                    logger.warning("Failed to fetch orderbook for %s", ticker, exc_info=True)
                    continue

                ob = _parse_orderbook(raw_ob)

                # Step 6: Filter — implied_best_no_ask in [scan_low, scan_high] (Task #10)
                if ob.implied_best_no_ask_cents is None:
                    continue

                if not (
                    config.price_window.scan_low
                    <= ob.implied_best_no_ask_cents
                    <= config.price_window.scan_high
                ):
                    continue

                candidate = CandidateRaw(
                    run_time_et=run_time_et_str,
                    target_date_local=today_str,
                    city=city,
                    market_type=market_type,
                    event_name=event_name,
                    market_ticker=ticker,
                    market_url=_MARKET_URL.format(ticker=ticker),
                    bracket_definition=_bracket_definition(market),
                    orderbook_snapshot=ob,
                    market_status_notes="",
                )
                candidates.append(candidate)
                logger.info(
                    "Candidate: %s  implied_no_ask=%s  bid_room=%s",
                    ticker,
                    ob.implied_best_no_ask_cents,
                    ob.bid_room_cents,
                )

    logger.info(
        "Scan complete: %d events, %d brackets, %d candidates in [%d,%d]",
        events_scanned,
        brackets_scanned,
        len(candidates),
        config.price_window.scan_low,
        config.price_window.scan_high,
    )
    return candidates
