"""Shared HTTP utilities for Open-Meteo API requests.

Provides concurrency control (semaphore) and retry with exponential backoff
to avoid 429 rate-limit errors when fetching weather data for many cities.
"""
from __future__ import annotations

import asyncio
import logging
import random

import httpx

logger = logging.getLogger(__name__)

MAX_CONCURRENT = 5
REQUEST_DELAY = 0.2
MAX_RETRIES = 4
BASE_BACKOFF = 1.0

# Lazy-initialized semaphore (must be created inside a running event loop)
_semaphore: asyncio.Semaphore | None = None


def _get_semaphore() -> asyncio.Semaphore:
    global _semaphore
    if _semaphore is None:
        _semaphore = asyncio.Semaphore(MAX_CONCURRENT)
    return _semaphore


async def fetch_with_retry(
    client: httpx.AsyncClient,
    url: str,
    params: dict,
    *,
    max_retries: int = MAX_RETRIES,
    base_delay: float = BASE_BACKOFF,
) -> dict:
    """Fetch JSON from a URL with concurrency limiting and retry on 429/5xx.

    Acquires a shared semaphore before each attempt, retries with exponential
    backoff on transient failures, and adds a small delay after each request
    to spread load.
    """
    sem = _get_semaphore()
    last_exc: Exception | None = None

    for attempt in range(max_retries + 1):
        async with sem:
            try:
                resp = await client.get(url, params=params)
                resp.raise_for_status()
                await asyncio.sleep(REQUEST_DELAY)
                return resp.json()
            except httpx.HTTPStatusError as exc:
                last_exc = exc
                status = exc.response.status_code
                if status == 429 or status >= 500:
                    if attempt < max_retries:
                        retry_after = exc.response.headers.get("Retry-After")
                        if retry_after and retry_after.isdigit():
                            delay = float(retry_after)
                        else:
                            delay = base_delay * (2 ** attempt) + random.uniform(0, 0.5)
                        logger.warning(
                            "HTTP %d from %s (attempt %d/%d), retrying in %.1fs",
                            status, url.split("/")[-1], attempt + 1, max_retries + 1, delay,
                        )
                        await asyncio.sleep(delay)
                        continue
                raise
            except (httpx.TimeoutException, httpx.ConnectError) as exc:
                last_exc = exc
                if attempt < max_retries:
                    delay = base_delay * (2 ** attempt) + random.uniform(0, 0.5)
                    logger.warning(
                        "Connection error from %s (attempt %d/%d), retrying in %.1fs",
                        url.split("/")[-1], attempt + 1, max_retries + 1, delay,
                    )
                    await asyncio.sleep(delay)
                    continue
                raise

    raise last_exc  # type: ignore[misc]
