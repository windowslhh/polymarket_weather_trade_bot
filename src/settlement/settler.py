"""Settlement detection and realized P&L computation.

Checks if any open positions' markets have settled (market resolved on Gamma API),
fetches settlement outcomes, and computes realized P&L.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import date, datetime, timezone

import httpx

from src.portfolio.store import Store

logger = logging.getLogger(__name__)

GAMMA_API_URL = "https://gamma-api.polymarket.com"


@dataclass
class SettlementResult:
    event_id: str
    city: str
    winning_slot: str
    positions_settled: int
    total_pnl: float


async def check_settlements(store: Store) -> list[SettlementResult]:
    """Check and process all settled markets with open positions.

    Idempotent: checks settlements table before processing to avoid double-counting.
    """
    open_positions = await store.get_open_positions()
    if not open_positions:
        return []

    # Group positions by event_id
    event_positions: dict[str, list[dict]] = {}
    for pos in open_positions:
        event_positions.setdefault(pos["event_id"], []).append(pos)

    # Get already-settled event_ids to avoid double-processing
    existing_settlements = await store.get_settlements()
    settled_event_ids = {s["event_id"] for s in existing_settlements}

    results: list[SettlementResult] = []

    async with httpx.AsyncClient(timeout=15) as client:
        for event_id, positions in event_positions.items():
            # Idempotency: skip already-settled events
            if event_id in settled_event_ids:
                # Just mark positions as settled if not already
                for pos in positions:
                    if pos["status"] == "open":
                        await store.db.execute(
                            "UPDATE positions SET status = 'settled', closed_at = datetime('now') WHERE id = ?",
                            (pos["id"],),
                        )
                await store.db.commit()
                continue

            city = positions[0]["city"]

            try:
                outcome = await _fetch_settlement_outcome(client, event_id)
            except Exception:
                logger.debug("Could not fetch settlement for event %s", event_id)
                continue

            if outcome is None:
                continue

            winning_slot, settled_prices = outcome
            logger.info("Settlement detected: %s — winning slot: %s", city, winning_slot)

            total_pnl = 0.0
            settled_count = 0

            for pos in positions:
                pnl = _compute_position_pnl(pos, settled_prices)
                total_pnl += pnl
                settled_count += 1

                await store.db.execute(
                    "UPDATE positions SET status = 'settled', closed_at = datetime('now') WHERE id = ?",
                    (pos["id"],),
                )
                logger.info(
                    "  Position %d: %s %s %s → P&L=$%.2f",
                    pos["id"], pos["side"], pos["token_type"],
                    pos["slot_label"][:30], pnl,
                )

            await store.db.commit()
            await store.insert_settlement(event_id, city, winning_slot, total_pnl)
            await _update_realized_pnl(store, date.today().isoformat(), total_pnl)

            results.append(SettlementResult(
                event_id=event_id, city=city, winning_slot=winning_slot,
                positions_settled=settled_count, total_pnl=total_pnl,
            ))

    if results:
        total = sum(r.total_pnl for r in results)
        logger.info(
            "Settled %d events, %d positions, total P&L=$%.2f",
            len(results), sum(r.positions_settled for r in results), total,
        )

    return results


async def _fetch_settlement_outcome(
    client: httpx.AsyncClient, event_id: str
) -> tuple[str, dict[str, float]] | None:
    """Fetch resolved outcome for an event from Gamma API.

    Returns (winning_slot_label, {question: resolved_yes_price}) or None.
    """
    try:
        resp = await client.get(f"{GAMMA_API_URL}/events/{event_id}")
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        event_data = resp.json()
    except Exception:
        logger.debug("Gamma API error for event %s", event_id)
        return None

    # Market is settled if closed=true OR if all outcome prices are 0/1
    is_closed = event_data.get("closed", False)

    markets = event_data.get("markets", [])
    if not markets:
        return None

    winning_slot = None
    settled_prices: dict[str, float] = {}

    for mkt in markets:
        question = mkt.get("question", "")
        outcome_prices = mkt.get("outcomePrices", [])
        if isinstance(outcome_prices, str):
            try:
                outcome_prices = json.loads(outcome_prices)
            except (json.JSONDecodeError, TypeError):
                continue

        if len(outcome_prices) < 2:
            continue

        try:
            yes_price = float(outcome_prices[0])
        except (ValueError, TypeError):
            continue

        settled_prices[question] = yes_price

        if yes_price >= 0.99:
            winning_slot = question

    if not settled_prices:
        return None

    # Check if market is actually resolved
    all_resolved = all(p >= 0.99 or p <= 0.01 for p in settled_prices.values())

    if not is_closed and not all_resolved:
        return None  # Not settled yet

    if winning_slot is None and all_resolved:
        winning_slot = "none"

    if winning_slot is None:
        return None

    return winning_slot, settled_prices


def _compute_position_pnl(position: dict, settled_prices: dict[str, float]) -> float:
    """Compute realized P&L for a single position."""
    slot_label = position["slot_label"]
    entry_price = position["entry_price"]
    shares = position["shares"]
    token_type = position["token_type"]

    # Match slot to settled outcome
    yes_resolved = None
    for label, price in settled_prices.items():
        if slot_label in label or label in slot_label:
            yes_resolved = price
            break

    if yes_resolved is None:
        logger.warning("Could not match slot %s to settlement data, assuming NO wins", slot_label[:30])
        yes_resolved = 0.0

    if token_type == "NO":
        if yes_resolved <= 0.01:
            return (1.0 - entry_price) * shares  # NO wins
        else:
            return -entry_price * shares  # NO loses
    else:  # YES
        if yes_resolved >= 0.99:
            return (1.0 - entry_price) * shares  # YES wins
        else:
            return -entry_price * shares  # YES loses


async def _update_realized_pnl(store: Store, date_str: str, pnl: float) -> None:
    """Add realized P&L to the daily total."""
    current = await store.get_daily_pnl(date_str)
    new_realized = (current or 0.0) + pnl
    exposure = await store.get_total_exposure()
    await store.upsert_daily_pnl(date_str, new_realized, 0, exposure)
