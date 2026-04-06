"""Settlement detection and realized P&L computation.

Checks if any open positions' markets have settled (market_date < today),
fetches settlement outcomes from Gamma API, and computes realized P&L.
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
    winning_slot: str  # outcome label of the winning slot
    positions_settled: int
    total_pnl: float


async def check_settlements(store: Store) -> list[SettlementResult]:
    """Check and process all settled markets with open positions.

    1. Find open positions with market_date < today
    2. Query Gamma API for resolved outcomes
    3. Compute realized P&L
    4. Update positions to 'settled' + record in settlements table
    """
    today = date.today()
    open_positions = await store.get_open_positions()

    if not open_positions:
        return []

    # Group positions by event_id and extract market dates from slot_label
    event_positions: dict[str, list[dict]] = {}
    for pos in open_positions:
        event_positions.setdefault(pos["event_id"], []).append(pos)

    results: list[SettlementResult] = []

    async with httpx.AsyncClient(timeout=15) as client:
        for event_id, positions in event_positions.items():
            # Check if this event should have settled
            # Parse date from the slot_label (contains "on April 5?" etc)
            city = positions[0]["city"]

            try:
                outcome = await _fetch_settlement_outcome(client, event_id)
            except Exception:
                logger.debug("Could not fetch settlement for event %s", event_id)
                continue

            if outcome is None:
                continue  # Market not yet settled

            winning_slot, settled_prices = outcome
            logger.info("Settlement detected: %s — winning slot: %s", city, winning_slot)

            # Compute P&L for each position
            total_pnl = 0.0
            settled_count = 0

            for pos in positions:
                pnl = _compute_position_pnl(pos, settled_prices)
                total_pnl += pnl
                settled_count += 1

                # Mark position as settled
                await store.close_position(pos["id"])
                await store.db.execute(
                    "UPDATE positions SET status = 'settled' WHERE id = ?",
                    (pos["id"],),
                )

                logger.info(
                    "  Position %d: %s %s %s → P&L=$%.2f",
                    pos["id"], pos["side"], pos["token_type"],
                    pos["slot_label"][:30], pnl,
                )

            await store.db.commit()

            # Record settlement
            await store.insert_settlement(event_id, city, winning_slot, total_pnl)

            # Update daily realized P&L
            await _update_realized_pnl(store, today.isoformat(), total_pnl)

            results.append(SettlementResult(
                event_id=event_id,
                city=city,
                winning_slot=winning_slot,
                positions_settled=settled_count,
                total_pnl=total_pnl,
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

    Returns (winning_slot_label, {slot_label: resolved_yes_price}) or None if not settled.
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

    # Check if event is closed/resolved
    if event_data.get("active") and not event_data.get("closed"):
        return None  # Still active

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

        # YES=1.0 means this slot won
        if yes_price >= 0.99:
            winning_slot = question

    if not settled_prices:
        return None

    # If no slot has YES=1.0, market might not be fully settled yet
    if winning_slot is None:
        # Check if all prices are 0 or 1 (fully resolved)
        all_resolved = all(p >= 0.99 or p <= 0.01 for p in settled_prices.values())
        if not all_resolved:
            return None
        winning_slot = "none"  # Edge case: no winner determined

    return winning_slot, settled_prices


def _compute_position_pnl(position: dict, settled_prices: dict[str, float]) -> float:
    """Compute realized P&L for a single position based on settlement outcome.

    For NO positions:
    - If the slot's YES resolved to 0 (NO wins): profit = (1.0 - entry_price) * shares
    - If the slot's YES resolved to 1 (NO loses): loss = -entry_price * shares
    """
    slot_label = position["slot_label"]
    entry_price = position["entry_price"]
    shares = position["shares"]
    token_type = position["token_type"]

    # Find matching settled price
    yes_resolved = None
    for label, price in settled_prices.items():
        if slot_label in label or label in slot_label:
            yes_resolved = price
            break

    if yes_resolved is None:
        # Can't find matching slot — assume NO wins (conservative for NO positions)
        logger.warning("Could not match slot %s to settlement data", slot_label[:30])
        yes_resolved = 0.0

    if token_type == "NO":
        if yes_resolved <= 0.01:
            # NO wins → we get $1 per share
            return (1.0 - entry_price) * shares
        else:
            # NO loses → we lose our stake
            return -entry_price * shares
    else:
        # YES position
        if yes_resolved >= 0.99:
            return (1.0 - entry_price) * shares
        else:
            return -entry_price * shares


async def _update_realized_pnl(store: Store, date_str: str, pnl: float) -> None:
    """Add realized P&L to the daily total (accumulates across settlements)."""
    current = await store.get_daily_pnl(date_str)
    new_realized = (current or 0.0) + pnl
    exposure = await store.get_total_exposure()
    await store.upsert_daily_pnl(date_str, new_realized, 0, exposure)
