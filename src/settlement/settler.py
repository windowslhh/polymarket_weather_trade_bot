"""Settlement detection and realized P&L computation.

Checks if any open positions' markets have settled (market resolved on Gamma API),
fetches settlement outcomes, and computes realized P&L.
"""
from __future__ import annotations

import json
import logging
from collections import defaultdict
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
        # BUG-1 heartbeat: log even when there's nothing to do so operators
        # can confirm the settlement job is alive (Apr 26 audit found the
        # job had been silently no-op for 25h+ because every internal
        # exception was swallowed at debug level).
        logger.info("Settlement check: scanned 0 events, settled 0, skipped 0 (no open positions)")
        return []

    # Group positions by event_id
    event_positions: dict[str, list[dict]] = {}
    for pos in open_positions:
        event_positions.setdefault(pos["event_id"], []).append(pos)

    # Get already-settled (event_id, strategy) pairs to avoid double-processing
    existing_settlements = await store.get_settlements()
    settled_pairs = {(s["event_id"], s.get("strategy", "B")) for s in existing_settlements}
    settled_event_ids = {s["event_id"] for s in existing_settlements}

    results: list[SettlementResult] = []
    # BUG-1 heartbeat counters
    n_total = len(event_positions)
    n_skipped_unclosed = 0  # Gamma reports closed=False (waiting next cycle)
    n_skipped_already = 0   # already-settled idempotent skip
    n_skipped_error = 0     # fetch error / invalid response

    async with httpx.AsyncClient(timeout=15) as client:
        for event_id, positions in event_positions.items():
            # Idempotency: mark positions settled only if their strategy has a settlement record
            if event_id in settled_event_ids:
                for pos in positions:
                    strat = pos.get("strategy", "B")
                    if pos["status"] == "open" and (event_id, strat) in settled_pairs:
                        await store.db.execute(
                            "UPDATE positions SET status = 'settled', closed_at = datetime('now') WHERE id = ?",
                            (pos["id"],),
                        )
                await store.db.commit()
                # Skip if ALL strategies for this event already settled
                unsettled_strategies = {pos.get("strategy", "B") for pos in positions if pos["status"] == "open"} - {s for eid, s in settled_pairs if eid == event_id}
                if not unsettled_strategies:
                    n_skipped_already += 1
                    continue

            # Double-check: skip if no open positions left (already settled by another run)
            open_count = sum(1 for p in positions if p["status"] == "open")
            if open_count == 0:
                n_skipped_already += 1
                continue

            city = positions[0]["city"]

            try:
                outcome = await _fetch_settlement_outcome(client, event_id)
            except Exception:
                # BUG-1: was logger.debug — invisible in normal log levels.
                # Promote to logger.exception so the stack reaches operators.
                logger.exception("Could not fetch settlement for event %s", event_id)
                n_skipped_error += 1
                continue

            if outcome is None:
                # outcome is None for two distinct reasons (404, closed=False,
                # JSON parse fail).  _fetch_settlement_outcome now logs the
                # *cause* itself (BUG-1); the count here just tracks
                # "couldn't settle this cycle for any reason".
                n_skipped_unclosed += 1
                continue

            winning_slot = outcome.winning_slot
            settled_prices = outcome.label_prices
            token_prices = outcome.token_prices
            logger.info("Settlement detected: %s — winning slot: %s", city, winning_slot)

            # Compute P&L per strategy for separate tracking
            strategy_pnl: dict[str, float] = defaultdict(float)
            strategy_count: dict[str, int] = defaultdict(int)
            total_pnl = 0.0
            settled_count = 0

            for pos in positions:
                strat = pos.get("strategy", "B")
                # Skip strategies already settled in a prior run — their P&L was
                # counted then; including them again would double-count realized P&L.
                if (event_id, strat) in settled_pairs:
                    continue
                pnl = _compute_position_pnl(pos, settled_prices, token_prices)
                exit_price = _settlement_exit_price(pos, settled_prices, token_prices)
                strategy_pnl[strat] += pnl
                strategy_count[strat] += 1
                total_pnl += pnl
                settled_count += 1

                await store.db.execute(
                    """UPDATE positions SET status = 'settled', closed_at = datetime('now'),
                       exit_price = ?, realized_pnl = ? WHERE id = ?""",
                    (exit_price, pnl, pos["id"]),
                )
                logger.info(
                    "  Position %d [%s]: %s %s %s → P&L=$%.2f",
                    pos["id"], strat, pos["side"], pos["token_type"],
                    pos["slot_label"][:30], pnl,
                )

            await store.db.commit()

            # Insert one settlement record per strategy (for per-strategy P&L tracking)
            for strat, pnl in strategy_pnl.items():
                await store.insert_settlement(event_id, city, winning_slot, pnl, strategy=strat)
                logger.info("  Strategy %s: %d positions, P&L=$%.2f", strat, strategy_count[strat], pnl)

            # R-02 fix: use UTC date instead of server-local date.today()
            # In Docker the server runs UTC; for US settlements this avoids
            # recording P&L under tomorrow's date during UTC midnight crossover.
            utc_date_str = datetime.now(timezone.utc).date().isoformat()
            await _update_realized_pnl(store, utc_date_str, total_pnl)

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

    # BUG-1 heartbeat: emit a one-line scan summary every cycle, even when
    # nothing settled.  Pre-fix the only INFO-level signal was the conditional
    # "Settled N events" above, which never fires when Gamma hasn't closed
    # anything — operators couldn't distinguish "job runs, no work" from
    # "job died silently".
    logger.info(
        "Settlement check: scanned %d events, settled %d, "
        "skipped_unclosed=%d skipped_already=%d skipped_error=%d",
        n_total, len(results),
        n_skipped_unclosed, n_skipped_already, n_skipped_error,
    )

    return results


