"""Tests for cycle-fix-2: position_check entry scan.

Covers the 7 paths documented in the cycle-frequency-fix spec:

1. Flag enabled + cached events + clean BB/KS + cache hits → BUY signal fires
2. Flag disabled → entry scan does NOT run (no signals, no Gamma fetch)
3. Circuit breaker engaged → entry scan skipped (TRIM/EXIT still run)
4. Kill switch engaged → entry scan skipped
5. Exit cooldown hit → that signal is dropped, others may proceed
6. Forecast cache miss for (event, market_date) → that event skipped
7. Held-position dedup: a 60-min cycle just opened position on
   (event, token); 15-min entry scan must not re-buy the same token

Tests stub the Gamma helper at ``src.strategy.rebalancer.refresh_gamma_prices_only``
so no real HTTP fires.  Forecasts are pre-seeded into
``_cached_forecasts_by_date``; events are placed in ``_last_events``
to mimic what the 60-min cycle would have cached.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.config import AppConfig, CityConfig, SchedulingConfig, StrategyConfig
from src.execution.executor import Executor
from src.markets.models import Side, TempSlot, TokenType, WeatherMarketEvent
from src.portfolio.tracker import PortfolioTracker
from src.strategy.rebalancer import Rebalancer
from src.weather.metar import DailyMaxTracker
from src.weather.models import Forecast, Observation


# ── helpers ──────────────────────────────────────────────────────────


def _make_config(**overrides) -> AppConfig:
    return AppConfig(
        strategy=StrategyConfig(
            no_distance_threshold_f=8,
            min_no_ev=0.01,
            max_no_price=0.95,
            enable_locked_wins=True,
            **overrides,
        ),
        scheduling=SchedulingConfig(),
        cities=[
            CityConfig("New York", "KLGA", 40.7128, -74.006,
                       tz="America/New_York"),
            CityConfig("Dallas", "KDAL", 32.8471, -96.8518,
                       tz="America/Chicago"),
        ],
        dry_run=True,
        db_path=Path("/tmp/test_entry_scan.db"),
    )


def _build_event(
    *,
    city: str = "New York",
    event_id: str = "evt_entry_1",
    market_date: date | None = None,
    no_price: float = 0.50,
) -> WeatherMarketEvent:
    """An event with two slots — one with a price near our target, one
    high-priced.  Distance 76-80 with forecast=85 means dist=5°F < 8
    threshold OK only if calibrator overrides — but min_no_ev=0.01 is
    permissive enough that win_prob need only beat ~0.51 to fire."""
    market_date = market_date or date.today()
    return WeatherMarketEvent(
        event_id=event_id,
        condition_id=f"cond_{event_id}",
        city=city,
        market_date=market_date,
        slots=[
            TempSlot(
                token_id_yes=f"{event_id}_yes_lo",
                token_id_no=f"{event_id}_no_lo",
                outcome_label=f"between 64-65°F on April 27",
                temp_lower_f=64.0, temp_upper_f=65.0,
                price_no=no_price,  # the candidate slot
            ),
            TempSlot(
                token_id_yes=f"{event_id}_yes_hi",
                token_id_no=f"{event_id}_no_hi",
                outcome_label=f"between 70-71°F on April 27",
                temp_lower_f=70.0, temp_upper_f=71.0,
                price_no=0.99,  # near-1 NO price → never enter
            ),
        ],
    )


def _seed_forecast(reb: Rebalancer, event: WeatherMarketEvent,
                   predicted_high_f: float = 85.0) -> None:
    """Stuff a Forecast into ``_cached_forecasts_by_date`` for the
    event's (market_date, city) so the entry scan finds it."""
    fc = Forecast(
        city=event.city,
        forecast_date=event.market_date,
        predicted_high_f=predicted_high_f,
        predicted_low_f=60.0,
        confidence_interval_f=4.0,
        source="test",
        fetched_at=datetime.now(timezone.utc),
    )
    reb._cached_forecasts_by_date.setdefault(event.market_date, {})[event.city] = fc


