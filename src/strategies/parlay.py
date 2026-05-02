"""
Parlay mode — 3-leg combined bet triggered every PARLAY_EVERY normal trades.

Each parlay scans eligible markets via the normal AI decision engine, picks
the 3 highest-confidence BUY candidates, and places a smaller bet on each.
Total parlay stake ≈ one normal bet (each leg is 1/PARLAY_LEGS of normal size).

Triggered from beast_mode_bot._run_trading_cycles whenever
total_bets_placed crosses a new multiple of PARLAY_EVERY.
"""

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional, Tuple

from src.utils.database import DatabaseManager, Market, Position
from src.utils.logging_setup import get_trading_logger
from src.clients.kalshi_client import KalshiClient
from src.clients.xai_client import XAIClient
from src.config.settings import settings
from src.jobs.decide import make_decision_for_market
from src.jobs.execute import execute_position

PARLAY_EVERY = 10
PARLAY_LEGS = 3
# How many candidate markets to scan when building the parlay shortlist.
# make_decision_for_market skips recently-analyzed markets cheaply (DB read
# only), so a pool of 15 is inexpensive during a normal cycle.
_CANDIDATE_POOL = 15

logger = get_trading_logger("parlay")


@dataclass
class ParlayResult:
    parlay_id: str
    legs_placed: int
    legs_attempted: int
    total_stake: float
    markets: List[str] = field(default_factory=list)
    all_legs_placed: bool = False


async def run_parlay(
    db_manager: DatabaseManager,
    kalshi_client: KalshiClient,
    xai_client: XAIClient,
    live_mode: bool,
) -> Optional[ParlayResult]:
    """
    Run a 3-leg parlay.

    Scans eligible markets, scores them via the standard AI decision engine,
    selects the top 3 by confidence, and places a scaled-down position on each.
    Returns a ParlayResult (or None if there aren't enough candidates).
    """
    parlay_id = str(uuid.uuid4())[:8]
    logger.info(f"PARLAY {parlay_id}: scanning for {PARLAY_LEGS}-leg parlay candidates")

    try:
        markets = await db_manager.get_eligible_markets(
            volume_min=settings.trading.min_volume,
            max_days_to_expiry=settings.trading.max_time_to_expiry_days,
        )
    except Exception as e:
        logger.error(f"PARLAY {parlay_id}: failed to fetch eligible markets: {e}")
        return None

    if len(markets) < PARLAY_LEGS:
        logger.warning(
            f"PARLAY {parlay_id}: only {len(markets)} eligible markets "
            f"(need {PARLAY_LEGS}). Skipping."
        )
        return None

    # Score candidates — cap pool to _CANDIDATE_POOL to control AI cost.
    candidates: List[Tuple[float, Market, Position]] = []
    for market in markets[:_CANDIDATE_POOL]:
        try:
            position = await make_decision_for_market(
                market, db_manager, xai_client, kalshi_client
            )
            if position and position.confidence:
                candidates.append((position.confidence, market, position))
        except Exception as e:
            logger.warning(f"PARLAY {parlay_id}: error evaluating {market.market_id}: {e}")

    if len(candidates) < PARLAY_LEGS:
        logger.warning(
            f"PARLAY {parlay_id}: only {len(candidates)} actionable candidates "
            f"after scan (need {PARLAY_LEGS}). Skipping."
        )
        return None

    # Pick the top 3 by AI confidence
    candidates.sort(key=lambda x: x[0], reverse=True)
    legs = candidates[:PARLAY_LEGS]

    result = ParlayResult(
        parlay_id=parlay_id,
        legs_placed=0,
        legs_attempted=PARLAY_LEGS,
        total_stake=0.0,
        markets=[m.market_id for _, m, _ in legs],
    )

    logger.info(
        f"PARLAY {parlay_id}: selected legs — "
        + ", ".join(
            f"{m.market_id} {pos.side} conf={conf:.0%}"
            for conf, m, pos in legs
        )
    )

    for leg_num, (confidence, market, position) in enumerate(legs, 1):
        # Scale each leg to 1/PARLAY_LEGS of the normal quantity
        position.quantity = max(1, position.quantity // PARLAY_LEGS)
        position.strategy = f"parlay_leg_{leg_num}:{parlay_id}"
        position.rationale = (
            f"[PARLAY {parlay_id} leg {leg_num}/{PARLAY_LEGS}] "
            + (position.rationale or "")
        )

        # execute_position requires position.id, so persist to DB first
        position_id = await db_manager.add_position(position)
        if position_id is None:
            logger.warning(
                f"PARLAY {parlay_id} leg {leg_num}: could not save position for "
                f"{market.market_id} (duplicate or DB error). Skipping leg."
            )
            continue

        position.id = position_id

        success = await execute_position(position, live_mode, db_manager, kalshi_client)
        if success:
            result.legs_placed += 1
            result.total_stake += position.quantity * position.entry_price
            logger.info(
                f"PARLAY {parlay_id} leg {leg_num}/{PARLAY_LEGS}: "
                f"{market.market_id} {position.side} x{position.quantity} "
                f"@ {position.entry_price:.2f} (conf {confidence:.0%})"
            )
        else:
            logger.warning(
                f"PARLAY {parlay_id} leg {leg_num}: execution failed for {market.market_id}"
            )

    result.all_legs_placed = result.legs_placed == PARLAY_LEGS
    status = "ALL LEGS PLACED" if result.all_legs_placed else f"{result.legs_placed}/{PARLAY_LEGS} legs placed"
    logger.info(
        f"PARLAY {parlay_id} done: {status} | "
        f"stake=${result.total_stake:.2f} | markets={result.markets}"
    )
    return result
