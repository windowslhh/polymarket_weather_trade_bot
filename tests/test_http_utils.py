"""Tests for the shared HTTP retry/throttle utilities."""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from src.weather import http_utils
from src.weather.http_utils import fetch_with_retry


def _make_response(status: int, json_data: dict | None = None, headers: dict | None = None):
    """Create a mock httpx.Response."""
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status
    resp.headers = headers or {}
    resp.json.return_value = json_data or {}
    if status >= 400:
        request = MagicMock(spec=httpx.Request)
        request.url = "https://example.com/test"
        resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            f"HTTP {status}", request=request, response=resp,
        )
    else:
        resp.raise_for_status.return_value = None
    return resp


@pytest.fixture(autouse=True)
def _reset_semaphore():
    """Reset the module-level semaphore between tests."""
    http_utils._semaphore = None
    yield
    http_utils._semaphore = None


@pytest.mark.asyncio
async def test_fetch_success():
    client = AsyncMock(spec=httpx.AsyncClient)
    client.get.return_value = _make_response(200, {"temp": 72})

    result = await fetch_with_retry(client, "https://example.com", {"q": "1"}, base_delay=0.01)
    assert result == {"temp": 72}
    client.get.assert_called_once()


@pytest.mark.asyncio
async def test_retry_on_429_then_success():
    client = AsyncMock(spec=httpx.AsyncClient)
    client.get.side_effect = [
        _make_response(429),
        _make_response(200, {"temp": 65}),
    ]

    result = await fetch_with_retry(client, "https://example.com", {}, base_delay=0.01)
    assert result == {"temp": 65}
    assert client.get.call_count == 2


@pytest.mark.asyncio
async def test_retry_on_500_then_success():
    client = AsyncMock(spec=httpx.AsyncClient)
    client.get.side_effect = [
        _make_response(500),
        _make_response(200, {"temp": 80}),
    ]

    result = await fetch_with_retry(client, "https://example.com", {}, base_delay=0.01)
    assert result == {"temp": 80}
    assert client.get.call_count == 2


@pytest.mark.asyncio
async def test_retries_exhausted_raises():
    client = AsyncMock(spec=httpx.AsyncClient)
    client.get.return_value = _make_response(429)

    with pytest.raises(httpx.HTTPStatusError):
        await fetch_with_retry(client, "https://example.com", {}, max_retries=2, base_delay=0.01)
    assert client.get.call_count == 3  # initial + 2 retries


@pytest.mark.asyncio
async def test_4xx_not_retried():
    client = AsyncMock(spec=httpx.AsyncClient)
    client.get.return_value = _make_response(404)

    with pytest.raises(httpx.HTTPStatusError):
        await fetch_with_retry(client, "https://example.com", {}, base_delay=0.01)
    assert client.get.call_count == 1


@pytest.mark.asyncio
async def test_retry_after_header_respected():
    resp_429 = _make_response(429, headers={"Retry-After": "1"})
    client = AsyncMock(spec=httpx.AsyncClient)
    client.get.side_effect = [resp_429, _make_response(200, {"ok": True})]

    result = await fetch_with_retry(client, "https://example.com", {}, base_delay=0.01)
    assert result == {"ok": True}


@pytest.mark.asyncio
async def test_semaphore_limits_concurrency():
    """Verify no more than MAX_CONCURRENT requests are in-flight at once."""
    peak = 0
    current = 0
    lock = asyncio.Lock()

    async def slow_get(*args, **kwargs):
        nonlocal peak, current
        async with lock:
            current += 1
            if current > peak:
                peak = current
        await asyncio.sleep(0.05)
        async with lock:
            current -= 1
        return _make_response(200, {"ok": True})

    client = AsyncMock(spec=httpx.AsyncClient)
    client.get.side_effect = slow_get

    tasks = [fetch_with_retry(client, "https://example.com", {"i": i}, base_delay=0.01) for i in range(15)]
    await asyncio.gather(*tasks)

    assert peak <= http_utils.MAX_CONCURRENT


@pytest.mark.asyncio
async def test_timeout_retried():
    client = AsyncMock(spec=httpx.AsyncClient)
    client.get.side_effect = [
        httpx.TimeoutException("timeout"),
        _make_response(200, {"temp": 55}),
    ]

    result = await fetch_with_retry(client, "https://example.com", {}, base_delay=0.01)
    assert result == {"temp": 55}
    assert client.get.call_count == 2
