"""Phase 4: 60-min rebalancer cycle must pass ``market_states`` to all four
evaluators (parity with the 15-min ``run_position_check`` path).

Pre-fix the full ``run()`` cycle invoked ``evaluate_no_signals`` and friends
with ``market_states=None`` (the legacy default), so a market that flipped
``closed=true`` between cycles still produced SELL/TRIM/EXIT signals → CLOB
rejection storm in the logs until the next 15-min position_check rebuilt
the lifecycle map.  Fix wires ``refresh_gamma_market_data`` + ``classify_market``
into the cycle setup, and threads the resulting dict into every evaluator
call (``rebalancer.py:1748–1818``).

Test strategy: mock external IO (discovery, forecasts, observations,
station validation, Gamma market-data) and patch the four evaluators in
the rebalancer module so we can inspect the ``market_states`` kwarg they
receive.  Verifying the wiring at this level keeps the test cheap and
robust to internal refactors of the evaluators themselves.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from src.config import AppConfig, CityConfig, SchedulingConfig, StrategyConfig
from src.execution.executor import Executor
from src.markets.clob_client import ClobClient
from src.markets.models import TempSlot, WeatherMarketEvent
from src.portfolio.store import Store
from src.portfolio.tracker import PortfolioTracker
from src.strategy.market_state import MarketState
from src.strategy.rebalancer import Rebalancer
from src.weather.metar import DailyMaxTracker
from src.weather.models import Forecast


def _make_config() -> AppConfig:
    return AppConfig(
        paper=True,
        strategy=StrategyConfig(
            no_distance_threshold_f=8,
            min_no_ev=0.01,
            max_position_per_slot_usd=5.0,
            max_exposure_per_city_usd=50.0,
            max_total_exposure_usd=1000.0,
            daily_loss_limit_usd=50.0,
            kelly_fraction=0.5,
        ),
        scheduling=SchedulingConfig(),
        cities=[CityConfig("New York", "KLGA", 40.7128, -74.006)],
        dry_run=False,
        db_path=Path("/tmp/test_bot_market_states.db"),
    )


def _make_event() -> WeatherMarketEvent:
    slot = TempSlot(
        token_id_yes="yes_72_74",
        token_id_no="no_72_74",
        outcome_label="72°F to 74°F",
        temp_lower_f=72.0, temp_upper_f=74.0,
        price_yes=0.40, price_no=0.60,
    )
    return WeatherMarketEvent(
        event_id="evt_nyc",
        condition_id="cond_nyc",
        city="New York",
        market_date=date.today(),
        slots=[slot],
        end_timestamp=datetime.now(timezone.utc) + timedelta(hours=12),
        title="Highest temperature in New York on test-day",
    )


@pytest.fixture
async def portfolio(tmp_path):
    store = Store(tmp_path / "test.db")
    await store.initialize()
    tracker = PortfolioTracker(store)
    yield tracker
    await store.close()


@pytest.mark.asyncio
async def test_60min_cycle_passes_market_states_to_all_evaluators(portfolio):
    """Regression: every evaluator call in ``Rebalancer.run()`` must receive
    a ``market_states`` dict (not the legacy ``None``).  When Gamma reports
    a market as ``closed=true`` with NO at the rail, the dict must classify
    that token as ``RESOLVED_WINNER`` so downstream gates short-circuit.

    Mirror of ``run_position_check``'s lifecycle filter at
    ``rebalancer.py:612–652``; the parity prevents the inter-cycle window
    where 60-min would generate stale SELLs while 15-min wouldn't.
    """
    config = _make_config()
    clob = ClobClient(config)
    executor = Executor(clob, portfolio)
    max_tracker = DailyMaxTracker()

    event = _make_event()
    market_date = event.market_date
    by_date_window = {
        market_date: {
            "New York": Forecast(
                "New York", market_date, 70.0, 60.0, 4.0, "mock",
                datetime.now(timezone.utc),
            ),
        },
    }

    rebalancer = Rebalancer(config, clob, portfolio, executor, max_tracker)

    # Resolved-NO payload: closed=true + outcomePrices=[YES=0, NO=1].
    # ``classify_market`` with these inputs returns RESOLVED_WINNER.
    closed_market_dict = {
        "closed": True,
        "outcomePrices": '["0", "1"]',
        "conditionId": event.condition_id,
    }

    captured: dict[str, dict] = {}

    def _capture(name):
        def _fn(*args, **kwargs):
            captured[name] = kwargs
            return []
        return _fn

    with (
        patch("src.strategy.rebalancer.discover_weather_markets",
              new_callable=AsyncMock) as mock_discover,
        patch("src.strategy.rebalancer.get_forecasts_for_city_local_window",
              new_callable=AsyncMock) as mock_forecasts,
        patch("src.strategy.rebalancer.fetch_settlement_temp",
              new_callable=AsyncMock) as mock_settle,
        patch("src.strategy.rebalancer.validate_station_config",
              return_value=[]),
        patch("src.strategy.rebalancer.refresh_gamma_market_data",
              new_callable=AsyncMock) as mock_market_data,
        patch("src.strategy.rebalancer.evaluate_no_signals",
              side_effect=_capture("no")),
        patch("src.strategy.rebalancer.evaluate_locked_win_signals",
              side_effect=_capture("locked")),
        patch("src.strategy.rebalancer.evaluate_exit_signals",
              side_effect=_capture("exit")),
        patch("src.strategy.rebalancer.evaluate_trim_signals",
              side_effect=_capture("trim")),
    ):
        mock_discover.return_value = [event]
        mock_forecasts.return_value = by_date_window
        mock_settle.return_value = None
        mock_market_data.return_value = {"no_72_74": closed_market_dict}

        await rebalancer.run()

    # Every evaluator must have been called once with market_states populated.
    for name in ("no", "locked", "exit", "trim"):
        assert name in captured, f"{name} evaluator was not called"
        kwargs = captured[name]
        assert "market_states" in kwargs, (
            f"{name} evaluator called without market_states kwarg "
            f"(legacy None path — regression of Fix B)"
        )
        market_states = kwargs["market_states"]
        assert market_states is not None, (
            f"{name} evaluator received market_states=None despite Gamma "
            f"data being available — should be the populated dict"
        )
        # The single NO token in our event must be classified as RESOLVED_WINNER.
        assert market_states.get("no_72_74") == MarketState.RESOLVED_WINNER, (
            f"{name} evaluator: expected no_72_74 → RESOLVED_WINNER, "
            f"got {market_states.get('no_72_74')!r}"
        )


@pytest.mark.asyncio
async def test_60min_cycle_falls_back_to_none_when_gamma_market_data_fails(portfolio):
    """Defensive: when ``refresh_gamma_market_data`` throws or returns empty,
    the cycle must pass ``market_states=None`` to evaluators (legacy
    "everything is OPEN" semantics) rather than ``{}`` — the latter would
    flip every slot to UNKNOWN→SKIP and disable all trading on a Gamma
    outage.  Mirrors the run_position_check guard at rebalancer.py:638.
    """
    config = _make_config()
    clob = ClobClient(config)
    executor = Executor(clob, portfolio)
    max_tracker = DailyMaxTracker()

    event = _make_event()
    market_date = event.market_date
    by_date_window = {
        market_date: {
            "New York": Forecast(
                "New York", market_date, 70.0, 60.0, 4.0, "mock",
                datetime.now(timezone.utc),
            ),
        },
    }

    rebalancer = Rebalancer(config, clob, portfolio, executor, max_tracker)

    captured: dict[str, dict] = {}

    def _capture(name):
        def _fn(*args, **kwargs):
            captured[name] = kwargs
            return []
        return _fn

    with (
        patch("src.strategy.rebalancer.discover_weather_markets",
              new_callable=AsyncMock) as mock_discover,
        patch("src.strategy.rebalancer.get_forecasts_for_city_local_window",
              new_callable=AsyncMock) as mock_forecasts,
        patch("src.strategy.rebalancer.fetch_settlement_temp",
              new_callable=AsyncMock) as mock_settle,
        patch("src.strategy.rebalancer.validate_station_config",
              return_value=[]),
        patch("src.strategy.rebalancer.refresh_gamma_market_data",
              new_callable=AsyncMock) as mock_market_data,
        patch("src.strategy.rebalancer.evaluate_no_signals",
              side_effect=_capture("no")),
        patch("src.strategy.rebalancer.evaluate_locked_win_signals",
              side_effect=_capture("locked")),
        patch("src.strategy.rebalancer.evaluate_exit_signals",
              side_effect=_capture("exit")),
        patch("src.strategy.rebalancer.evaluate_trim_signals",
              side_effect=_capture("trim")),
    ):
        mock_discover.return_value = [event]
        mock_forecasts.return_value = by_date_window
        mock_settle.return_value = None
        # Empty result simulates a Gamma outage / batch failure.
        mock_market_data.return_value = {}

        await rebalancer.run()

    for name in ("no", "locked", "exit", "trim"):
        assert name in captured, f"{name} evaluator was not called"
        assert captured[name].get("market_states") is None, (
            f"{name} evaluator: Gamma outage must yield market_states=None "
            f"(legacy passthrough), got {captured[name].get('market_states')!r}"
        )