def _mock_rebalancer(config=None, positions=None):
    """Same shape as test_position_check._mock_rebalancer but tailored
    for entry-scan tests.  Defaults: no held positions, entry scan
    flag ON, kill switch off, daily P&L within budget."""
    config = config or _make_config()

    mock_clob = MagicMock()
    mock_portfolio = MagicMock(spec=PortfolioTracker)
    mock_executor = MagicMock(spec=Executor)
    mock_executor.execute_signals = AsyncMock(return_value=[])

    mock_portfolio.get_all_open_positions = AsyncMock(return_value=positions or [])
    mock_portfolio.get_open_positions_for_event = AsyncMock(return_value=[])
    mock_portfolio.get_city_exposure = AsyncMock(return_value=0.0)
    mock_portfolio.get_total_exposure = AsyncMock(return_value=0.0)
    mock_portfolio.get_daily_pnl = AsyncMock(return_value=None)
    mock_portfolio.record_exit_cooldown = AsyncMock()
    mock_portfolio.load_active_exit_cooldowns = AsyncMock(return_value={})
    mock_portfolio.store = MagicMock()
    mock_portfolio.store.get_bot_paused = AsyncMock(return_value=False)

    rebalancer = Rebalancer(
        config=config,
        clob=mock_clob,
        portfolio=mock_portfolio,
        executor=mock_executor,
        max_tracker=DailyMaxTracker(),
    )
    rebalancer.refresh_forecasts = AsyncMock(return_value=None)
    rebalancer._fetch_observations = AsyncMock(return_value=({}, {}))
    return rebalancer


# ── test cases (numbered to match the 7 spec paths) ──────────────────


@pytest.mark.asyncio
async def test_path1_enabled_with_cached_events_fires_buy(monkeypatch):
    """Path 1: flag on + event in cache + clean breaker → entry scan
    runs evaluate_no_signals and emits at least one BUY signal."""
    reb = _mock_rebalancer()
    event = _build_event(no_price=0.55)
    _seed_forecast(reb, event)
    reb._last_events = [event]

    # Stub the Gamma fetch — return prices that pass max_no_price=0.95.
    fetched = {
        f"{event.event_id}_no_lo": 0.55,
        f"{event.event_id}_no_hi": 0.99,
    }
    with patch(
        "src.strategy.rebalancer.refresh_gamma_prices_only",
        new=AsyncMock(return_value=fetched),
    ):
        signals = await reb.run_position_check()

    buys = [s for s in signals if s.side == Side.BUY]
    assert buys, "entry scan should emit at least one BUY"
    assert any(s.token_id == f"{event.event_id}_no_lo" for s in buys), (
        "the lo-price slot should win, not the 0.99 hi-price slot"
    )
    # Reason carries the entry-scan tag so dashboards / decision_log
    # operators can tell where the signal came from.
    assert any("entry-scan" in (s.reason or "") for s in buys)
    # cycle-fix-10: size must be positive — a zero-size BUY would slip
    # through the executor and never actually open a position.  This
    # was implicit in path1 originally; pinning it here so a future
    # regression in compute_size or the city/total exposure
    # bookkeeping is caught at the boundary.
    assert all(s.suggested_size_usd > 0 for s in buys), (
        f"BUY signals should have positive suggested_size_usd; "
        f"observed sizes: {[s.suggested_size_usd for s in buys]}"
    )


@pytest.mark.asyncio
async def test_path2_flag_disabled_skips_entry_scan(monkeypatch):
    """Path 2: enable_position_check_entry_scan=False → no Gamma fetch,
    no entry-scan signals.  Held-position phase still runs (here it's
    a no-op since no positions held)."""
    reb = _mock_rebalancer(config=_make_config(
        enable_position_check_entry_scan=False,
    ))
    event = _build_event()
    _seed_forecast(reb, event)
    reb._last_events = [event]

    gamma_mock = AsyncMock(return_value={})
    with patch("src.strategy.rebalancer.refresh_gamma_prices_only", new=gamma_mock):
        signals = await reb.run_position_check()

    assert signals == []
    # Entry-scan did NOT call Gamma.  (The held-token refresh would
    # also call Gamma, but with no held positions held_token_ids is
    # empty → no call there either.)
    gamma_mock.assert_not_called()


