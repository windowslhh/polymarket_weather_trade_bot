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

import json
import logging

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


async def refresh_gamma_prices_only(
    token_ids: list[str],
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

    Errors per batch are logged at WARNING and swallowed: a partial
    refresh is more useful than a hard fail when some tokens are
    transiently 5xx-ing.
    """
    prices: dict[str, float] = {}
    if not token_ids:
        return prices

    try:
        async with httpx.AsyncClient(timeout=timeout_s) as client:
            for i in range(0, len(token_ids), batch_size):
                batch = token_ids[i:i + batch_size]
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
                        i // batch_size, len(batch), exc_info=True,
                    )
                    continue

                if not isinstance(data, list):
                    continue
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
                            prices[tid] = float(px)
                        except (ValueError, TypeError):
                            # Gamma occasionally returns "" or null for
                            # markets in odd states — skip silently so
                            # the caller's stale cache stays canonical
                            # for those tokens until the next refresh.
                            pass
    except Exception:
        logger.warning(
            "Gamma price refresh failed at the client level; returning %d "
            "tokens already collected", len(prices), exc_info=True,
        )
    return prices
