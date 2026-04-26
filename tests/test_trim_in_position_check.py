"""FIX-02: run_position_check must produce TRIM signals.

Previously only evaluate_locked_win_signals + evaluate_exit_signals ran in
the 15-min cycle — TRIM was 60-min-only.  Chicago 80-81 (2026-04-15) bled
72% before the hourly TRIM caught up.  These tests verify:

1. A position whose Gamma price has collapsed below the trim_price_stop
   threshold produces a SELL in position_check.
2. The signal carries a "TRIM [price_stop]" reason prefixed with the
   strategy tag (so dashboards and decision_log preserve the trigger).
3. The default trim_price_stop_ratio tightened from 0.25 → 0.20 is
   reflected in config.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.config import AppConfig, CityConfig, SchedulingConfig, StrategyConfig
from src.execution.executor import Executor
from src.markets.models import Side
from src.portfolio.tracker import PortfolioTracker
from src.strategy.rebalancer import Rebalancer
from src.weather.metar import DailyMaxTracker


class _Obs:
    def __init__(self, icao, temp_f):
        self.icao = icao
        self.temp_f = temp_f
        self.observation_time = datetime.now(timezone.utc)
        self.raw_data = ""
        # C-5: SettlementObservation now exposes these — default to "no fallback".
        self.primary_icao = icao
        self.used_fallback = False


def test_default_price_stop_ratio_is_020():
    """FIX-02: config default tightened from 0.25 to 0.20."""
    assert StrategyConfig().trim_price_stop_ratio == 0.20


def _make_config() -> AppConfig:
    return AppConfig(
        strategy=StrategyConfig(
            no_distance_threshold_f=8,
            min_no_ev=0.01,
            enable_locked_wins=True,
            trim_price_stop_ratio=0.20,  # price drops > 20% → TRIM
            min_trim_ev_absolute=999.0,  # disable absolute gate so only price_stop fires
            trim_ev_decay_ratio=0.99,    # disable relative gate
        ),
        scheduling=SchedulingConfig(),
        cities=[
            # tz="" to keep DailyMaxTracker on UTC keying — avoids NYC vs UTC
            # split during the 4-hour overnight UTC window.
            CityConfig("New York", "KLGA", 40.7128, -74.006, tz=""),
        ],
        dry_run=True,
        db_path=Path("/tmp/test_trim_pc.db"),
    )


def _open_position(entry_price: float, token_id: str = "no_1"):
    # Blocker 2 (review): rebuild the date suffix from today's UTC date
    # so the test isn't time-bombed.  position_check parses with
    # `re.search(r'on (\w+ \d+)', label)` so any "on <Month> <day>" works.
    today_suffix = datetime.now(timezone.utc).strftime("on %B %-d")
    return {
        "id": 1, "event_id": "evt_1", "city": "New York",
        "token_id": token_id, "token_type": "NO", "side": "BUY",
        "slot_label": f"70°F to 74°F {today_suffix}",
        "strategy": "B", "entry_price": entry_price,
        "size_usd": 10.0, "shares": 10.0 / entry_price,
        "status": "open", "created_at": datetime.now(timezone.utc).isoformat(),
        "closed_at": None, "buy_reason": "[B] NO: test", "entry_ev": 0.05,
    }


def _mock_rebalancer(positions):
    config = _make_config()
    mock_clob = MagicMock()
    mock_portfolio = MagicMock(spec=PortfolioTracker)
    mock_portfolio.get_all_open_positions = AsyncMock(return_value=positions)
    mock_portfolio.get_city_exposure = AsyncMock(return_value=0.0)
    mock_portfolio.get_total_exposure = AsyncMock(return_value=0.0)
    mock_portfolio.get_daily_pnl = AsyncMock(return_value=None)
    mock_portfolio.record_exit_cooldown = AsyncMock()
    mock_portfolio.load_active_exit_cooldowns = AsyncMock(return_value={})
    # Blocker 2 (review) parity: FIX-11 reads portfolio.store.get_bot_paused
    # at the top of run_position_check; without an AsyncMock the await
    # raises and the kill-switch read errors out (caught but cascades).
    mock_portfolio.store = MagicMock()
    mock_portfolio.store.get_bot_paused = AsyncMock(return_value=False)
    mock_executor = MagicMock(spec=Executor)
    mock_executor.execute_signals = AsyncMock(return_value=[])
    return Rebalancer(
        config=config, clob=mock_clob, portfolio=mock_portfolio,
        executor=mock_executor, max_tracker=DailyMaxTracker(),
    )


@pytest.mark.asyncio
async def test_price_collapse_triggers_trim_in_position_check():
    """Entry 0.645, current 0.180 — drop well past 20% stop → TRIM SELL."""
    pos = _open_position(entry_price=0.645)
    reb = _mock_rebalancer([pos])

    # Prime the Gamma cache so the held slot's price_no reflects the collapse.
    reb._last_gamma_prices = {"no_1": 0.180}

    # Provide a forecast for today so evaluate_trim_signals runs.
    from src.weather.models import Forecast
    today = datetime.now(timezone.utc).date()
    # Need a forecast whose forecast_date matches the event's market_date.
    # The position_check parses "April 24" from slot_label — pick a date we
    # know will parse, matching today's year.
    import re
    m = re.search(r"on (\w+ \d+)", pos["slot_label"])
    label_date = datetime.strptime(f"{m.group(1)} {today.year}", "%B %d %Y").date()
    reb._cached_forecasts_by_date = {
        label_date: {"New York": Forecast(
            city="New York", forecast_date=label_date,
            predicted_high_f=72.0, predicted_low_f=55.0,
            confidence_interval_f=3.0, source="test",
            fetched_at=datetime.now(timezone.utc),
        )},
    }
    # daily_max inside the slot range so position isn't "locked win-like"
    reb._max_tracker.update(_Obs("KLGA", 72.0))

    async def _fetch_obs(city, client):
        return _Obs("KLGA", 72.0) if city == "New York" else None

    with patch("src.strategy.rebalancer.fetch_settlement_temp", side_effect=_fetch_obs):
        signals = await reb.run_position_check()

    sells = [s for s in signals if s.side == Side.SELL]
    assert sells, "Expected a SELL signal from the price-collapse TRIM"
    trim_sells = [s for s in sells if "TRIM" in (s.reason or "")]
    assert trim_sells, (
        f"Expected at least one TRIM SELL; got reasons: {[s.reason for s in sells]}"
    )
    assert "price_stop" in trim_sells[0].reason
    assert trim_sells[0].strategy == "B"


@pytest.mark.asyncio
async def test_no_trim_when_price_holds():
    """Entry 0.645, current 0.60 — only 7% drop, below 20% threshold → no TRIM.

    Entry_ev=None here disables the relative-decay gate (needs a positive
    entry_ev to compute a threshold) so the test isolates price-stop
    behaviour.  A realistic in-profit position with entry_ev >0 would also
    stay out of relative decay as long as current EV >= entry_ev × decay;
    this test is a unit check, not a full-scenario integration test.
    """
    pos = _open_position(entry_price=0.645)
    pos["entry_ev"] = None  # disables relative gate
    reb = _mock_rebalancer([pos])
    reb._last_gamma_prices = {"no_1": 0.60}

    from src.weather.models import Forecast
    today = datetime.now(timezone.utc).date()
    import re
    m = re.search(r"on (\w+ \d+)", pos["slot_label"])
    label_date = datetime.strptime(f"{m.group(1)} {today.year}", "%B %d %Y").date()
    reb._cached_forecasts_by_date = {
        label_date: {"New York": Forecast(
            city="New York", forecast_date=label_date,
            predicted_high_f=72.0, predicted_low_f=55.0,
            confidence_interval_f=3.0, source="test",
            fetched_at=datetime.now(timezone.utc),
        )},
    }
    reb._max_tracker.update(_Obs("KLGA", 72.0))

    async def _fetch_obs(city, client):
        return _Obs("KLGA", 72.0) if city == "New York" else None

    with patch("src.strategy.rebalancer.fetch_settlement_temp", side_effect=_fetch_obs):
        signals = await reb.run_position_check()

    trims = [s for s in signals if "TRIM" in (s.reason or "")]
    assert not trims, f"Did not expect TRIM; got: {[s.reason for s in trims]}"
