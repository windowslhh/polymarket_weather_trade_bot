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


# ──────────────────────────────────────────────────────────────────────
# Bug C Phase 1 (2026-04-29): SELL clamp by on-chain ERC1155 balance.
# DB shares can be > on-chain (the BUY taker fee was deducted in shares
# from the token side; legacy rows wrote ``size_usd / limit_price``).
# Without the clamp, the matcher 400's "not enough balance".
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_sell_clamps_to_on_chain_when_db_overshoots():
    """DB says 3.0838 shares, chain has 3.046120 — order must use the
    chain figure so Polymarket doesn't reject with 'not enough balance'.

    Replays the 2026-04-29 Denver id=6 incident.
    """
    store = await _mk_store()
    tracker = PortfolioTracker(store)
    cfg = replace(StrategyConfig(),
                  min_order_size_shares=5.0, min_order_amount_usd=1.0)
    clob = _FakeClob(cfg)
    # Chain returns raw 6-decimal: 3.046120 shares == 3,046,120 raw.
    clob.get_conditional_balance = AsyncMock(return_value=3_046_120)
    clob.place_limit_order = AsyncMock(
        return_value=OrderResult(order_id="ok_id", success=True),
    )
    executor = Executor(clob, tracker)

    await store.insert_position(
        event_id="ev_sell", token_id="tok_drift", token_type="NO", city="Chicago",
        slot_label="80°F to 84°F", side="BUY", entry_price=0.775, size_usd=2.39,
        shares=3.0838709677, strategy="D", buy_reason="seed",
    )
    # Price 0.50 keeps 3.046 × $0.50 = $1.52 above the $1 min-amount gate,
    # so the clamp behaviour is exercised end-to-end through to CLOB.
    sig = _sell_signal_with_token("tok_drift", price_no=0.50)
    await executor.execute_signals([sig])

    # Verify the order used the on-chain count, not DB.
    clob.place_limit_order.assert_called_once()
    call_kwargs = clob.place_limit_order.call_args.kwargs
    assert abs(call_kwargs["size"] - 3.046120) < 1e-6, (
        f"SELL size should be clamped to chain balance, got {call_kwargs['size']}"
    )
    await store.close()


@pytest.mark.asyncio
async def test_sell_skips_when_on_chain_zero():
    """If chain has 0 shares (already redeemed / never settled mid-cycle),
    the SELL must skip without hitting CLOB — even if DB still shows
    open shares.  Prevents 400's after a redeemer race."""
    store = await _mk_store()
    tracker = PortfolioTracker(store)
    cfg = replace(StrategyConfig(),
                  min_order_size_shares=5.0, min_order_amount_usd=1.0)
    clob = _FakeClob(cfg)
    clob.get_conditional_balance = AsyncMock(return_value=0)
    executor = Executor(clob, tracker)

    await store.insert_position(
        event_id="ev_sell", token_id="tok_gone", token_type="NO", city="Chicago",
        slot_label="80°F to 84°F", side="BUY", entry_price=0.50, size_usd=5.0,
        shares=10.0, strategy="D", buy_reason="seed",
    )
    sig = _sell_signal_with_token("tok_gone", price_no=0.30)
    await executor.execute_signals([sig])

    clob.place_limit_order.assert_not_called()
    await store.close()


@pytest.mark.asyncio
async def test_sell_falls_back_to_db_when_chain_query_raises():
    """RPC failure must NOT block legitimate SELLs — fall back to DB
    shares with a warning.  The pre-fix behavior is the safe fallback."""
    store = await _mk_store()
    tracker = PortfolioTracker(store)
    cfg = replace(StrategyConfig(),
                  min_order_size_shares=5.0, min_order_amount_usd=1.0)
    clob = _FakeClob(cfg)
    clob.get_conditional_balance = AsyncMock(
        side_effect=RuntimeError("RPC timeout"),
    )
    clob.place_limit_order = AsyncMock(
        return_value=OrderResult(order_id="ok_id", success=True),
    )
    executor = Executor(clob, tracker)

    await store.insert_position(
        event_id="ev_sell", token_id="tok_fallback", token_type="NO", city="Chicago",
        slot_label="80°F to 84°F", side="BUY", entry_price=0.50, size_usd=5.0,
        shares=10.0, strategy="D", buy_reason="seed",
    )
    sig = _sell_signal_with_token("tok_fallback", price_no=0.40)
    await executor.execute_signals([sig])

    clob.place_limit_order.assert_called_once()
    call_kwargs = clob.place_limit_order.call_args.kwargs
    assert call_kwargs["size"] == 10.0, (
        "On RPC failure SELL must fall back to DB shares, "
        f"got {call_kwargs['size']}"
    )
    await store.close()