@dataclass
class SettlementOutcome:
    """Parsed settlement data from the Gamma API."""
    winning_slot: str
    label_prices: dict[str, float]    # {question_text: resolved_yes_price}
    token_prices: dict[str, float]    # {clob_token_id: resolved_yes_price} — SET-02 fix


async def _fetch_settlement_outcome(
    client: httpx.AsyncClient, event_id: str
) -> SettlementOutcome | None:
    """Fetch resolved outcome for an event from Gamma API.

    Returns a SettlementOutcome with both label-keyed and token_id-keyed price
    maps, or None if the event is not yet settled.
    """
    try:
        resp = await client.get(f"{GAMMA_API_URL}/events/{event_id}")
        if resp.status_code == 404:
            # BUG-1: was silent.  A 404 on an event we still hold a
            # position in usually means the event was archived /
            # deprecated / wrong-id; surface it so the watchdog can
            # eventually flag the stuck position.
            logger.warning("Event %s returned 404 from Gamma", event_id)
            return None
        resp.raise_for_status()
        event_data = resp.json()
    except Exception:
        # BUG-1: was logger.debug — invisible at default INFO level.
        # Use exception() so the stack reaches the operator (network
        # blip vs JSON parse fail vs malformed schema all matter).
        logger.exception("Gamma fetch failed for event %s", event_id)
        return None

    # Market is settled if closed=true OR if all outcome prices are 0/1
    is_closed = event_data.get("closed", False)

    markets = event_data.get("markets", [])
    if not markets:
        return None

    winning_slot = None
    label_prices: dict[str, float] = {}
    token_prices: dict[str, float] = {}

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

        label_prices[question] = yes_price

        # SET-02 fix: also build a token_id → yes_price map so that settlement
        # matching can use exact token_id lookup instead of substring on labels.
        clob_token_ids = mkt.get("clobTokenIds", [])
        if isinstance(clob_token_ids, str):
            try:
                clob_token_ids = json.loads(clob_token_ids)
            except (json.JSONDecodeError, TypeError):
                clob_token_ids = []
        if clob_token_ids:
            # First token is YES, map it to the resolved yes_price
            token_prices[clob_token_ids[0]] = yes_price
            # Also map the NO token (second entry) so positions holding NO tokens
            # can be looked up directly
            if len(clob_token_ids) > 1:
                token_prices[clob_token_ids[1]] = yes_price

        if yes_price >= 0.99:
            winning_slot = question

    if not label_prices:
        return None

    # Only settle when the event is officially closed by Polymarket
    # Note: individual slots may show 0/1 prices early (e.g. temp already exceeded
    # a low slot), but the overall event is not settled until closed=true
    if not is_closed:
        return None

    if winning_slot is None:
        # All resolved to 0 but none to 1 — unusual, treat as no winner
        winning_slot = "none"

    return SettlementOutcome(
        winning_slot=winning_slot,
        label_prices=label_prices,
        token_prices=token_prices,
    )


def _resolve_yes_price(
    slot_label: str,
    label_prices: dict[str, float],
    token_id: str = "",
    token_prices: dict[str, float] | None = None,
) -> float | None:
    """Match a position to the settled YES price.

    SET-02 fix: prefers exact token_id match (no ambiguity possible), then falls
    back to label matching.  Token_id matching uses the clobTokenIds returned by
    the Gamma API which are guaranteed unique per slot.

    Falls back to label matching (exact first, then substring) for backward
    compatibility in case token_prices is unavailable.
    """
    # Priority 1: exact token_id match — unambiguous, no false positives
    if token_id and token_prices and token_id in token_prices:
        return token_prices[token_id]

    # Priority 2: exact label match
    if slot_label in label_prices:
        return label_prices[slot_label]

    # Priority 3: substring fallback (kept for edge cases where labels differ slightly)
    matches = [
        (label, price) for label, price in label_prices.items()
        if slot_label in label or label in slot_label
    ]
    if len(matches) == 1:
        return matches[0][1]
    if len(matches) > 1:
        matches.sort(key=lambda lp: len(lp[0]), reverse=True)
        logger.warning(
            "Ambiguous slot match for %r — using longest match %r",
            slot_label, matches[0][0],
        )
        return matches[0][1]
    return None


