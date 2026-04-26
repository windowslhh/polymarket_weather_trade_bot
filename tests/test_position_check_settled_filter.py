"""BUG-5: position-check filters out tokens already settled (price 0/1).

Once a Polymarket event resolves, Gamma reports the NO token at exactly
0.0 (lost) or 1.0 (won).  Pre-fix the position-check would feed those
prices into the strategy layer where:
  - PriceStopGate explicitly defends against `<= 0` (a residual guard
    from this exact scenario)
  - But other gates compute EV using the 0/1 price as if it were a
    mid-market quote, producing nonsense (e.g. EV = -entry_price for a
    NO at 1.0) and spurious TRIM signals on positions the settler is
    seconds away from closing anyway.

The fix is one filter at the held_no_slots construction site in
run_position_check: when cached_price is exactly 0.0 or 1.0, skip the
token (the settler will close it on the next cycle).
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest


@pytest.fixture(autouse=True)
def _block_gamma_refresh(monkeypatch):
    """Avoid the 5-10s real httpx call inside run_position_check's price
    refresh.  We don't care about it for these tests — the relevant
    state is `_last_gamma_prices` which is set explicitly per test."""

    class _FastFailClient:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return None

        async def get(self, *a, **kw):
            raise httpx.ConnectError("blocked by test fixture")

    monkeypatch.setattr(httpx, "AsyncClient", _FastFailClient)

from src.config import AppConfig, CityConfig, SchedulingConfig, StrategyConfig
from src.execution.executor import Executor
from src.markets.models import Side
from src.portfolio.tracker import PortfolioTracker
from src.strategy.rebalancer import Rebalancer
from src.weather.metar import DailyMaxTracker
from src.weather.models import Forecast


def _make_config() -> AppConfig:
    return AppConfig(
        strategy=StrategyConfig(
            no_distance_threshold_f=8,
            min_no_ev=0.01,
            max_no_price=0.95,
            enable_locked_wins=True,
        ),
        scheduling=SchedulingConfig(),
        cities=[
            CityConfig("New York", "KLGA", 40.7128, -74.006, tz="America/New_York"),
        ],
        dry_run=True,
        db_path=Path("/tmp/test_position_check_settled.db"),
    )


def _make_rebalancer(positions: list[dict]) -> Rebalancer:
    config = _make_config()
    mock_clob = MagicMock()
    mock_portfolio = MagicMock(spec=PortfolioTracker)
    mock_executor = MagicMock(spec=Executor)
    mock_executor.execute_signals = AsyncMock(return_value=[])

    mock_portfolio.get_all_open_positions = AsyncMock(return_value=positions)
    mock_portfolio.get_city_exposure = AsyncMock(return_value=0.0)
    mock_portfolio.get_total_exposure = AsyncMock(return_value=0.0)
    mock_portfolio.get_daily_pnl = AsyncMock(return_value=None)
    mock_portfolio.record_exit_cooldown = AsyncMock()
    mock_portfolio.load_active_exit_cooldowns = AsyncMock(return_value={})
    mock_portfolio.store = MagicMock()
    mock_portfolio.store.get_bot_paused = AsyncMock(return_value=False)

    rebalancer = Rebalancer(
        config=config, clob=mock_clob, portfolio=mock_portfolio,
        executor=mock_executor, max_tracker=DailyMaxTracker(),
    )
    rebalancer.refresh_forecasts = AsyncMock(return_value=None)

    # Seed the city-local forecast cache so FIX-22 doesn't blow up.
    from datetime import datetime as _dt, timezone as _tz
    from zoneinfo import ZoneInfo as _Zi
    candidate_dates = {_dt.now(_tz.utc).date()}
    for c in config.cities:
        if c.tz:
            candidate_dates.add(_dt.now(_Zi(c.tz)).date())
    for d in candidate_dates:
        rebalancer._cached_forecasts_by_date[d] = {
            c.name: Forecast(
                city=c.name, forecast_date=d,
                predicted_high_f=78.0, predicted_low_f=60.0,
                confidence_interval_f=4.0, source="test",
                fetched_at=_dt.now(_tz.utc),
            ) for c in config.cities
        }
    return rebalancer


def _pos(token_id: str, slot_label: str = "80°F to 84°F",
         entry_price: float = 0.90, strategy: str = "B") -> dict:
    return {
        "id": 1, "event_id": "ev1", "city": "New York",
        "token_id": token_id, "token_type": "NO",
        "side": "BUY", "slot_label": slot_label, "strategy": strategy,
        "entry_price": entry_price, "size_usd": 5.0,
        "shares": 5.0 / entry_price, "status": "open",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "closed_at": None,
    }


class _Obs:
    def __init__(self, icao, temp_f):
        self.icao = icao
        self.temp_f = temp_f
        self.observation_time = datetime.now(timezone.utc)
        self.raw_data = ""


@pytest.mark.asyncio
async def test_settled_no_token_at_price_zero_filtered_from_position_check():
    """A position whose Gamma price = 0.0 (NO lost on resolution) must
    not reach the strategy layer.  Pre-fix this could trip TRIM /
    spurious EV calculations."""
    pos = _pos(token_id="no_settled_lost")
    reb = _make_rebalancer([pos])
    reb._last_gamma_prices = {"no_settled_lost": 0.0}

    async def mock_fetch(city, client):
        return _Obs("KLGA", 82.0) if city == "New York" else None

    with patch("src.strategy.rebalancer.fetch_settlement_temp", side_effect=mock_fetch), \
         patch("src.strategy.rebalancer.evaluate_locked_win_signals") as m_lock, \
         patch("src.strategy.rebalancer.evaluate_exit_signals") as m_exit, \
         patch("src.strategy.rebalancer.evaluate_trim_signals") as m_trim:
        m_lock.return_value = []
        m_exit.return_value = []
        m_trim.return_value = []
        signals = await reb.run_position_check()

    # No signals because the only held token was filtered out (settled).
    assert signals == []
    # held_no_slots is arg index 3 for evaluate_exit_signals and arg
    # index 2 for evaluate_trim_signals.  evaluate_locked_win_signals
    # uses held_token_ids (a set of token id strings) at arg index 3.
    for call in m_exit.call_args_list:
        held_slots = call.args[3] if len(call.args) > 3 else []
        assert all(s.token_id_no != "no_settled_lost" for s in held_slots), (
            "BUG-5: settled token reached evaluate_exit_signals"
        )
    for call in m_trim.call_args_list:
        held_slots = call.args[2] if len(call.args) > 2 else []
        assert all(s.token_id_no != "no_settled_lost" for s in held_slots), (
            "BUG-5: settled token reached evaluate_trim_signals"
        )
    for call in m_lock.call_args_list:
        held_ids = call.args[3] if len(call.args) > 3 else set()
        assert "no_settled_lost" not in held_ids, (
            "BUG-5: settled token reached evaluate_locked_win_signals"
        )


@pytest.mark.asyncio
async def test_settled_no_token_at_price_one_filtered_from_position_check():
    """Same as above, NO token at 1.0 (NO won — about to settle in our favor).
    Strategy must not see it; settler will book the win on its next cycle."""
    pos = _pos(token_id="no_settled_won")
    reb = _make_rebalancer([pos])
    reb._last_gamma_prices = {"no_settled_won": 1.0}

    async def mock_fetch(city, client):
        return _Obs("KLGA", 70.0) if city == "New York" else None  # under slot

    with patch("src.strategy.rebalancer.fetch_settlement_temp", side_effect=mock_fetch), \
         patch("src.strategy.rebalancer.evaluate_trim_signals") as m_trim, \
         patch("src.strategy.rebalancer.evaluate_exit_signals") as m_exit, \
         patch("src.strategy.rebalancer.evaluate_locked_win_signals") as m_lock:
        m_trim.return_value = []
        m_exit.return_value = []
        m_lock.return_value = []
        await reb.run_position_check()

    for call in m_exit.call_args_list:
        held_slots = call.args[3] if len(call.args) > 3 else []
        assert all(s.token_id_no != "no_settled_won" for s in held_slots)
    for call in m_trim.call_args_list:
        held_slots = call.args[2] if len(call.args) > 2 else []
        assert all(s.token_id_no != "no_settled_won" for s in held_slots)
    for call in m_lock.call_args_list:
        held_ids = call.args[3] if len(call.args) > 3 else set()
        assert "no_settled_won" not in held_ids


@pytest.mark.asyncio
async def test_active_token_at_mid_price_still_evaluated():
    """Sanity / regression: a healthy mid-market token MUST still reach
    the strategy layer.  Don't accidentally filter live positions."""
    pos = _pos(token_id="no_active", entry_price=0.6)
    reb = _make_rebalancer([pos])
    reb._last_gamma_prices = {"no_active": 0.55}  # mid-market

    async def mock_fetch(city, client):
        return _Obs("KLGA", 75.0) if city == "New York" else None

    with patch("src.strategy.rebalancer.fetch_settlement_temp", side_effect=mock_fetch), \
         patch("src.strategy.rebalancer.evaluate_trim_signals") as m_trim, \
         patch("src.strategy.rebalancer.evaluate_exit_signals") as m_exit, \
         patch("src.strategy.rebalancer.evaluate_locked_win_signals") as m_lock:
        m_trim.return_value = []
        m_exit.return_value = []
        m_lock.return_value = []
        await reb.run_position_check()

    # At least one of exit/trim was called with the active slot, OR
    # locked_win saw the token id in its held set.
    saw_active_slot = False
    for call in m_exit.call_args_list:
        held = call.args[3] if len(call.args) > 3 else []
        if any(s.token_id_no == "no_active" for s in held):
            saw_active_slot = True
            break
    if not saw_active_slot:
        for call in m_trim.call_args_list:
            held = call.args[2] if len(call.args) > 2 else []
            if any(s.token_id_no == "no_active" for s in held):
                saw_active_slot = True
                break
    if not saw_active_slot:
        for call in m_lock.call_args_list:
            held_ids = call.args[3] if len(call.args) > 3 else set()
            if "no_active" in held_ids:
                saw_active_slot = True
                break
    assert saw_active_slot, (
        "regression: active mid-market token was filtered out of held_no_slots"
    )


