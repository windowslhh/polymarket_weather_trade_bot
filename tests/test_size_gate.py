"""Min-order-size gate tests (Phase 5, 2026-04-28; SELL revised 2026-04-29).

Polymarket CLOB minimums are order-type dependent.  The BUY path uses
GTC-resting orders, which require both a 5-share floor AND a $1
notional floor.  Q1's GTC→FAK cutover (2026-04-29) made SELL a taker
order, which the venue gates only on $1 notional — the 5-share floor
is GTC-specific.  The BUY-side gate inside ``compute_size`` therefore
still enforces both minimums; the SELL-side gate inside
``Executor._execute_one`` now enforces only $1 so legitimate
stop-loss exits of sub-5-share positions can attempt.
"""
from __future__ import annotations

import tempfile
from dataclasses import replace
from datetime import date
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from src.config import StrategyConfig
from src.execution.executor import Executor
from src.markets.clob_client import OrderResult
from src.markets.models import Side, TempSlot, TokenType, TradeSignal, WeatherMarketEvent
from src.portfolio.store import Store
from src.portfolio.tracker import PortfolioTracker
from src.strategy.sizing import compute_size


def _slot(price_no: float = 0.50) -> TempSlot:
    return TempSlot(
        token_id_yes="yes_x", token_id_no="no_x",
        outcome_label="80°F to 84°F",
        temp_lower_f=80.0, temp_upper_f=84.0,
        price_no=price_no, price_yes=1.0 - price_no,
    )


def _event() -> WeatherMarketEvent:
    return WeatherMarketEvent(
        event_id="ev_size", condition_id="c", city="Chicago",
        market_date=date(2026, 4, 28), slots=[],
    )


def _signal(price_no: float = 0.50, locked_win: bool = False) -> TradeSignal:
    s = _slot(price_no)
    return TradeSignal(
        token_type=TokenType.NO, side=Side.BUY, slot=s, event=_event(),
        expected_value=0.05, estimated_win_prob=0.70,
        is_locked_win=locked_win,
    )


# ── BUY path: compute_size ────────────────────────────────────────────

def test_buy_size_below_min_shares_skips():
    """High price + tiny per-slot cap → rounded size implies < 5 shares.

    Price 0.95, slot cap $4 ⇒ Kelly-scaled output ~ a few cents → < 5 shares.
    """
    cfg = replace(StrategyConfig(),
                  max_position_per_slot_usd=4.0,
                  max_locked_win_per_slot_usd=4.0,
                  kelly_fraction=0.5,
                  min_order_size_shares=5.0,
                  min_order_amount_usd=1.0)
    sig = _signal(price_no=0.95)
    size = compute_size(
        sig, city_exposure_usd=0.0, total_exposure_usd=0.0, config=cfg,
    )
    # 0.95 × 5 shares = $4.75; if size_usd < that, gate must fire.
    if size > 0:
        assert size / 0.95 >= 5.0, (
            f"size={size} → {size/0.95:.2f} shares < min 5"
        )
    # Confirm gate actually engaged at least once for some realistic
    # signal; with this config and 0.95 price the gate WILL fire.
    sig_low_kelly = _signal(price_no=0.95)
    sig_low_kelly = replace(sig_low_kelly, estimated_win_prob=0.96)  # tiny edge
    size_lk = compute_size(
        sig_low_kelly, city_exposure_usd=0.0, total_exposure_usd=0.0, config=cfg,
    )
    # tiny edge × $4 cap × 0.5 frac → likely sub-min, expect 0
    assert size_lk == 0.0


def test_buy_amount_below_min_usd_skips():
    """Force the rounded notional under $1 → gate fires."""
    cfg = replace(StrategyConfig(),
                  max_position_per_slot_usd=0.5,  # hard cap below $1
                  kelly_fraction=0.5,
                  min_order_amount_usd=1.0)
    sig = _signal(price_no=0.40)
    size = compute_size(
        sig, city_exposure_usd=0.0, total_exposure_usd=0.0, config=cfg,
    )
    # size_usd capped at $0.50 < $1.0 floor → 0
    assert size == 0.0


def test_buy_size_passes_when_above_both_minimums():
    cfg = replace(StrategyConfig(),
                  max_position_per_slot_usd=10.0,
                  kelly_fraction=0.5,
                  min_order_size_shares=5.0,
                  min_order_amount_usd=1.0)
    sig = _signal(price_no=0.40)
    size = compute_size(
        sig, city_exposure_usd=0.0, total_exposure_usd=0.0, config=cfg,
    )
    assert size > 0
    assert size / 0.40 >= 5.0
    assert size >= 1.0


# ── SELL path: Executor._execute_one ─────────────────────────────────

class _FakeClob:
    """Minimal clob double — exposes ``_config`` so the executor can read
    ``strategy.min_order_amount_usd`` off it via getattr()."""

    def __init__(self, cfg: StrategyConfig) -> None:
        self._config = type("AppCfg", (), {"strategy": cfg, "dry_run": False, "paper": False})()
        self.place_limit_order = AsyncMock(
            return_value=OrderResult(order_id="should_not_be_called", success=True),
        )
        self.cancel_order = AsyncMock(return_value=True)
        self.get_fill_summary = AsyncMock(return_value=None)


