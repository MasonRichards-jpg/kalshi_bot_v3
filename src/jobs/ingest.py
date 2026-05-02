"""
Market Ingestion Job

This job fetches active markets from the Kalshi API, transforms them into a structured format,
and upserts them into the database.
"""
import asyncio
import time
from datetime import datetime
from typing import Optional, List

from src.clients.kalshi_client import KalshiClient
from src.utils.database import DatabaseManager, Market
from src.config.settings import settings
from src.utils.logging_setup import get_trading_logger
from src.utils.market_prices import is_tradeable_market


async def process_and_queue_markets(
    markets_data: List[dict],
    db_manager: DatabaseManager,
    queue: asyncio.Queue,
    existing_position_market_ids: set,
    logger,
):
    """
    Transforms market data, upserts to DB, and puts eligible markets on the queue.
    """
    markets_to_upsert = []
    for market_data in markets_data:
        # A simple approach is to take the average of bid and ask.
        # Kalshi API v2 uses dollar-denominated fields (yes_bid_dollars, yes_ask_dollars)
        # These are already in dollars (e.g., 0.5000 = $0.50)
        # Fall back to legacy cent-based fields (yes_bid, yes_ask) divided by 100
        if "yes_bid_dollars" in market_data:
            yes_bid = float(market_data.get("yes_bid_dollars", 0) or 0)
            yes_ask = float(market_data.get("yes_ask_dollars", 0) or 0)
            no_bid = float(market_data.get("no_bid_dollars", 0) or 0)
            no_ask = float(market_data.get("no_ask_dollars", 0) or 0)
            yes_price = (yes_bid + yes_ask) / 2
            no_price = (no_bid + no_ask) / 2
        else:
            # Legacy API: values in cents (0-100)
            yes_ask = market_data.get("yes_ask", 0) / 100
            no_ask = market_data.get("no_ask", 0) / 100
            yes_price = (market_data.get("yes_bid", 0) / 100 + yes_ask) / 2
            no_price = (market_data.get("no_bid", 0) / 100 + no_ask) / 2

        # Skip collection/aggregate tickers: both yes_ask and no_ask near $1.00
        # (e.g. KXMVECROSSCATEGORY-*, KXMVESPORTSMULTIGAMEEXTENDED-*).
        # The Kalshi API returns yes_ask=1.0000 and no_ask=1.0000 for these
        # multi-event container markets — they are NOT real binary markets and
        # cannot be traded.
        _COLLECTION_THRESHOLD = 0.98
        if yes_ask >= _COLLECTION_THRESHOLD and no_ask >= _COLLECTION_THRESHOLD:
            logger.warning(
                f"Skipping collection/aggregate market {market_data['ticker']}: "
                f"yes_ask={yes_ask:.4f}, no_ask={no_ask:.4f} "
                f"(both >= {_COLLECTION_THRESHOLD}, not a tradeable binary market)"
            )
            continue

        # Kalshi API v2 uses volume_fp (string/float) instead of volume (int)
        volume = int(float(market_data.get("volume_fp", 0) or market_data.get("volume", 0) or 0))

        has_position = market_data["ticker"] in existing_position_market_ids

        # Skip collection/series tickers — these return $1.00/$1.00 for both
        # sides and are not directly tradeable (see GitHub issue #42).
        # Centralised in is_tradeable_market() so execute.py can reuse the same guard.
        if not is_tradeable_market(market_data):
            logger.debug(f"Skipping collection ticker {market_data['ticker']} (YES_ask={yes_ask}, NO_ask={no_ask})")
            continue

        market = Market(
            market_id=market_data["ticker"],
            title=market_data["title"],
            yes_price=yes_price,
            no_price=no_price,
            volume=volume,
            expiration_ts=int(
                datetime.fromisoformat(
                    market_data["expiration_time"].replace("Z", "+00:00")
                ).timestamp()
            ),
            category=market_data.get("category", "unknown"),
            status=market_data["status"],
            last_updated=datetime.now(),
            has_position=has_position,
        )
        markets_to_upsert.append(market)

    if markets_to_upsert:
        await db_manager.upsert_markets(markets_to_upsert)
        logger.info(f"Successfully upserted {len(markets_to_upsert)} markets.")

        # Primary filtering criteria - MORE PERMISSIVE FOR MORE OPPORTUNITIES!
        min_volume: float = 100.0  # DECREASED: Much lower volume threshold (was 100, keeping low)
        min_volume_for_ai_analysis: float = 150.0  # DECREASED: Lower volume for AI analysis (was 200, now 150)  
        preferred_categories: List[str] = []  # Empty = all categories allowed
        excluded_categories: List[str] = []  # Empty = no categories excluded

        # Enhanced filtering for better opportunities - MORE PERMISSIVE FOR MORE TRADES
        min_price_movement: float = 0.015  # DECREASED: Even lower minimum range (was 0.02, now 1.5¢)
        max_bid_ask_spread: float = 0.20   # INCREASED: Allow even wider spreads (was 0.15, now 20¢)
        min_confidence_for_long_term: float = 0.40  # DECREASED: Lower confidence required (was 0.5, now 40%)

        eligible_markets = [
            m
            for m in markets_to_upsert
            if m.volume >= min_volume
            # REMOVED TIME RESTRICTION - we can now trade markets with ANY deadline!
            # Dynamic exit strategies will handle timing automatically
            and (
                not settings.trading.preferred_categories
                or m.category in settings.trading.preferred_categories
            )
            and m.category not in settings.trading.excluded_categories
        ]

        logger.info(
            f"Found {len(eligible_markets)} eligible markets to process in this batch."
        )
        for market in eligible_markets:
            await queue.put(market)

    else:
        logger.info("No new markets to upsert in this batch.")


