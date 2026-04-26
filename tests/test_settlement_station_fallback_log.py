"""C-5: when daily_max falls back to a non-primary ICAO (e.g. Denver
KBKF→KDEN), the rebalancer must persist a decision_log breadcrumb so
operators can correlate Denver-specific anomalies with the fallback.
Pre-fix the only signal was a stdout warning that scrolled away.
"""
from __future__ import annotations

import tempfile
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from src.config import AppConfig, CityConfig, SchedulingConfig, StrategyConfig
from src.execution.executor import Executor
from src.portfolio.store import Store
from src.portfolio.tracker import PortfolioTracker
from src.strategy.rebalancer import Rebalancer
from src.weather.metar import DailyMaxTracker
from src.weather.settlement import SettlementObservation


@pytest.fixture(autouse=True)
def _block_real_httpx(monkeypatch):
    class _FastFailClient:
        def __init__(self, *a, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return None
        async def get(self, *a, **kw):
            raise httpx.ConnectError("blocked")
    monkeypatch.setattr(httpx, "AsyncClient", _FastFailClient)


async def _mk_setup() -> tuple[Store, Rebalancer]:
    tmp = Path(tempfile.mkdtemp()) / "bot.db"
    store = Store(tmp)
    await store.initialize()
    tracker = PortfolioTracker(store)
    config = AppConfig(
        strategy=StrategyConfig(),
        scheduling=SchedulingConfig(),
        cities=[CityConfig("Denver", "KBKF", 39.74, -104.99, tz="America/Denver")],
        dry_run=True,
        db_path=tmp,
    )
    rebalancer = Rebalancer(
        config=config, clob=MagicMock(), portfolio=tracker,
        executor=MagicMock(spec=Executor), max_tracker=DailyMaxTracker(),
    )
    return store, rebalancer


@pytest.mark.asyncio
async def test_no_decision_log_entry_when_primary_station_succeeds():
    """Sanity: a non-fallback observation must NOT spam decision_log."""
    store, reb = await _mk_setup()

    async def _fake_fetch(city, client):
        return SettlementObservation(
            city=city, icao="KBKF", temp_f=72.0,
            observation_time=datetime.now(timezone.utc),
            source="metar", raw_data="",
            primary_icao="KBKF", used_fallback=False,
        )

    with patch("src.strategy.rebalancer.fetch_settlement_temp", side_effect=_fake_fetch):
        await reb._fetch_observations(reb._config.cities)

    async with store.db.execute(
        "SELECT COUNT(*) FROM decision_log WHERE signal_type='STATION_FALLBACK'"
    ) as cur:
        (n,) = await cur.fetchone()
    assert n == 0, "non-fallback observation must not write to decision_log"
    await store.close()


@pytest.mark.asyncio
async def test_fallback_observation_writes_decision_log_entry():
    """C-5: when fetch_settlement_temp returns used_fallback=True, the
    rebalancer must persist a STATION_FALLBACK row to decision_log."""
    store, reb = await _mk_setup()

    async def _fake_fetch(city, client):
        return SettlementObservation(
            city=city, icao="KDEN", temp_f=78.5,  # fallback station
            observation_time=datetime.now(timezone.utc),
            source="metar", raw_data="",
            primary_icao="KBKF", used_fallback=True,
        )

    with patch("src.strategy.rebalancer.fetch_settlement_temp", side_effect=_fake_fetch):
        await reb._fetch_observations(reb._config.cities)

    async with store.db.execute(
        "SELECT city, signal_type, action, reason FROM decision_log "
        "WHERE signal_type='STATION_FALLBACK'"
    ) as cur:
        rows = await cur.fetchall()
    assert len(rows) == 1, (
        "C-5: a single fallback observation must produce one decision_log row"
    )
    row = dict(rows[0])
    assert row["city"] == "Denver"
    assert row["action"] == "OBSERVE"
    # Reason must name BOTH the primary that failed and the fallback that was used
    assert "KBKF" in row["reason"]
    assert "KDEN" in row["reason"]
    assert "78.5" in row["reason"]  # observed temp from the fallback
    await store.close()


@pytest.mark.asyncio
async def test_settlement_observation_dataclass_has_fallback_fields():
    """C-5 contract: SettlementObservation must expose primary_icao
    and used_fallback so callers can inspect the fallback state."""
    obs = SettlementObservation(
        city="Denver", icao="KDEN", temp_f=80.0,
        observation_time=datetime.now(timezone.utc),
        source="metar", raw_data="",
    )
    # Defaults preserve pre-C-5 callers
    assert obs.primary_icao == ""
    assert obs.used_fallback is False
    # Explicit-fallback case
    obs2 = SettlementObservation(
        city="Denver", icao="KDEN", temp_f=80.0,
        observation_time=datetime.now(timezone.utc),
        source="metar", raw_data="",
        primary_icao="KBKF", used_fallback=True,
    )
    assert obs2.primary_icao == "KBKF"
    assert obs2.used_fallback is True
