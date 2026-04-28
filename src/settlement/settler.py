"""Settlement detection, on-chain confirmation, redeem, and realized P&L.

The settler runs every 15 minutes (alongside the position check).  It
walks every open position and asks "has *this market* — not the parent
event — closed on Gamma?".  If yes, it confirms on-chain (the dispute
window can keep payoutDenominator==0 for ~30 min after Gamma flips),
classifies winner vs loser for our held NO, and either:

  - WINNER: atomically reserves the row for redeem, calls the Redeemer
            (Safe execTransaction → ConditionalTokens / NegRiskAdapter),
            on success marks the position settled with redeem metadata.
  - LOSER:  marks the position settled at exit_price=0 (no redeem
            needed — the ERC1155 expires worthless on its own).

Per-market (vs per-event) detection — the Polymarket "event" is just a
bag of markets that share a settlement date; individual slot markets
flip ``closed=true`` independently as their resolution becomes certain.
The pre-2026-04-28 settler waited for the *event* to close, leaving
locked-win positions stuck for hours after their slot had resolved.

The Redeemer instance is optional: paper / dry-run mode (or a startup
without a wallet) leaves it None, and we still update the DB so the
dashboard reflects settlement P&L — we just don't issue the on-chain
call.  Live mode wires it in via main.py.
"""
from __future__ import annotations

import json
import logging
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx

from src.alerts import Alerter
from src.portfolio.store import Store
from src.portfolio.utils import effective_entry_price
from src.settlement.redeemer import Redeemer

logger = logging.getLogger(__name__)

GAMMA_API_URL = "https://gamma-api.polymarket.com"

# Max redeem attempts before we give up and alert.  Per-cycle retries are
# free; the alert exists so a persistent failure (gas spike, wrong cid,
# RPC rot) gets a human in the loop rather than silently wasting budget.
MAX_REDEEM_ATTEMPTS = 3


@dataclass
class SettlementResult:
    event_id: str
    city: str
    winning_slot: str
    positions_settled: int
    total_pnl: float


@dataclass
class _MarketRecord:
    """Subset of Gamma market data we need for per-market settlement."""
    condition_id: str
    closed: bool
    yes_price: float | None  # 0..1 from outcomePrices[0], or None if unparsable
    clob_token_ids: list[str]