def _settlement_exit_price(
    position: dict,
    label_prices: dict[str, float],
    token_prices: dict[str, float] | None = None,
) -> float:
    """Determine the exit price for a settled position (0.0 or 1.0)."""
    yes_resolved = _resolve_yes_price(
        position["slot_label"], label_prices,
        token_id=position.get("token_id", ""), token_prices=token_prices,
    )
    if yes_resolved is None:
        yes_resolved = 0.0
    if position["token_type"] == "NO":
        return 1.0 if yes_resolved <= 0.01 else 0.0
    return 1.0 if yes_resolved >= 0.99 else 0.0


def _compute_position_pnl(
    position: dict,
    label_prices: dict[str, float],
    token_prices: dict[str, float] | None = None,
) -> float:
    """Compute realized P&L for a single position."""
    entry_price = position["entry_price"]
    shares = position["shares"]
    token_type = position["token_type"]

    yes_resolved = _resolve_yes_price(
        position["slot_label"], label_prices,
        token_id=position.get("token_id", ""), token_prices=token_prices,
    )
    if yes_resolved is None:
        logger.warning("Could not match slot %s to settlement data, assuming NO wins", position["slot_label"][:30])
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


async def check_stuck_positions(
    store: Store,
    alerter,
    *,
    max_age_hours: int = 48,
) -> list[dict]:
    """BUG-2: alert on positions still open well past their settle window.

    The settler is silent (BUG-1 heartbeat aside) when Gamma simply
    hasn't closed an event yet, which is normal.  But a position open
    for 48h+ is **not** normal — Polymarket weather events resolve
    within 24-36h of creation in steady state.  A stuck row at this
    age means one of:

      - Gamma forgot to close the event (vendor issue)
      - The event_id we hold no longer exists upstream (deprecation
        / wrong id)
      - The settler is throwing on this row consistently (now visible
        thanks to BUG-1, but the watchdog still wants to alert)
      - Operator forgot to manually mark a paper-only or test-only row

    We use ``created_at`` (always populated, indexed) as the proxy for
    "should have settled by now".  Strictly speaking the right anchor
    is ``market_date``, but that field doesn't exist in the positions
    table — slot_label encodes it as text, and parsing dates out of
    free-form labels at query time is brittle.  ``created_at +
    max_age_hours`` is a strict over-estimate (positions are typically
    opened a few hours before market_date), so the watchdog can never
    fire prematurely; the cost is a slight delay in catching genuinely
    stuck rows.

    Returns the list of stuck position rows so callers can log /
    correlate.  Sends a warning alert when the list is non-empty.
    """
    rows = []
    async with store.db.execute(
        """SELECT id, event_id, strategy, city, slot_label, created_at, size_usd
           FROM positions
           WHERE status = 'open'
             AND created_at < datetime('now', ?)
           ORDER BY created_at""",
        (f"-{max_age_hours} hours",),
    ) as cur:
        async for row in cur:
            rows.append({
                "id": row[0], "event_id": row[1], "strategy": row[2],
                "city": row[3], "slot_label": row[4], "created_at": row[5],
                "size_usd": row[6],
            })

    if not rows:
        return []

    # One alert with a compact summary; per-row detail in the log line so
    # the webhook doesn't get spammed with 30 messages on a bad day.
    summary = ", ".join(
        f"id={r['id']}({r['strategy']}/{r['city']}, {r['created_at'][:16]})"
        for r in rows[:10]
    )
    suffix = f" (+{len(rows) - 10} more)" if len(rows) > 10 else ""
    await alerter.send(
        "warning",
        f"Stuck position alert: {len(rows)} positions open >{max_age_hours}h "
        f"past creation: {summary}{suffix}",
    )
    return rows


async def _update_realized_pnl(store: Store, date_str: str, pnl: float) -> None:
    """Atomically increment realized P&L for the given date.

    Uses a single INSERT … ON CONFLICT DO UPDATE SET realized_pnl = realized_pnl + ?
    instead of a read-modify-write so that concurrent settlement runs (before R-01
    lock is in place) cannot lose increments by overwriting each other.
    """
    exposure = await store.get_total_exposure()
    await store.db.execute(
        """INSERT INTO daily_pnl (date, realized_pnl, unrealized_pnl, total_exposure, updated_at)
           VALUES (?, ?, 0, ?, datetime('now'))
           ON CONFLICT(date) DO UPDATE SET
               realized_pnl  = realized_pnl + ?,
               total_exposure = ?,
               updated_at    = datetime('now')""",
        (date_str, pnl, exposure, pnl, exposure),
    )
    await store.db.commit()