@pytest.mark.asyncio
async def test_path3_circuit_breaker_skips_entry_scan(monkeypatch):
    """Path 3: daily P&L below loss limit → _cb_block_buys=True →
    entry scan skipped (closing trades still run)."""
    reb = _mock_rebalancer()
    # Daily loss exceeds the $50 default limit (we use the config
    # default daily_loss_limit_usd; build_event uses $50 implicitly).
    reb._portfolio.get_daily_pnl = AsyncMock(
        return_value=-(reb._config.strategy.daily_loss_limit_usd + 1.0),
    )
    event = _build_event(no_price=0.50)
    _seed_forecast(reb, event)
    reb._last_events = [event]

    gamma_mock = AsyncMock(return_value={})
    with patch("src.strategy.rebalancer.refresh_gamma_prices_only", new=gamma_mock):
        signals = await reb.run_position_check()

    assert all(s.side != Side.BUY for s in signals), (
        "circuit breaker must block all BUY signals from the entry scan"
    )
    gamma_mock.assert_not_called()


@pytest.mark.asyncio
async def test_path4_kill_switch_skips_entry_scan(monkeypatch):
    """Path 4: bot paused via kill switch → entry scan skipped."""
    reb = _mock_rebalancer()
    reb._portfolio.store.get_bot_paused = AsyncMock(return_value=True)
    event = _build_event(no_price=0.50)
    _seed_forecast(reb, event)
    reb._last_events = [event]

    gamma_mock = AsyncMock(return_value={})
    with patch("src.strategy.rebalancer.refresh_gamma_prices_only", new=gamma_mock):
        signals = await reb.run_position_check()

    assert all(s.side != Side.BUY for s in signals)
    gamma_mock.assert_not_called()


@pytest.mark.asyncio
async def test_path5_exit_cooldown_drops_signal(monkeypatch):
    """Path 5: token in _recent_exits within cooldown window → BUY for
    that token is suppressed.  A separate token in the same event
    should still be eligible."""
    reb = _mock_rebalancer()
    event = _build_event(no_price=0.55)
    _seed_forecast(reb, event)
    reb._last_events = [event]
    # Mark the lo-price token as recently exited.
    now = datetime.now(timezone.utc)
    reb._recent_exits[f"{event.event_id}_no_lo"] = now

    fetched = {
        f"{event.event_id}_no_lo": 0.55,
        f"{event.event_id}_no_hi": 0.99,
    }
    with patch(
        "src.strategy.rebalancer.refresh_gamma_prices_only",
        new=AsyncMock(return_value=fetched),
    ):
        signals = await reb.run_position_check()

    # Cooled-down token must NOT show up as a BUY.
    assert not any(
        s.token_id == f"{event.event_id}_no_lo" and s.side == Side.BUY
        for s in signals
    ), "token in cooldown must not be re-bought"


@pytest.mark.asyncio
async def test_path6_forecast_cache_miss_skips_event(monkeypatch):
    """Path 6: no forecast cached for (event.market_date, event.city)
    → the event is silently skipped (no signal, no exception).  H-9
    invariant: entry scan must NOT fall back to today's by-name forecast
    for a D+1 event."""
    reb = _mock_rebalancer()
    event = _build_event(
        market_date=date.today() + timedelta(days=1),
        no_price=0.50,
    )
    # Intentionally NOT seeding the by-date cache for this event.
    reb._last_events = [event]

    fetched = {
        f"{event.event_id}_no_lo": 0.50,
        f"{event.event_id}_no_hi": 0.99,
    }
    with patch(
        "src.strategy.rebalancer.refresh_gamma_prices_only",
        new=AsyncMock(return_value=fetched),
    ):
        signals = await reb.run_position_check()

    # No BUY because the event was skipped.  Held-position phase has
    # nothing held so emits nothing either.
    assert all(s.side != Side.BUY for s in signals), (
        "events without a by-date forecast must be skipped (H-9)"
    )


def _make_locked_win_signal(event: WeatherMarketEvent) -> "TradeSignal":
    """Build a TradeSignal that mimics what
    ``evaluate_locked_win_signals`` would emit on the lo-price slot.
    Reused by the two cycle-fix-7 tests below."""
    from src.markets.models import TradeSignal as _TS
    return _TS(
        token_type=TokenType.NO,
        side=Side.BUY,
        slot=event.slots[0],
        event=event,
        expected_value=0.20,
        estimated_win_prob=0.97,
        is_locked_win=True,
        reason="LOCKED WIN: below-slot",
    )