async def check_settlements(
    store: Store,
    redeemer: Redeemer | None = None,
    alerter: Alerter | None = None,
) -> list[SettlementResult]:
    """Per-market settlement: classify each open position, redeem winners.

    Idempotent on every layer:
      - Gamma fetch is read-only.
      - claim_redeem_attempt() is atomic (UPDATE … WHERE redeem_status IS NULL).
      - The Redeemer itself short-circuits when on-chain balance == 0.
      - settlements table has UNIQUE(event_id, strategy) so we never insert
        a duplicate P&L row even if classify and update run twice.
    """
    open_positions = await store.get_open_positions()
    if not open_positions:
        return []

    # Already-settled (event_id, strategy) pairs — skip P&L re-insertion
    # without skipping the redeem path, because a position could be
    # marked settled by a prior cycle but still need the redeem call.
    existing_settlements = await store.get_settlements()
    settled_pairs = {(s["event_id"], s.get("strategy", "B")) for s in existing_settlements}

    # Group by event_id so we make one Gamma call per event regardless
    # of how many positions hit it.
    event_positions: dict[str, list[dict]] = defaultdict(list)
    for pos in open_positions:
        event_positions[pos["event_id"]].append(pos)

    results: list[SettlementResult] = []
    async with httpx.AsyncClient(timeout=15) as client:
        for event_id, positions in event_positions.items():
            try:
                markets = await _fetch_event_markets(client, event_id)
            except Exception:
                logger.debug("Could not fetch settlement for event %s", event_id)
                continue

            if not markets:
                continue

            # token_id → _MarketRecord for fast per-position lookup.
            by_token: dict[str, _MarketRecord] = {}
            for m in markets:
                for tid in m.clob_token_ids:
                    by_token[tid] = m

            # Strategy-keyed P&L for this event's settlements record.
            strategy_pnl: dict[str, float] = defaultdict(float)
            strategy_count: dict[str, int] = defaultdict(int)
            settled_in_this_event = 0
            winning_slot_label = ""

            for pos in positions:
                if pos["status"] != "open":
                    continue
                strat = pos.get("strategy", "B")

                tid = pos["token_id"]
                record = by_token.get(tid)
                if record is None or not record.closed:
                    # Per-market: the slot we hold hasn't flipped yet.
                    # Other positions in the same event may have flipped
                    # — those will settle independently.
                    continue

                # Yes-side market price → winner classification.
                # NO wins when YES resolves to 0; YES wins when YES resolves to 1.
                yes_price = record.yes_price
                if yes_price is None:
                    # closed=true but outcomePrices unparsable — treat as
                    # RESOLVING and skip until next cycle.
                    logger.warning(
                        "Market closed but outcomePrices unparsable for "
                        "event=%s token=%s — skipping",
                        event_id, tid[:16] + "...",
                    )
                    continue

                no_won = yes_price <= 0.01
                yes_won = yes_price >= 0.99
                if not (no_won or yes_won):
                    # closed=true but neither side at the rail — Gamma
                    # may be lagging on-chain resolution.  Skip and retry.
                    logger.debug(
                        "Market closed but outcome ambiguous (yes=%.3f) for "
                        "event=%s — awaiting finality",
                        yes_price, event_id,
                    )
                    continue

                # On-chain confirm — only required for the WINNER path,
                # since redeem against an unfinalized condition reverts.
                # Loser path is purely DB bookkeeping and is safe to do
                # now (the ERC1155 will be worthless regardless of when
                # payoutDenominator finalizes).
                token_type = pos.get("token_type", "NO")
                bot_wins = (token_type == "NO" and no_won) or (
                    token_type == "YES" and yes_won
                )

                if bot_wins:
                    settled = await _settle_winner(
                        store=store, position=pos, market=record,
                        redeemer=redeemer, alerter=alerter,
                    )
                else:
                    settled = await _settle_loser(
                        store=store, position=pos,
                    )

                if not settled:
                    continue

                # Compute P&L from the resolved YES price for the settlements row.
                pnl = _pnl_from_yes_price(pos, yes_price)
                strategy_pnl[strat] += pnl
                strategy_count[strat] += 1
                settled_in_this_event += 1

                # Track the human label (first winner we see) for the
                # SettlementResult message.
                if no_won and not winning_slot_label:
                    winning_slot_label = pos.get("slot_label", "")

            if settled_in_this_event == 0:
                continue

            # Insert one settlements row per (event, strategy) — UNIQUE
            # index makes this idempotent across cycles, but skip pairs
            # that already exist to avoid noise in the audit log.
            city = positions[0]["city"]
            for strat, pnl in strategy_pnl.items():
                if (event_id, strat) in settled_pairs:
                    continue
                await store.insert_settlement(
                    event_id, city, winning_slot_label or "<per-market>",
                    pnl, strategy=strat,
                )
                logger.info(
                    "Settlement [%s] %s: %d positions, P&L=$%.2f",
                    strat, city, strategy_count[strat], pnl,
                )

            utc_date_str = datetime.now(timezone.utc).date().isoformat()
            await _update_realized_pnl(
                store, utc_date_str, sum(strategy_pnl.values()),
            )

            results.append(SettlementResult(
                event_id=event_id, city=city,
                winning_slot=winning_slot_label or "<per-market>",
                positions_settled=settled_in_this_event,
                total_pnl=sum(strategy_pnl.values()),
            ))

    if results:
        total = sum(r.total_pnl for r in results)
        logger.info(
            "Settled %d events, %d positions, total P&L=$%.2f",
            len(results), sum(r.positions_settled for r in results), total,
        )
    return results