@pytest.mark.asyncio
async def test_sell_paper_mode_skips_chain_clamp_uses_db_shares():
    """Bug C Phase 1 paper-mode short-circuit (2026-04-29): paper has no
    real chain position so ``get_conditional_balance`` returns 0 (the
    wrapper at clob_client.py:300-335 swallows errors and yields 0).
    Without the short-circuit ``min(db, 0) == 0`` would silently kill
    every paper SELL — masking strategy regressions in CI/dryrun.
    """
    store = await _mk_store()
    tracker = PortfolioTracker(store)
    cfg = replace(StrategyConfig(),
                  min_order_size_shares=5.0, min_order_amount_usd=1.0)
    clob = _FakeClob(cfg)
    clob._config.paper = True
    # Even if paper somehow returns 0 from chain, we must trust DB.
    clob.get_conditional_balance = AsyncMock(return_value=0)
    clob.place_limit_order = AsyncMock(
        return_value=OrderResult(order_id="paper_id", success=True),
    )
    executor = Executor(clob, tracker)

    await store.insert_position(
        event_id="ev_sell", token_id="tok_paper", token_type="NO", city="Chicago",
        slot_label="80°F to 84°F", side="BUY", entry_price=0.45, size_usd=10.0,
        shares=20.0, strategy="D", buy_reason="seed",
    )
    sig = _sell_signal_with_token("tok_paper", price_no=0.40)
    await executor.execute_signals([sig])

    clob.place_limit_order.assert_called_once()
    call_kwargs = clob.place_limit_order.call_args.kwargs
    assert call_kwargs["size"] == 20.0, (
        f"Paper SELL must use db_shares=20.0, got {call_kwargs['size']}"
    )
    await store.close()


@pytest.mark.asyncio
async def test_sell_paper_mode_does_not_call_get_conditional_balance():
    """Paper short-circuit must avoid the chain RPC entirely — no
    keychain creds / no Polygon RPC needed for paper runs."""
    store = await _mk_store()
    tracker = PortfolioTracker(store)
    cfg = replace(StrategyConfig(),
                  min_order_size_shares=5.0, min_order_amount_usd=1.0)
    clob = _FakeClob(cfg)
    clob._config.paper = True
    clob.get_conditional_balance = AsyncMock(return_value=0)
    clob.place_limit_order = AsyncMock(
        return_value=OrderResult(order_id="paper_id", success=True),
    )
    executor = Executor(clob, tracker)

    await store.insert_position(
        event_id="ev_sell", token_id="tok_p2", token_type="NO", city="Chicago",
        slot_label="80°F to 84°F", side="BUY", entry_price=0.45, size_usd=10.0,
        shares=20.0, strategy="D", buy_reason="seed",
    )
    sig = _sell_signal_with_token("tok_p2", price_no=0.40)
    await executor.execute_signals([sig])

    clob.get_conditional_balance.assert_not_called()
    await store.close()


@pytest.mark.asyncio
async def test_sell_uses_db_when_chain_balance_higher():
    """If chain > DB (somehow — shouldn't happen post-fix, but defensive),
    don't sell more than DB has tracked.  Otherwise we'd sell tokens we
    don't know about → P&L mis-attribution."""
    store = await _mk_store()
    tracker = PortfolioTracker(store)
    cfg = replace(StrategyConfig(),
                  min_order_size_shares=5.0, min_order_amount_usd=1.0)
    clob = _FakeClob(cfg)
    # Chain: 100 shares.  DB: 50.  min(50, 100) = 50.
    clob.get_conditional_balance = AsyncMock(return_value=100_000_000)
    clob.place_limit_order = AsyncMock(
        return_value=OrderResult(order_id="ok_id", success=True),
    )
    executor = Executor(clob, tracker)

    await store.insert_position(
        event_id="ev_sell", token_id="tok_under", token_type="NO", city="Chicago",
        slot_label="80°F to 84°F", side="BUY", entry_price=0.50, size_usd=25.0,
        shares=50.0, strategy="D", buy_reason="seed",
    )
    sig = _sell_signal_with_token("tok_under", price_no=0.40)
    await executor.execute_signals([sig])

    clob.place_limit_order.assert_called_once()
    call_kwargs = clob.place_limit_order.call_args.kwargs
    assert call_kwargs["size"] == 50.0
    await store.close()
