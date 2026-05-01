"""GF-1/2/3: ghost-fill recovery for BUY orders the bot mis-marked failed.

Production case (2026-05-01): orders #521 and #522 returned ``status=delayed
matched=False`` from CLOB, the executor wrapper saw ``success=False`` and
flipped them to ``failed`` — but Polymarket's server asynchronously matched
both within seconds, leaving 12.26 NO shares on chain that the DB had no
record of.

Three call sites, all sharing the same primitive in this module:

- **Inline (executor.py)** — ``GF-1`` BUY late-fill probe runs at order
  submission time and writes the position via ``record_fill_atomic`` (it
  has full signal context: city, slot, strategy).  Doesn't use this
  module — it's the cheapest fix and preserves the synchronous flow.
- **Reconciler (reconciler.py)** — ``GF-3`` runs on startup, scans
  ``status='failed' AND failure_reason LIKE '%delayed%'`` BUY rows, and
  retroactively recovers any that filled on chain.  Uses this module.
- **One-shot script (scripts/recover_ghost_buys.py)** — ``GF-2`` runs by
  hand against specific order IDs (the current #521/#522 case).  Also
  uses this module.

The reconciler and the script need event metadata (city, slot_label,
token_type) that the orders table doesn't carry, so this module fetches
it from Gamma at recovery time.  The executor doesn't need any of this
because it has the live ``signal`` object.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx

logger = logging.getLogger(__name__)

GAMMA_API_URL = "https://gamma-api.polymarket.com"


@dataclass
class _SlotMeta:
    """Subset of Gamma event/market data needed to materialise a position."""
    city: str
    slot_label: str
    token_type: str  # 'YES' or 'NO'


async def _fetch_slot_metadata(
    client: httpx.AsyncClient, event_id: str, token_id: str,
) -> _SlotMeta | None:
    """Look up city / slot_label / token_type from Gamma /events/{id}.

    Returns None on any error so the caller can skip this row and move
    on (we'd rather miss one ghost recovery than wedge the whole
    reconciler).
    """
    try:
        resp = await client.get(f"{GAMMA_API_URL}/events/{event_id}")
        if resp.status_code != 200:
            logger.warning(
                "Ghost recovery: Gamma /events/%s returned %d",
                event_id, resp.status_code,
            )
            return None
        event_data = resp.json()
    except Exception:
        logger.warning(
            "Ghost recovery: Gamma fetch raised for event=%s",
            event_id, exc_info=True,
        )
        return None

    # event-level city: Polymarket weather events embed the city in the
    # event title (e.g. "Highest temperature in Miami on April 30?").
    title = (event_data.get("title") or "").strip()
    city = ""
    for known in (
        "Atlanta", "Boston", "Chicago", "Cincinnati", "Cleveland",
        "Dallas", "Denver", "Detroit", "Houston", "Indianapolis",
        "Kansas City", "Las Vegas", "Los Angeles", "Memphis", "Miami",
        "Minneapolis", "Nashville", "New York", "Orlando", "Philadelphia",
        "Phoenix", "Pittsburgh", "Portland", "Salt Lake City", "San Antonio",
        "San Francisco", "Seattle", "St. Louis", "Tampa",
    ):
        if known in title:
            city = known
            break

    # market-level slot_label: per-market `groupItemTitle` or `question`.
    # outcomePrices and clobTokenIds are JSON-encoded strings on Gamma —
    # parse them so we can match our token_id against the YES/NO pair.
    for mkt in event_data.get("markets", []) or []:
        token_ids = mkt.get("clobTokenIds")
        if isinstance(token_ids, str):
            try:
                token_ids = json.loads(token_ids)
            except (json.JSONDecodeError, TypeError):
                token_ids = []
        if not isinstance(token_ids, list):
            continue
        if str(token_id) not in [str(t) for t in token_ids]:
            continue
        # Match the source-of-truth ordering in markets/discovery.py:261
        # so recovered slot_labels are byte-identical to live-discovered
        # ones — keeps dashboard / audit greps consistent.  Fallback chain:
        # question → groupItemTitle → "" (Gamma sometimes drops one or
        # the other on partial-resolve markets).
        slot_label = (
            mkt.get("question")
            or mkt.get("groupItemTitle")
            or ""
        ).strip()
        # Polymarket convention: outcomePrices[0]=YES, [1]=NO,
        # mirroring clobTokenIds[0]=YES_token, [1]=NO_token.
        token_type = "YES" if str(token_ids[0]) == str(token_id) else "NO"
        if not city:
            # fall back to per-market title parse if event title was empty
            for known in (
                "Atlanta", "Boston", "Chicago", "Cincinnati", "Cleveland",
                "Dallas", "Denver", "Detroit", "Houston", "Indianapolis",
                "Kansas City", "Las Vegas", "Los Angeles", "Memphis", "Miami",
                "Minneapolis", "Nashville", "New York", "Orlando",
                "Philadelphia", "Phoenix", "Pittsburgh", "Portland",
                "Salt Lake City", "San Antonio", "San Francisco", "Seattle",
                "St. Louis", "Tampa",
            ):
                if known in slot_label:
                    city = known
                    break
        return _SlotMeta(city=city, slot_label=slot_label, token_type=token_type)
    return None


async def recover_one_ghost_fill(
    *,
    store,
    clob_client,
    failed_order_row: dict,
    http_client: httpx.AsyncClient | None = None,
) -> tuple[bool, str]:
    """Recover a single failed-delayed BUY order if on-chain shows a fill.

    Returns ``(recovered, message)``:
    - (True, "...")  → position written; orders row promoted to 'filled'
    - (False, "...") → no fill on chain; row left as 'failed'

    Caller owns the http_client lifecycle when running multiple
    recoveries (the script does its own ``async with``); pass None for
    one-off use and we'll create a short-lived one.
    """
    order_id = failed_order_row.get("order_id")
    token_id = failed_order_row.get("token_id")
    event_id = failed_order_row.get("event_id")
    if not order_id or not token_id or not event_id:
        return False, "missing order_id / token_id / event_id"

    # CLOB created_at_epoch is best-effort: parse from orders.created_at,
    # rewind 5 min for clock skew (matches main.py:183 reconciler adapter).
    created_at_epoch: int | None = None
    raw_created = failed_order_row.get("created_at")
    if raw_created:
        try:
            dt = datetime.fromisoformat(str(raw_created).replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            created_at_epoch = int(dt.timestamp()) - 300
        except ValueError:
            pass

    try:
        summary = await clob_client.get_fill_summary(
            token_id=token_id,
            order_id=order_id,
            created_at_epoch=created_at_epoch,
        )
    except Exception as exc:
        return False, f"get_fill_summary raised: {exc}"

    if summary is None or summary.shares <= 0:
        return False, "no fill on chain"

    # Need event metadata for the position row.  Reuse caller's http_client
    # if provided; otherwise spin up a 15s-timeout one for this single call.
    own_client = http_client is None
    if own_client:
        http_client = httpx.AsyncClient(timeout=15)
    try:
        meta = await _fetch_slot_metadata(http_client, str(event_id), str(token_id))
    finally:
        if own_client:
            await http_client.aclose()

    if meta is None or not meta.slot_label:
        return False, "Gamma metadata lookup failed"

    # size_usd reflects on-chain notional (matches the Bug-C-followup fix in
    # tracker.record_fill_atomic): actual_shares × match_price.
    actual_size_usd = summary.net_shares * summary.match_price
    today = datetime.now(timezone.utc).strftime("%Y_%m_%d")
    pos_id = await store.recover_ghost_buy_fill(
        failed_order_id=int(failed_order_row["id"]),
        clob_order_id=str(order_id),
        event_id=str(event_id),
        token_id=str(token_id),
        token_type=meta.token_type,
        city=meta.city or "Unknown",
        slot_label=meta.slot_label,
        entry_price=float(failed_order_row["price"]),
        size_usd=actual_size_usd,
        shares=summary.net_shares,
        strategy=str(failed_order_row.get("strategy") or "D"),
        buy_reason=f"ghost_recovered_{today}",
        match_price=summary.match_price,
        fee_paid_usd=summary.fee_paid_usd,
    )
    if pos_id == -1:
        return False, "position already exists (idempotent skip)"
    return True, (
        f"position id={pos_id} shares={summary.net_shares:.4f} "
        f"match=${summary.match_price:.4f} "
        f"size=${actual_size_usd:.2f}"
    )