async def _settle_winner(
    *,
    store: Store,
    position: dict,
    market: _MarketRecord,
    redeemer: Redeemer | None,
    alerter: Alerter | None,
) -> bool:
    """Redeem path for a winning position.

    Returns True iff the position is now ``status='settled'`` (either we
    redeemed successfully OR the row was already redeemed by a prior
    cycle).  Returns False on transient failure — caller skips P&L
    rollup for this position and retries next cycle.
    """
    pos_id = position["id"]
    cid = position.get("condition_id") or market.condition_id
    if not cid:
        logger.error(
            "Cannot redeem position %d: no condition_id (DB and Gamma both empty)",
            pos_id,
        )
        return False

    # On-chain confirmation guard — only enforced when we have a redeemer
    # (paper / dry-run skip the chain call entirely and trust Gamma).
    if redeemer is not None:
        is_resolved, _ = await redeemer.check_condition_resolved_async(cid)
        if not is_resolved:
            logger.info(
                "Position %d cid=%s — Gamma closed but on-chain "
                "payoutDenominator=0; awaiting finality",
                pos_id, cid[:16] + "...",
            )
            return False

    # No redeemer = paper / dry-run / live without wallet config.  Skip
    # the on-chain call but still mark the position settled so the
    # dashboard P&L reflects reality.
    if redeemer is None:
        await store.db.execute(
            "UPDATE positions SET status = 'settled', closed_at = datetime('now'), "
            "exit_price = 1.0, realized_pnl = ? WHERE id = ?",
            (_pnl_from_yes_price(position, market.yes_price or 0.0), pos_id),
        )
        await store.db.commit()
        return True

    # Atomic claim — race-safe across concurrent settler runs.  Returns
    # False if another caller already flipped the row; we then skip and
    # let next cycle either find status='success' (we missed the receipt)
    # or status NULL (the previous claim rolled back) and act accordingly.
    claimed = await store.claim_redeem_attempt(pos_id)
    if not claimed:
        logger.debug(
            "Position %d already claimed for redeem — skipping this cycle",
            pos_id,
        )
        return False

    neg_risk = bool(position.get("neg_risk", 0))
    # The redeemer needs the NO token_id for its CLOB-API balance check
    # (negRisk shares aren't indexed under the standard ConditionalTokens
    # positionId, so the previous balanceOf path returned 0 — see
    # redeemer.py module docstring).  positions.token_id is the CLOB
    # asset_id we bought; it's the right input.
    result = await redeemer.redeem_position(
        cid, neg_risk, token_id=position["token_id"],
    )

    if result.status in ("success", "already_redeemed"):
        tx_hash = result.tx_hash or "already_redeemed"
        await store.complete_redeem(pos_id, tx_hash)
        # Mark the position settled with realized P&L and exit_price=1.0.
        # The yes_price is 0 for a NO winner; payout per share is $1.
        pnl = _pnl_from_yes_price(position, market.yes_price or 0.0)
        await store.db.execute(
            "UPDATE positions SET status = 'settled', closed_at = datetime('now'), "
            "exit_price = 1.0, realized_pnl = ? WHERE id = ?",
            (pnl, pos_id),
        )
        await store.db.commit()
        logger.info(
            "Redeemed position %d cid=%s tx=%s pnl=$%.2f",
            pos_id, cid[:16] + "...", tx_hash, pnl,
        )
        return True

    # All non-success outcomes (gas_too_high / rpc_error / tx_reverted /
    # no_balance / no_funder) roll back the claim so next cycle retries,
    # bumping the attempt count.  A persistent failure trips the
    # MAX_REDEEM_ATTEMPTS gate and pages the operator.
    attempt_count = await store.release_redeem_attempt(
        pos_id, max_attempts=MAX_REDEEM_ATTEMPTS,
    )
    logger.warning(
        "Redeem position %d cid=%s failed (status=%s attempt=%d): %s",
        pos_id, cid[:16] + "...", result.status, attempt_count,
        result.error or "n/a",
    )
    if attempt_count >= MAX_REDEEM_ATTEMPTS and alerter is not None:
        await alerter.send(
            "critical",
            f"Manual redeem required for position id={pos_id} "
            f"(cid={cid[:16]}…, last status={result.status}, "
            f"attempts={attempt_count}). Check Polygon gas / wallet / "
            f"on-chain finality.",
        )
    return False


async def _settle_loser(*, store: Store, position: dict) -> bool:
    """Mark a losing NO position settled at exit_price=0, realized = -cost."""
    pos_id = position["id"]
    entry_price = effective_entry_price(position)
    shares = float(position.get("shares") or 0)
    pnl = -entry_price * shares
    await store.db.execute(
        "UPDATE positions SET status = 'settled', closed_at = datetime('now'), "
        "exit_price = 0.0, realized_pnl = ? WHERE id = ?",
        (pnl, pos_id),
    )
    await store.db.commit()
    return True


async def _fetch_event_markets(
    client: httpx.AsyncClient, event_id: str,
) -> list[_MarketRecord]:
    """Pull every market under this event with the per-market status fields.

    Returns empty list on 404 / parse error; non-fatal — caller skips
    the event for this cycle and retries next one.
    """
    try:
        resp = await client.get(f"{GAMMA_API_URL}/events/{event_id}")
        if resp.status_code == 404:
            return []
        resp.raise_for_status()
        event_data = resp.json()
    except Exception:
        logger.debug("Gamma API error for event %s", event_id)
        return []

    markets = event_data.get("markets", [])
    out: list[_MarketRecord] = []
    for mkt in markets:
        outcome_prices = mkt.get("outcomePrices", [])
        if isinstance(outcome_prices, str):
            try:
                outcome_prices = json.loads(outcome_prices)
            except (json.JSONDecodeError, TypeError):
                outcome_prices = []

        yes_price: float | None = None
        if len(outcome_prices) >= 1:
            try:
                yes_price = float(outcome_prices[0])
            except (TypeError, ValueError):
                yes_price = None

        clob_token_ids = mkt.get("clobTokenIds", [])
        if isinstance(clob_token_ids, str):
            try:
                clob_token_ids = json.loads(clob_token_ids)
            except (json.JSONDecodeError, TypeError):
                clob_token_ids = []

        out.append(_MarketRecord(
            condition_id=str(mkt.get("conditionId", "")),
            closed=bool(mkt.get("closed", False)),
            yes_price=yes_price,
            clob_token_ids=[str(t) for t in clob_token_ids],
        ))
    return out