async def run_ingestion(
    db_manager: DatabaseManager,
    queue: asyncio.Queue,
    market_ticker: Optional[str] = None,
):
    """
    Main function for the market ingestion job.

    Args:
        db_manager: DatabaseManager instance.
        queue: asyncio.Queue to put ingested markets into.
        market_ticker: Optional specific market ticker to ingest.
    """
    logger = get_trading_logger("market_ingestion")
    logger.info("Starting market ingestion job.", market_ticker=market_ticker)

    kalshi_client = KalshiClient()

    try:
        # Get all market IDs with existing positions
        existing_position_market_ids = await db_manager.get_markets_with_positions()

        if market_ticker:
            logger.info(f"Fetching single market: {market_ticker}")
            market_response = await kalshi_client.get_market(ticker=market_ticker)
            if market_response and "market" in market_response:
                await process_and_queue_markets(
                    [market_response["market"]],
                    db_manager,
                    queue,
                    existing_position_market_ids,
                    logger,
                )
            else:
                logger.warning(f"Could not find market with ticker: {market_ticker}")
        else:
            # Primary: fetch via events API (Kalshi migrated all tickers to KXMVE*,
            # so /markets only returns parlay tickers. Real markets live under events.)
            logger.info("Fetching markets via events API (with nested markets).")
            seen_tickers = set()
            cursor = None
            events_page = 0
            try:
                while True:
                    params = {"status": "open", "limit": 100, "with_nested_markets": "true"}
                    if cursor:
                        params["cursor"] = cursor
                    
                    resp = await kalshi_client._make_authenticated_request(
                        "GET", "/trade-api/v2/events", params=params
                    )
                    events = resp.get("events", [])
                    if not events:
                        break
                    
                    batch = []
                    for event in events:
                        for m in event.get("markets", []):
                            ticker = m.get("ticker", "")
                            if ticker and ticker not in seen_tickers and m.get("status") == "active":
                                seen_tickers.add(ticker)
                                batch.append(m)
                    
                    if batch:
                        logger.info(f"Fetched {len(batch)} active markets from events page {events_page}.")
                        await process_and_queue_markets(
                            batch,
                            db_manager,
                            queue,
                            existing_position_market_ids,
                            logger,
                        )
                    
                    cursor = resp.get("cursor")
                    if not cursor:
                        break
                    events_page += 1
                    # Cap at 20 pages (~2000 events, ~5000+ markets) to avoid
                    # 16+ minute ingestion that blocks the trading cycle.
                    # Most high-volume tradeable markets appear in the first pages.
                    if events_page > 20:
                        logger.info(f"Reached page limit (20), stopping ingestion with {len(seen_tickers)} markets.")
                        break
                    
                    await asyncio.sleep(0.1)
            except Exception as events_err:
                logger.warning(f"Events API failed, falling back to /markets: {events_err}")
            
            # Fallback: also check /markets for anything missed
            if len(seen_tickers) < 100:
                logger.info(f"Few markets from events ({len(seen_tickers)}), also fetching /markets.")
                cursor = None
                while True:
                    response = await kalshi_client.get_markets(limit=100, cursor=cursor)
                    markets_page = response.get("markets", [])

                    active_markets = [m for m in markets_page if m["status"] == "active" 
                                     and m.get("ticker", "") not in seen_tickers]
                    if active_markets:
                        for m in active_markets:
                            seen_tickers.add(m.get("ticker", ""))
                        logger.info(
                            f"Fetched {len(markets_page)} markets, {len(active_markets)} new active."
                        )
                        await process_and_queue_markets(
                            active_markets,
                            db_manager,
                            queue,
                            existing_position_market_ids,
                            logger,
                        )

                    cursor = response.get("cursor")
                    if not cursor:
                        break
            
            logger.info(f"Total unique markets ingested: {len(seen_tickers)}")

    except Exception as e:
        logger.error(
            "An error occurred during market ingestion.", error=str(e), exc_info=True
        )
    finally:
        await kalshi_client.close()
        logger.info("Market ingestion job finished.")
