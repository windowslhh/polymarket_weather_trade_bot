"""Cheap, batch Gamma-API price fetcher.

Used by the 15-min position check to refresh outcome prices for known
tokens without going through the full discovery / forecast / METAR
chain.  Pure read of Polymarket's ``/markets`` endpoint, batched to keep
URL length sane.

The caller (``run_position_check``) handles smoothing, exit cooldowns,
and gate evaluation; this module is a thin HTTP helper so the call
shape is identical between held-token refresh and the broader
"refresh all active-event tokens" path used by the entry scan.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Iterable

import httpx

logger = logging.getLogger(__name__)

_GAMMA_MARKETS_URL = "https://gamma-api.polymarket.com/markets"

# Polymarket rejects URL lengths > a few KB.  Each token id is ~78 chars
# and we use a repeated query param (``clob_token_ids=<id>``), so 20
# tokens per call keeps us comfortably under any URL-length ceiling.
DEFAULT_BATCH_SIZE = 20

# Conservative timeout — Gamma typically responds in < 1s; 10s gives
# headroom for tail latency without freezing the position-check cycle.
DEFAULT_TIMEOUT_S = 10.0


async def _fetch_one_batch(
    client: httpx.AsyncClient, batch: list[str], batch_idx: int,
) -> dict[str, float]:
    """Fetch a single batch's prices.  Returns {token: price} for the
    tokens this batch's response covered; an empty dict on per-batch
    failure (logged at WARNING).  Never raises — caller uses
    ``return_exceptions`` semantics via ``asyncio.gather``."""
    out: dict[str, float] = {}
    try:
        resp = await client.get(
            _GAMMA_MARKETS_URL,
            params=[("clob_token_ids", tid) for tid in batch],
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception:
        logger.warning(
            "Gamma price batch fetch failed (batch %d, %d tokens)",
            batch_idx, len(batch), exc_info=True,
        )
        return out

    if not isinstance(data, list):
        return out
    for mkt in data:
        toks = mkt.get("clobTokenIds", [])
        pxs = mkt.get("outcomePrices", [])
        if isinstance(toks, str):
            try:
                toks = json.loads(toks)
            except Exception:
                toks = []
        if isinstance(pxs, str):
            try:
                pxs = json.loads(pxs)
            except Exception:
                pxs = []
        for tid, px in zip(toks, pxs):
            try:
                out[tid] = float(px)
            except (ValueError, TypeError):
                # Gamma occasionally returns "" or null for markets in
                # odd states — skip silently so the caller's stale
                # cache stays canonical for those tokens.
                pass
    return out


async def refresh_gamma_prices_only(
    token_ids: Iterable[str],
    *,
    batch_size: int = DEFAULT_BATCH_SIZE,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> dict[str, float]:
    """Batch-fetch Polymarket Gamma ``outcomePrices`` for ``token_ids``.

    Returns ``{token_id: price}`` for every token Gamma returned a parseable
    price for; tokens that fail or are missing from the response are simply
    omitted so the caller can fall back to its existing price cache.

    Does NOT touch:
    - NWS / Open-Meteo forecast caches
    - METAR observations
    - DailyMaxTracker
    - Local DB

    Errors per batch are logged at WARNING and swallowed (asyncio.gather
    runs with ``return_exceptions=True``): a partial refresh is more
    useful than a hard fail when some tokens are transiently 5xx-ing.

    cycle-fix-9: batches run concurrently via ``asyncio.gather``.  At 30
    events × 2 outcomes × ~30 tokens/event = ~60 tokens → 3 batches.
    Pre-fix: serial 3×~1s = 3s.  Post-fix: 1×~1s + small fan-out
    overhead.  Failure of one batch does not abort the others.
    """
    # Materialise + dedup so order is stable AND a caller passing a
    # set/generator still gets a sensible batch split.
    tokens = list({tid: None for tid in token_ids})
    prices: dict[str, float] = {}
    if not tokens:
        return prices

    batches = [
        tokens[i:i + batch_size]
        for i in range(0, len(tokens), batch_size)
    ]

    try:
        async with httpx.AsyncClient(timeout=timeout_s) as client:
            results = await asyncio.gather(
                *(_fetch_one_batch(client, b, idx) for idx, b in enumerate(batches)),
                return_exceptions=True,
            )
        for r in results:
            if isinstance(r, Exception):
                # _fetch_one_batch already logs per-batch failures and
                # returns {}.  This branch fires only for an exception
                # raised OUTSIDE the helper — log once at the gather
                # level and carry on with whatever we did get.
                logger.warning(
                    "Gamma price gather caught uncaught exception: %r", r,
                )
                continue
            prices.update(r)
    except Exception:
        logger.warning(
            "Gamma price refresh failed at the client level; returning %d "
            "tokens already collected", len(prices), exc_info=True,
        )
    return prices