def _pnl_from_yes_price(position: dict, yes_price: float) -> float:
    """Realized P&L for a single position given the resolved YES price.

    Internal helper for the per-market settlement path.  Legacy callers
    (and the test suite) use ``_compute_position_pnl(pos, label_prices)``
    further down — that wraps this with label-keyed lookup.
    """
    entry_price = effective_entry_price(position)
    shares = float(position.get("shares") or 0)
    token_type = position.get("token_type", "NO")
    if token_type == "NO":
        if yes_price <= 0.01:
            return (1.0 - entry_price) * shares
        return -entry_price * shares
    # YES (legacy)
    if yes_price >= 0.99:
        return (1.0 - entry_price) * shares
    return -entry_price * shares


# ── Legacy helpers (preserved for back-compat tests) ─────────────────
# Pre-Phase 3 the settler exposed these as module-level functions and
# the test suite imports them by name.  The new flow doesn't use them
# (per-market detection works off the structured ``_MarketRecord``),
# but renaming would gratuitously break the test suite — so the old
# signatures are kept as thin wrappers below.

@dataclass
class SettlementOutcome:
    """Pre-Phase 3 settlement outcome dataclass — kept for back-compat tests.

    The new per-market path doesn't materialise this; it operates on
    ``_MarketRecord`` directly.  Tests built before the refactor still
    import this dataclass and the helper that returns it, so we keep
    the old shape intact.
    """
    winning_slot: str
    label_prices: dict[str, float]
    token_prices: dict[str, float]


async def _fetch_settlement_outcome(
    client: httpx.AsyncClient, event_id: str,
) -> SettlementOutcome | None:
    """Pre-Phase 3 helper: returns event-level outcome ONLY when ``closed=true``.

    The new check_settlements path does NOT call this — per-market
    detection bypasses the all-or-nothing event close.  Preserved here
    so the existing test suite (which exercises label-keyed price
    matching independently of the settler trigger) keeps working.
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

    is_closed = event_data.get("closed", False)
    markets = event_data.get("markets", [])
    if not markets:
        return None

    winning_slot: str | None = None
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

        clob_token_ids = mkt.get("clobTokenIds", [])
        if isinstance(clob_token_ids, str):
            try:
                clob_token_ids = json.loads(clob_token_ids)
            except (json.JSONDecodeError, TypeError):
                clob_token_ids = []
        if clob_token_ids:
            token_prices[clob_token_ids[0]] = yes_price
            if len(clob_token_ids) > 1:
                token_prices[clob_token_ids[1]] = yes_price

        if yes_price >= 0.99:
            winning_slot = question

    if not label_prices:
        return None
    if not is_closed:
        return None
    if winning_slot is None:
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
    """Pre-Phase 3 label/token resolver.  Test-suite contract pinned here.

    Priority: exact token_id → exact label → substring.  Substring with
    multiple matches falls back to longest match (per the
    ``test_settlement_exit_price_partial_match`` regression).
    """
    if token_id and token_prices and token_id in token_prices:
        return token_prices[token_id]
    if slot_label in label_prices:
        return label_prices[slot_label]
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
    """Legacy 0.0/1.0 exit-price resolver used by some tests."""
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
    """Legacy label-keyed P&L wrapper.

    Resolves the position's slot to a YES price (label / token-id
    lookup, falling back to 0.0 when nothing matches — the same
    "assume NO wins" failsafe the pre-Phase 3 settler used) then
    delegates to ``_pnl_from_yes_price``.
    """
    yes_resolved = _resolve_yes_price(
        position["slot_label"], label_prices,
        token_id=position.get("token_id", ""), token_prices=token_prices,
    )
    if yes_resolved is None:
        logger.warning(
            "Could not match slot %s to settlement data, assuming NO wins",
            position["slot_label"][:30],
        )
        yes_resolved = 0.0
    return _pnl_from_yes_price(position, yes_resolved)


async def _update_realized_pnl(store: Store, date_str: str, pnl: float) -> None:
    """Atomically increment realized P&L for the given date.

    Single INSERT … ON CONFLICT DO UPDATE so concurrent runs cannot lose
    increments by overwriting each other.  Same pattern as the previous
    (per-event) implementation — preserved verbatim.
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