async def _mk_store():
    tmp = Path(tempfile.mkdtemp()) / "bot.db"
    store = Store(tmp)
    await store.initialize()
    return store


def _sell_signal_with_token(token_id: str, price_no: float, strat: str = "D") -> TradeSignal:
    slot = TempSlot(
        token_id_yes="yes_z", token_id_no=token_id,
        outcome_label="80°F to 84°F",
        temp_lower_f=80.0, temp_upper_f=84.0,
        price_no=price_no, price_yes=1.0 - price_no,
    )
    event = WeatherMarketEvent(
        event_id="ev_sell", condition_id="cs", city="Chicago",
        market_date=date(2026, 4, 28), slots=[slot],
    )
    return TradeSignal(
        token_type=TokenType.NO, side=Side.SELL, slot=slot, event=event,
        expected_value=-0.10, estimated_win_prob=0.30,
        suggested_size_usd=0.0, strategy=strat, reason="test_sell",
    )


@pytest.mark.asyncio
async def test_sell_subfive_shares_passes_when_amount_above_one():
    """Q1 FAK regression guard: 3.14 shares × $0.755 = $2.37 notional.

    Before 2026-04-29 the SELL gate also enforced a 5-share floor — but
    that floor is GTC-only on Polymarket and Q1 switched SELL to FAK,
    where the venue accepts anything ≥ $1 notional.  Sub-5-share
    legacy positions must be allowed to attempt their stop-loss exit
    so they don't wedge the alert channel forever.
    """
    store = await _mk_store()
    tracker = PortfolioTracker(store)
    cfg = replace(StrategyConfig(),
                  min_order_size_shares=5.0, min_order_amount_usd=1.0)
    clob = _FakeClob(cfg)
    clob.place_limit_order = AsyncMock(
        return_value=OrderResult(order_id="ok_id", success=True),
    )
    executor = Executor(clob, tracker)

    await store.insert_position(
        event_id="ev_sell", token_id="tok_sub", token_type="NO", city="Chicago",
        slot_label="80°F to 84°F", side="BUY", entry_price=0.45, size_usd=1.41,
        shares=3.14, strategy="D", buy_reason="seed",
    )

    sig = _sell_signal_with_token("tok_sub", price_no=0.755)
    await executor.execute_signals([sig])

    # CLOB now hit — the share gate no longer blocks.
    clob.place_limit_order.assert_called_once()
    async with store.db.execute(
        "SELECT action, reason FROM decision_log WHERE action = 'SKIP'"
    ) as cur:
        rows = [dict(r) for r in await cur.fetchall()]
    assert rows == []
    await store.close()


@pytest.mark.asyncio
async def test_sell_below_min_amount_skips_clob_call():
    """6 shares × $0.10 = $0.60 notional → AMOUNT_BELOW_MIN_USD."""
    store = await _mk_store()
    tracker = PortfolioTracker(store)
    cfg = replace(StrategyConfig(),
                  min_order_size_shares=5.0, min_order_amount_usd=1.0)
    clob = _FakeClob(cfg)
    executor = Executor(clob, tracker)

    await store.insert_position(
        event_id="ev_sell", token_id="tok_dust", token_type="NO", city="Chicago",
        slot_label="80°F to 84°F", side="BUY", entry_price=0.50, size_usd=3.0,
        shares=6.0, strategy="D", buy_reason="seed",
    )

    sig = _sell_signal_with_token("tok_dust", price_no=0.10)
    await executor.execute_signals([sig])

    clob.place_limit_order.assert_not_called()
    async with store.db.execute(
        "SELECT reason FROM decision_log WHERE action = 'SKIP'"
    ) as cur:
        rows = [dict(r) for r in await cur.fetchall()]
    assert len(rows) == 1
    assert "AMOUNT_BELOW_MIN_USD" in rows[0]["reason"]
    await store.close()


@pytest.mark.asyncio
async def test_sell_above_thresholds_proceeds_to_clob():
    """100 shares × $0.40 = $40 → comfortable above both floors."""
    store = await _mk_store()
    tracker = PortfolioTracker(store)
    cfg = replace(StrategyConfig(),
                  min_order_size_shares=5.0, min_order_amount_usd=1.0)
    clob = _FakeClob(cfg)
    clob.place_limit_order = AsyncMock(
        return_value=OrderResult(order_id="ok_id", success=True),
    )
    executor = Executor(clob, tracker)

    await store.insert_position(
        event_id="ev_sell", token_id="tok_real", token_type="NO", city="Chicago",
        slot_label="80°F to 84°F", side="BUY", entry_price=0.45, size_usd=45.0,
        shares=100.0, strategy="D", buy_reason="seed",
    )
    sig = _sell_signal_with_token("tok_real", price_no=0.40)
    await executor.execute_signals([sig])

    clob.place_limit_order.assert_called_once()
    await store.close()