@pytest.mark.asyncio
async def test_no_gamma_price_falls_back_to_entry_price_not_filtered():
    """When Gamma has no entry for a token (cold start), we used to fall
    back to entry_price.  That fallback must still run; the BUG-5 filter
    only triggers when a real 0/1 price is observed."""
    pos = _pos(token_id="no_uncached", entry_price=0.7)
    reb = _make_rebalancer([pos])
    reb._last_gamma_prices = {}  # nothing cached

    async def mock_fetch(city, client):
        return _Obs("KLGA", 75.0) if city == "New York" else None

    with patch("src.strategy.rebalancer.fetch_settlement_temp", side_effect=mock_fetch), \
         patch("src.strategy.rebalancer.evaluate_trim_signals") as m_trim, \
         patch("src.strategy.rebalancer.evaluate_exit_signals") as m_exit, \
         patch("src.strategy.rebalancer.evaluate_locked_win_signals") as m_lock:
        m_trim.return_value = []
        m_exit.return_value = []
        m_lock.return_value = []
        await reb.run_position_check()

    # The token reaches the strategy layer at entry_price (0.7), NOT 0/1.
    saw_at_entry_price = False
    for call in m_exit.call_args_list:
        held = call.args[3] if len(call.args) > 3 else []
        for slot in held:
            if slot.token_id_no == "no_uncached":
                assert slot.price_no == 0.7
                saw_at_entry_price = True
    for call in m_trim.call_args_list:
        held = call.args[2] if len(call.args) > 2 else []
        for slot in held:
            if slot.token_id_no == "no_uncached":
                assert slot.price_no == 0.7
                saw_at_entry_price = True
    assert saw_at_entry_price, "uncached token must use entry_price fallback"