@pytest.mark.asyncio
async def test_path8_locked_win_in_entry_scan(monkeypatch):
    """cycle-fix-7: ``evaluate_locked_win_signals`` runs alongside
    ``evaluate_no_signals`` inside the entry scan with the same
    forecast / daily_max / dedup / cap.  Locked wins must reach the
    merged signals list with positive size and the entry-scan tag.
    """
    reb = _mock_rebalancer()
    event = _build_event(no_price=0.55)
    _seed_forecast(reb, event)
    reb._last_events = [event]

    locked_signal = _make_locked_win_signal(event)

    fetched = {
        f"{event.event_id}_no_lo": 0.55,
        f"{event.event_id}_no_hi": 0.99,
    }
    with patch(
        "src.strategy.rebalancer.refresh_gamma_prices_only",
        new=AsyncMock(return_value=fetched),
    ), patch(
        "src.strategy.rebalancer.evaluate_locked_win_signals",
        return_value=[locked_signal],
    ):
        signals = await reb.run_position_check()

    locked_buys = [
        s for s in signals
        if s.side == Side.BUY and s.is_locked_win
    ]
    assert locked_buys, "locked-win signal should reach the merged list"
    assert all(s.suggested_size_usd > 0 for s in locked_buys)
    assert all("entry-scan" in (s.reason or "") for s in locked_buys), (
        "locked wins from the entry scan must carry the (entry-scan) tag "
        "so dashboards / decision_log can attribute them correctly"
    )


@pytest.mark.asyncio
async def test_path9_locked_win_respects_cooldown(monkeypatch):
    """cycle-fix-7: a token in ``_recent_exits`` within the cooldown
    window must NOT receive a locked-win BUY from the entry scan.
    Same dedup the forecast-NO path uses (path 5)."""
    reb = _mock_rebalancer()
    event = _build_event(no_price=0.55)
    _seed_forecast(reb, event)
    reb._last_events = [event]

    locked_token = event.slots[0].token_id_no
    reb._recent_exits[locked_token] = datetime.now(timezone.utc)

    locked_signal = _make_locked_win_signal(event)

    fetched = {
        f"{event.event_id}_no_lo": 0.55,
        f"{event.event_id}_no_hi": 0.99,
    }
    with patch(
        "src.strategy.rebalancer.refresh_gamma_prices_only",
        new=AsyncMock(return_value=fetched),
    ), patch(
        "src.strategy.rebalancer.evaluate_locked_win_signals",
        return_value=[locked_signal],
    ):
        signals = await reb.run_position_check()

    assert not any(
        s.is_locked_win and s.token_id == locked_token and s.side == Side.BUY
        for s in signals
    ), "locked-win token in cooldown must not be re-bought"


@pytest.mark.asyncio
async def test_total_exposure_queried_once_per_strategy(monkeypatch):
    """cycle-fix-8: ``get_total_exposure(strategy=)`` is independent of
    event_id, so the entry scan must call it ONCE per active variant
    before the event loop — not N_events × N_variants times inside.

    With 5 events and N active variants, pre-fix shape called the
    aggregate 5 × N times; cached shape calls it N times.  Pin the
    call count to N (number of active variants from
    ``get_strategy_variants()``).
    """
    from src.config import get_strategy_variants

    reb = _mock_rebalancer()
    events = [
        _build_event(event_id=f"evt_{i}", no_price=0.55)
        for i in range(5)
    ]
    for ev in events:
        _seed_forecast(reb, ev)
    reb._last_events = events

    fetched: dict[str, float] = {}
    for ev in events:
        fetched[f"{ev.event_id}_no_lo"] = 0.55
        fetched[f"{ev.event_id}_no_hi"] = 0.99

    with patch(
        "src.strategy.rebalancer.refresh_gamma_prices_only",
        new=AsyncMock(return_value=fetched),
    ):
        await reb.run_position_check()

    n_variants = len(get_strategy_variants())
    assert reb._portfolio.get_total_exposure.await_count == n_variants, (
        f"get_total_exposure should be called once per active variant "
        f"(={n_variants}), not N_events × N_variants times — cycle-fix-8.  "
        f"Observed: {reb._portfolio.get_total_exposure.await_count}"
    )


def test_config_yaml_exposes_entry_scan_flag(tmp_path):
    """cycle-fix-6: ``config.yaml`` writes ``enable_position_check_entry_scan``
    and ``load_config()`` carries it through to the StrategyConfig
    instance.  Pins the YAML key name + boolean coercion so renaming
    the flag in code without updating the YAML is caught here."""
    from src.config import load_config

    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        "strategy:\n"
        "  enable_position_check_entry_scan: false\n"
        "scheduling:\n"
        "  discovery_interval_minutes: 15\n"
        "  rebalance_interval_minutes: 60\n"
        "  pnl_snapshot_interval_hours: 24\n"
        "cities: []\n"
    )
    cfg = load_config(config_path=cfg_path, env_path=tmp_path / ".env")
    assert cfg.strategy.enable_position_check_entry_scan is False

    cfg_path.write_text(
        "strategy:\n"
        "  enable_position_check_entry_scan: true\n"
        "scheduling:\n"
        "  discovery_interval_minutes: 15\n"
        "  rebalance_interval_minutes: 60\n"
        "  pnl_snapshot_interval_hours: 24\n"
        "cities: []\n"
    )
    cfg = load_config(config_path=cfg_path, env_path=tmp_path / ".env")
    assert cfg.strategy.enable_position_check_entry_scan is True


@pytest.mark.asyncio
async def test_entry_scan_exception_does_not_block_exit_trim(monkeypatch):
    """cycle-fix-5: a bug inside _run_entry_scan must NOT swallow the
    held-position EXIT/TRIM signals already in the merged list.  The
    isolation try/except in run_position_check is the safety net.

    Closing trades are always allowed (matches the daily-loss circuit
    breaker invariant); a regression here would mean a flaky entry-scan
    bug could prevent a needed EXIT from reaching the executor.
    """
    reb = _mock_rebalancer()
    event = _build_event(no_price=0.55)
    _seed_forecast(reb, event)
    reb._last_events = [event]

    # Force entry scan to crash hard.
    boom = AsyncMock(side_effect=RuntimeError("entry-scan synthetic failure"))
    with patch.object(reb, "_run_entry_scan", new=boom):
        signals = await reb.run_position_check()

    # Function returned cleanly (no propagated exception) — the empty
    # list is fine; what matters is we got here at all.  The held-
    # position phase produced no signals because no positions held;
    # the test's job is to assert the run_position_check itself
    # doesn't bubble the entry-scan failure.
    assert isinstance(signals, list)
    boom.assert_called_once()


@pytest.mark.asyncio
async def test_path7_held_token_dedup(monkeypatch):
    """Path 7: held position on (event, token, strategy) → entry scan
    must not re-buy the same token.  Mirrors what happens when a
    60-min cycle just opened a position 5 min before the 15-min
    position_check fires."""
    reb = _mock_rebalancer()
    event = _build_event(no_price=0.55)
    _seed_forecast(reb, event)
    reb._last_events = [event]
    held_token = f"{event.event_id}_no_lo"
    held_position = {
        "id": 1,
        "event_id": event.event_id,
        "token_id": held_token,
        "token_type": "NO",
        "city": event.city,
        "side": "BUY",
        "slot_label": "between 64-65°F on April 27",
        "strategy": "B",
        "entry_price": 0.55,
        "size_usd": 5.0,
        "shares": 9.09,
        "status": "open",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "closed_at": None,
        "buy_reason": "[B] NO: dist=20°F",
    }
    # The event-scoped lookup (used inside entry scan) returns the
    # existing position so its token is in held_token_ids.
    reb._portfolio.get_open_positions_for_event = AsyncMock(
        return_value=[held_position],
    )
    # The general "all open positions" call also surfaces it so the
    # held-position phase activates and may try to refresh.
    reb._portfolio.get_all_open_positions = AsyncMock(
        return_value=[held_position],
    )

    fetched = {
        held_token: 0.55,
        f"{event.event_id}_no_hi": 0.99,
    }
    with patch(
        "src.strategy.rebalancer.refresh_gamma_prices_only",
        new=AsyncMock(return_value=fetched),
    ):
        signals = await reb.run_position_check()

    buys_on_held = [
        s for s in signals
        if s.side == Side.BUY and s.token_id == held_token
    ]
    assert not buys_on_held, (
        "entry scan must dedup against already-held token "
        "(60-min just bought, 15-min must not double up)"
    )
