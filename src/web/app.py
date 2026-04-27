"""Flask web dashboard for the Polymarket Weather Trading Bot."""
from __future__ import annotations

import asyncio
import hmac
import logging
import re
import threading
import time
from datetime import date
from pathlib import Path

from flask import Flask, jsonify, render_template, request

from src.config import get_strategy_variants
from src.portfolio.utils import effective_entry_price
from src.web.strategy_meta import (
    active_variant_keys,
    empty_strategy_aggregation,
    fold_legacy_into_active,
    strat_meta,
)

logger = logging.getLogger(__name__)

# Persistent event loop running in a background thread
_bg_loop: asyncio.AbstractEventLoop | None = None
_bg_thread: threading.Thread | None = None
_bg_lock = threading.Lock()
# W-01 fix: track which threads are currently inside _run_async to detect
# re-entrant calls that would deadlock the background event loop.
_active_threads: set[int] = set()
_active_threads_lock = threading.Lock()


def _ensure_bg_loop() -> asyncio.AbstractEventLoop:
    """Start a background event loop thread (once) for running async DB queries."""
    global _bg_loop, _bg_thread
    with _bg_lock:
        if _bg_loop is None or _bg_thread is None or not _bg_thread.is_alive():
            _bg_loop = asyncio.new_event_loop()
            _bg_thread = threading.Thread(target=_bg_loop.run_forever, daemon=True)
            _bg_thread.start()
    return _bg_loop


def _run_async(coro, timeout: float = 10):
    """Run async coroutine on the persistent background loop (fast).

    W-01 fix: detects re-entrant calls from the same thread.  If a coroutine
    scheduled on the background loop somehow triggers another _run_async call
    (e.g. via a callback that runs on a Flask worker thread that is already
    blocked waiting for the first result), the second call would deadlock
    because future.result() blocks the thread while the loop can't proceed.
    This guard raises immediately instead of hanging.
    """
    tid = threading.get_ident()
    with _active_threads_lock:
        if tid in _active_threads:
            raise RuntimeError(
                "_run_async re-entrant call detected — would deadlock the "
                "background event loop. Refactor the caller to avoid nesting "
                "sync-over-async bridges."
            )
        _active_threads.add(tid)
    try:
        loop = _ensure_bg_loop()
        future = asyncio.run_coroutine_threadsafe(coro, loop)
        return future.result(timeout=timeout)
    finally:
        with _active_threads_lock:
            _active_threads.discard(tid)


# Simple TTL cache for dashboard data
_cache: dict[str, tuple[float, object]] = {}
_CACHE_TTL = 5  # seconds


def _cached(key: str, ttl: float = _CACHE_TTL):
    """Decorator-like: return cached value if fresh, else None."""
    if key in _cache:
        ts, val = _cache[key]
        if time.time() - ts < ttl:
            return val
    return None


def _set_cache(key: str, val: object):
    _cache[key] = (time.time(), val)


async def _fetch_gamma_prices(token_ids: list[str]) -> dict[str, float]:
    """Fetch current prices from Gamma API for a list of CLOB token IDs.

    Calls /markets?clob_token_ids=<id1>&clob_token_ids=<id2>&... using
    repeated query parameters (comma-joined values return HTTP 422).
    Batches to stay within reasonable URL length limits.
    """
    import json as _json
    import httpx as _httpx

    if not token_ids:
        return {}

    prices: dict[str, float] = {}
    try:
        async with _httpx.AsyncClient(timeout=5) as client:
            batch_size = 20
            for i in range(0, len(token_ids), batch_size):
                batch = token_ids[i:i + batch_size]
                # Gamma API requires repeated params, not comma-joined values.
                # httpx accepts a list of (key, value) tuples for repeated params.
                params = [("clob_token_ids", tid) for tid in batch]
                resp = await client.get(
                    "https://gamma-api.polymarket.com/markets",
                    params=params,
                )
                resp.raise_for_status()
                data = resp.json()
                if not isinstance(data, list):
                    continue
                for mkt in data:
                    tokens_raw = mkt.get("clobTokenIds", [])
                    prices_raw = mkt.get("outcomePrices", [])
                    if isinstance(tokens_raw, str):
                        try:
                            tokens_raw = _json.loads(tokens_raw)
                        except Exception:
                            tokens_raw = []
                    if isinstance(prices_raw, str):
                        try:
                            prices_raw = _json.loads(prices_raw)
                        except Exception:
                            prices_raw = []
                    for tid, p in zip(tokens_raw, prices_raw):
                        try:
                            prices[tid] = float(p)
                        except (ValueError, TypeError):
                            pass
    except Exception:
        logger.warning("Failed to fetch fresh Gamma prices for %d tokens", len(token_ids),
                       exc_info=True)
    return prices


_MONTH_SHORT = {
    "January": "Jan", "February": "Feb", "March": "Mar", "April": "Apr",
    "May": "May", "June": "Jun", "July": "Jul", "August": "Aug",
    "September": "Sep", "October": "Oct", "November": "Nov", "December": "Dec",
}
_MONTH_REGEX = "|".join(_MONTH_SHORT.keys())


def _parse_slot_label(label: str) -> tuple[str, str]:
    """Extract short slot description and market date from full slot_label.

    'Will the highest temperature in Seattle be between 66-67°F on April 5?'
    → ('66-67°F', 'Apr 5')

    'Will the highest temperature in Chicago be 56°F or higher on April 5?'
    → ('≥56°F', 'Apr 5')
    """
    # Extract temperature part
    temp = ""
    m = re.search(r'between (\d+-\d+°F)', label)
    if m:
        temp = m.group(1)
    else:
        m = re.search(r'(\d+)°F or (?:higher|above|more)', label)
        if m:
            temp = "≥" + m.group(1) + "°F"
        else:
            m = re.search(r'(\d+)°F or lower', label) or re.search(r'(?:below|under) (\d+)°F', label)
            if m:
                temp = "≤" + m.group(1) + "°F"

    # Extract date — supports all 12 months
    market_date = ""
    m = re.search(rf'on ({_MONTH_REGEX}) (\d+)', label)
    if m:
        market_date = f"{_MONTH_SHORT.get(m.group(1), m.group(1))} {m.group(2)}"

    return (temp or label[:25], market_date)


def _utc_to_beijing(utc_str: str) -> str:
    """Convert UTC datetime string to Beijing time (UTC+8)."""
    if not utc_str or len(utc_str) < 16:
        return utc_str or "-"
    try:
        from datetime import datetime, timedelta, timezone
        # Handle various formats
        clean = utc_str.replace("T", " ").replace("Z", "").split("+")[0].strip()
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
            try:
                dt = datetime.strptime(clean[:19], fmt)
                break
            except ValueError:
                continue
        else:
            return utc_str[:16]
        beijing = dt + timedelta(hours=8)
        return beijing.strftime("%m-%d %H:%M")
    except Exception:
        return utc_str[:16]


def create_app(store, rebalancer, config) -> Flask:
    template_dir = Path(__file__).parent / "templates"
    app = Flask(__name__, template_folder=str(template_dir))
    app.config["bot_store"] = store
    app.config["bot_rebalancer"] = rebalancer
    app.config["bot_config"] = config

    # Register Jinja filter for UTC → Beijing time
    app.jinja_env.filters["beijing"] = _utc_to_beijing

    def _mode():
        if config.dry_run:
            return "dry"
        if config.paper:
            return "paper"
        return "live"

    def _get_dashboard_data():
        """Fetch all dashboard data with caching."""
        cached = _cached("dashboard")
        if cached:
            return cached

        st = app.config["bot_store"]
        cfg = app.config["bot_config"]
        reb = app.config["bot_rebalancer"]

        positions = _run_async(st.get_open_positions())
        closed_pos = _run_async(st.get_closed_positions(limit=200))
        exposure = _run_async(st.get_total_exposure())
        # Blocker 4 (review): tracker's daily_pnl PK is UTC; using
        # server-local date.today() here meant ET evening dashboards
        # showed $0 because they queried tomorrow's still-empty bucket.
        from datetime import datetime as _dt, timezone as _tz
        daily_pnl_val = _run_async(
            st.get_daily_pnl(_dt.now(_tz.utc).date().isoformat()),
        )
        decision_log = _run_async(st.get_decision_log(limit=8))
        active_keys = active_variant_keys()
        active_set = set(active_keys)
        strategy_summary_raw = _run_async(st.get_strategy_summary()) if hasattr(st, 'get_strategy_summary') else []
        strategy_summary = [s for s in strategy_summary_raw if s.get("strategy") in active_set]
        strat_realized_raw = _run_async(st.get_strategy_realized_pnl()) if hasattr(st, 'get_strategy_realized_pnl') else {}
        strat_realized = fold_legacy_into_active(strat_realized_raw or {})
        # Add realized P&L from closed/settled positions on top of the
        # settlement-table aggregate.  Bucketed by the row's own strategy;
        # legacy keys remain visible alongside active variants.
        for p in closed_pos:
            rpnl = p.get("realized_pnl")
            if rpnl is None:
                continue
            key = p.get("strategy") or (active_keys[0] if active_keys else "B")
            strat_realized[key] = strat_realized.get(key, 0.0) + rpnl

        # W-03 fix: use positions table as single source of truth for realized P&L.
        # Previously this summed daily_pnl_val (which includes settlement P&L) PLUS
        # closed positions' realized_pnl (which also includes settlement P&L),
        # double-counting settlement gains/losses.
        total_realized = sum(
            p["realized_pnl"] for p in closed_pos if p.get("realized_pnl") is not None
        )

        state = reb.get_dashboard_state() if hasattr(reb, "get_dashboard_state") else {}

        signals = [
            {
                "city": s.get("city", ""),
                "token_type": s.get("token_type", ""),
                "side": s.get("side", ""),
                "slot_label": s.get("slot_label", ""),
                "ev": s.get("expected_value", 0),
                "win_prob": s.get("estimated_win_prob", 0),
                "size_usd": s.get("suggested_size_usd", 0),
            }
            for s in state.get("last_signals", [])
        ]

        data = {
            "positions": positions,
            "exposure": exposure,
            "daily_pnl_val": daily_pnl_val,
            "total_realized": total_realized,
            "decision_log": decision_log,
            "state": state,
            "signals": signals,
            "cities_with_positions": len(set(p["city"] for p in positions)),
            "strategy_summary": strategy_summary,
            "strat_realized": strat_realized,
        }
        _set_cache("dashboard", data)
        return data

    @app.route("/")
    def dashboard():
        cfg = app.config["bot_config"]
        d = _get_dashboard_data()

        return render_template(
            "dashboard.html",
            active_page="dashboard",
            mode=_mode(),
            exposure=d["exposure"],
            max_exposure=cfg.strategy.max_total_exposure_usd,
            unrealized=d["state"].get("unrealized", 0.0),
            active_events=d["state"].get("active_events", 0),
            signals=d["signals"],
            positions=d["positions"],
            cities_with_positions=d["cities_with_positions"],
            trends=d["state"].get("trends", {}),
            forecasts=d["state"].get("forecasts", {}),
            daily_maxes=d["state"].get("daily_maxes", {}),
            realized=d["total_realized"],
            strategy_summary=d.get("strategy_summary", []),
            strat_realized=d.get("strat_realized", empty_strategy_aggregation()),
            strat_meta=strat_meta(),
            daily_loss_remaining=cfg.strategy.daily_loss_limit_usd - abs(d["total_realized"]),
            daily_loss_limit=cfg.strategy.daily_loss_limit_usd,
            decision_log=d["decision_log"],
            price_source=d["state"].get("price_source", "gamma"),
        )

    @app.route("/positions")
    def positions_page():
        cfg = app.config["bot_config"]
        st = app.config["bot_store"]
        reb = app.config["bot_rebalancer"]

        open_pos = _run_async(st.get_open_positions())
        closed_pos = _run_async(st.get_closed_positions(limit=200))
        exposure = _run_async(st.get_total_exposure())
        active_keys = active_variant_keys()
        active_set = set(active_keys)
        fallback_strat = active_keys[0] if active_keys else "B"
        strategy_summary_raw = _run_async(st.get_strategy_summary()) if hasattr(st, 'get_strategy_summary') else []
        strategy_summary = [s for s in strategy_summary_raw if s.get("strategy") in active_set]
        strat_realized_raw = _run_async(st.get_strategy_realized_pnl()) if hasattr(st, 'get_strategy_realized_pnl') else {}
        strat_realized = fold_legacy_into_active(strat_realized_raw or {})
        # Add realized P&L from closed/settled positions on top of the
        # settlement-table aggregate.  Bucketed by the row's own strategy.
        for p in closed_pos:
            rpnl = p.get("realized_pnl")
            if rpnl is None:
                continue
            key = p.get("strategy") or fallback_strat
            strat_realized[key] = strat_realized.get(key, 0.0) + rpnl

        # Get current prices for P&L calculation.
        # Use the shared prices_fresh cache (same 30s TTL as /api/prices).
        # If the cache is cold, build it now: rebalancer cache + fresh Gamma fetch.
        # This ensures the initial page render always shows live P&L, not just '-'.
        gamma_prices = _cached("prices_fresh")
        if gamma_prices is None:
            _reb_cache = reb.get_gamma_prices() if hasattr(reb, 'get_gamma_prices') else {}
            gamma_prices = dict(_reb_cache)
            _pos_token_ids = [p["token_id"] for p in open_pos if p.get("token_id")]
            if _pos_token_ids:
                try:
                    _fresh = _run_async(_fetch_gamma_prices(_pos_token_ids), timeout=5)
                    gamma_prices.update(_fresh)
                except Exception:
                    pass
            _set_cache("prices_fresh", gamma_prices)

        # Enrich positions with current price, unrealized P&L, and parsed slot info.
        # 2026-04-28: prefer ``match_price`` (actual per-share fill from /data/trades)
        # over ``entry_price`` (limit submitted) so unrealized P&L reflects the
        # actual cost basis after tick-level slippage.  Legacy / paper rows have
        # match_price=NULL and continue to use entry_price.  ``effective_entry_price``
        # is the single source of truth — see src/portfolio/utils.py.
        for p in open_pos:
            effective_entry = effective_entry_price(p)
            current = gamma_prices.get(p["token_id"])
            if current is not None:
                p["current_price"] = current
                p["unrealized_pnl"] = round((current - effective_entry) * p["shares"], 4)
            else:
                p["current_price"] = None
                p["unrealized_pnl"] = None
            p["strategy"] = p.get("strategy") or fallback_strat
            p["buy_reason"] = p.get("buy_reason", "")
            p["slot_short"], p["market_date"] = _parse_slot_label(p.get("slot_label", ""))

        # Enrich closed positions with parsed slot info — keep their original
        # strategy key so legacy rows show up under their own column.
        for p in closed_pos:
            p["slot_short"], p["market_date"] = _parse_slot_label(p.get("slot_label", ""))
            p["buy_reason"] = p.get("buy_reason", "")
            p["exit_reason"] = p.get("exit_reason", "")
            p["strategy"] = p.get("strategy") or fallback_strat

        # Group by strategy.  Active variants always get a (possibly empty)
        # group; legacy strategies still in the DB only appear when they have
        # at least one open or closed row.
        strategies: dict[str, list] = {k: [] for k in active_keys}
        strat_pnl: dict[str, float] = {k: 0.0 for k in active_keys}
        strat_exposure: dict[str, float] = {k: 0.0 for k in active_keys}
        for p in open_pos:
            key = p["strategy"]
            strategies.setdefault(key, []).append(p)
            strat_exposure[key] = strat_exposure.get(key, 0.0) + p["size_usd"]
            if p["unrealized_pnl"] is not None:
                strat_pnl[key] = strat_pnl.get(key, 0.0) + p["unrealized_pnl"]

        # Group by city
        cities = {}
        for p in open_pos:
            city = p["city"]
            if city not in cities:
                cities[city] = {"count": 0, "exposure": 0.0}
            cities[city]["count"] += 1
            cities[city]["exposure"] += p["size_usd"]

        return render_template(
            "positions.html",
            active_page="positions",
            mode=_mode(),
            positions=open_pos,
            closed_positions=closed_pos,
            total_exposure=exposure,
            global_limit=cfg.strategy.max_total_exposure_usd,
            cities=cities,
            city_limit=cfg.strategy.max_exposure_per_city_usd,
            strategies=strategies,
            strat_pnl=strat_pnl,
            strat_realized=strat_realized,
            strat_exposure=strat_exposure,
            strategy_summary=strategy_summary,
            strat_meta=strat_meta(),
        )

    @app.route("/markets")
    def markets_page():
        reb = app.config["bot_rebalancer"]
        state = reb.get_dashboard_state() if hasattr(reb, "get_dashboard_state") else {}

        return render_template(
            "markets.html",
            active_page="markets",
            mode=_mode(),
            events=state.get("markets", []),
            next_scan_minutes=app.config["bot_config"].scheduling.rebalance_interval_minutes,
        )

    @app.route("/trades")
    def trades_page():
        st = app.config["bot_store"]
        reb = app.config["bot_rebalancer"]
        gamma_prices = reb.get_gamma_prices() if hasattr(reb, 'get_gamma_prices') else {}

        # Fetch all data sources
        decisions = _run_async(st.get_decision_log(limit=100))
        open_pos = _run_async(st.get_open_positions())
        closed_pos = _run_async(st.get_closed_positions(limit=50))

        # Build lookup: (city, slot_label, strategy) → latest BUY decision reason
        buy_reasons = {}
        for d in decisions:
            if d.get("action") == "BUY":
                key = (d.get("city", ""), d.get("slot_label", ""), d.get("strategy", "B"))
                if key not in buy_reasons:
                    buy_reasons[key] = d.get("reason", "") or f"EV={d.get('expected_value', 0):.3f}, win={d.get('win_prob', 0)*100:.0f}%"

        # Build unified timeline
        timeline = []

        # 1. Open positions (BUY rows)
        for p in open_pos:
            current = gamma_prices.get(p["token_id"])
            # B1 (2026-04-28): cost basis = match_price when present, else
            # entry_price (limit) as documented fallback for legacy / paper.
            entry = effective_entry_price(p)
            unrealized = round((current - entry) * p["shares"], 3) if current else None
            reason = p.get("buy_reason") or ""
            if not reason:
                reason_key = (p["city"], p.get("slot_label", ""), p.get("strategy", "B"))
                reason = buy_reasons.get(reason_key, "")
            slot_short, market_date = _parse_slot_label(p.get("slot_label", ""))
            timeline.append({
                "time": p["created_at"][:16] if p.get("created_at") else "",
                "city": p["city"],
                "slot": slot_short,
                "market_date": market_date,
                "strategy": p.get("strategy", "B"),
                "action": "BUY",
                "size": f"${p['size_usd']:.1f}",
                "entry": f"{entry:.3f}",
                "exit": "-",
                "current": f"{current:.3f}" if current else "-",
                "pnl": f"{'+'if unrealized>0 else ''}${unrealized:.3f}" if unrealized is not None else "-",
                "reason": reason,
                "type": "open",
                "sort_key": p.get("created_at", ""),
            })

        # 2. Closed/settled positions (SELL rows)
        # Build sell reason lookup
        sell_reasons = {}
        for d in decisions:
            if d.get("action") == "SELL":
                key = (d.get("city", ""), d.get("slot_label", ""), d.get("strategy", "B"))
                if key not in sell_reasons:
                    sell_reasons[key] = d.get("reason", "") or "Trim/Exit"

        for p in closed_pos:
            slot_short, market_date = _parse_slot_label(p.get("slot_label", ""))
            exit_reason = p.get("exit_reason") or ""
            if not exit_reason:
                reason_key = (p["city"], p.get("slot_label", ""), p.get("strategy", "B"))
                exit_reason = sell_reasons.get(reason_key, "Settled" if p.get("status") == "settled" else "Closed")
            timeline.append({
                "time": p.get("closed_at", "")[:16] if p.get("closed_at") else "",
                "city": p["city"],
                "slot": slot_short,
                "market_date": market_date,
                "strategy": p.get("strategy", "B"),
                "action": "SELL",
                "size": f"${p['size_usd']:.1f}",
                # B1: closed rows show effective entry (cost basis), not the
                # limit price — keeps the entry / exit / P&L triplet self-consistent.
                "entry": f"{effective_entry_price(p):.3f}",
                "exit": f"{p['exit_price']:.3f}" if p.get("exit_price") is not None else "-",
                "current": "-",
                "pnl": f"{'+'if p.get('realized_pnl',0)>0 else ''}${p['realized_pnl']:.3f}" if p.get("realized_pnl") is not None else "-",
                "reason": exit_reason,
                "type": "settled" if p.get("status") == "settled" else "closed",
                "sort_key": p.get("closed_at") or p.get("created_at", ""),
            })
            # 2b. Hidden-by-default BUY row for the same position so the
            #     "Show entries" toggle reveals the full lifecycle.  The
            #     row lands at ``created_at`` (open time) so it sorts
            #     above the SELL row when both are visible.
            buy_reason = p.get("buy_reason") or ""
            if not buy_reason:
                buy_reason_key = (p["city"], p.get("slot_label", ""), p.get("strategy", "B"))
                buy_reason = buy_reasons.get(buy_reason_key, "")
            timeline.append({
                "time": p["created_at"][:16] if p.get("created_at") else "",
                "city": p["city"],
                "slot": slot_short,
                "market_date": market_date,
                "strategy": p.get("strategy", "B"),
                "action": "BUY",
                "size": f"${p['size_usd']:.1f}",
                # B1: hidden BUY companion row uses the same effective-entry as the
                # SELL row above so both halves of the lifecycle agree.
                "entry": f"{effective_entry_price(p):.3f}",
                "exit": "-",
                "current": "-",
                "pnl": "-",
                "reason": buy_reason,
                "type": "closed_entry",
                "sort_key": p.get("created_at", ""),
            })

        # 3. SKIP decisions
        for d in decisions:
            if d.get("action") == "SKIP":
                skip_slot_short, skip_market_date = _parse_slot_label(d.get("slot_label", ""))
                timeline.append({
                    "time": d["cycle_at"][:16] if d.get("cycle_at") else "",
                    "city": d.get("city", ""),
                    "slot": skip_slot_short,
                    "market_date": skip_market_date,
                    "strategy": d.get("strategy", ""),
                    "action": "SKIP",
                    "size": "-",
                    "entry": f"{d['price']:.3f}" if d.get("price") else "-",
                    "exit": "-",
                    "current": "-",
                    "pnl": "-",
                    "reason": d.get("reason", ""),
                    "type": "decision",
                    "sort_key": d.get("cycle_at", ""),
                })

        # Sort by time descending
        timeline.sort(key=lambda x: x.get("sort_key", ""), reverse=True)

        # Per-strategy stats — bucketed by the row's own strategy.  Active
        # variants always get a (possibly zero-valued) row; legacy keys
        # show up only when at least one open or closed position carries
        # them.  No more "everything rolled into B" remap.
        active_keys = active_variant_keys()
        fallback_strat = active_keys[0] if active_keys else "B"
        strat_pnl: dict[str, float] = {k: 0.0 for k in active_keys}
        strat_exposure: dict[str, float] = {k: 0.0 for k in active_keys}
        strat_counts: dict[str, dict] = {
            k: {"open": 0, "settled": 0} for k in active_keys
        }
        for p in open_pos:
            key = p.get("strategy") or fallback_strat
            strat_counts.setdefault(key, {"open": 0, "settled": 0})
            strat_exposure[key] = strat_exposure.get(key, 0.0) + p["size_usd"]
            strat_counts[key]["open"] += 1
            current = gamma_prices.get(p["token_id"])
            if current:
                # B1: per-strategy unrealized P&L must use effective entry too,
                # otherwise the timeline strat-summary disagrees with the row P&L.
                strat_pnl[key] = strat_pnl.get(key, 0.0) + (current - effective_entry_price(p)) * p["shares"]
        for p in closed_pos:
            key = p.get("strategy") or fallback_strat
            strat_counts.setdefault(key, {"open": 0, "settled": 0})
            strat_exposure.setdefault(key, 0.0)
            strat_pnl.setdefault(key, 0.0)
            if p.get("status") == "settled":
                strat_counts[key]["settled"] += 1
            if p.get("realized_pnl") is not None:
                strat_pnl[key] = strat_pnl.get(key, 0.0) + p["realized_pnl"]

        # Limit bumped from 80 → 120 because every closed position now
        # emits an extra ``closed_entry`` row (hidden until toggled).
        # After the limit, still-hidden rows count against visible ones,
        # but the earliest few are what operators usually care about.
        # Count closed_entry rows from the *full* timeline so the button
        # label reflects the real total rather than only in-view rows
        # (older BUY rows beyond the 120-row window would otherwise be
        # invisible to the label).
        closed_entry_count = sum(1 for t in timeline if t.get("type") == "closed_entry")
        limited = timeline[:120]

        return render_template(
            "trades.html",
            active_page="trades",
            mode=_mode(),
            timeline=limited,
            closed_entry_count=closed_entry_count,
            strat_pnl=strat_pnl,
            strat_exposure=strat_exposure,
            strat_counts=strat_counts,
            strat_meta=strat_meta(),
        )

    @app.route("/analytics")
    def analytics_page():
        st = app.config["bot_store"]
        cached = _cached("analytics", ttl=15)
        if cached:
            edge_summary, edge_history, decision_log = cached
        else:
            edge_summary = _run_async(st.get_edge_summary())
            edge_history = _run_async(st.get_edge_history(limit=200))
            decision_log = _run_async(st.get_decision_log(limit=30))
            _set_cache("analytics", (edge_summary, edge_history, decision_log))

        return render_template(
            "analytics.html",
            active_page="analytics",
            mode=_mode(),
            edge_summary=edge_summary,
            edge_history=edge_history,
            decision_log=decision_log,
        )

    @app.route("/history")
    def history_page():
        st = app.config["bot_store"]
        pnl_history = _run_async(st.get_pnl_history())
        settlements = _run_async(st.get_settlements())

        return render_template(
            "history.html",
            active_page="history",
            mode=_mode(),
            pnl_history=pnl_history,
            settlements=settlements,
            strat_meta=strat_meta(),
        )

    @app.route("/config")
    def config_page():
        cfg = app.config["bot_config"]
        return render_template(
            "config.html",
            active_page="config",
            mode=_mode(),
            strategy=cfg.strategy,
            scheduling=cfg.scheduling,
            cities=cfg.cities,
            variants=get_strategy_variants(),
            strat_meta=strat_meta(),
        )

    @app.route("/temperatures")
    def temperatures_page():
        cfg = app.config["bot_config"]
        reb = app.config["bot_rebalancer"]
        state = reb.get_dashboard_state() if hasattr(reb, "get_dashboard_state") else {}

        obs_series = state.get("observation_series", {})
        forecasts = state.get("forecasts", {})
        daily_maxes = state.get("daily_maxes", {})
        error_dists = state.get("error_dists", {})
        # Only show cities with active market data
        active_cities = sorted(set(obs_series.keys()) | set(forecasts.keys()) | set(daily_maxes.keys()))
        # City → IANA timezone string for local time display
        city_tzs = {c.name: c.tz for c in cfg.cities if getattr(c, "tz", None)}
        return render_template(
            "temperatures.html",
            active_page="temperatures",
            mode=_mode(),
            cities=active_cities,
            observation_series=obs_series,
            forecasts=forecasts,
            daily_maxes=daily_maxes,
            trends=state.get("trends", {}),
            city_timezones=city_tzs,
            error_dists=error_dists,
        )

    @app.route("/api/prices")
    def api_prices():
        """Return current prices for all open position token IDs.

        Primary source: rebalancer's in-memory cache (refreshed every 15 min by
        position check + every 60 min by rebalance cycle).  Augmented with a
        best-effort fresh Gamma fetch when available.

        The fresh Gamma fetch is optional — if it times out or fails for any
        reason the endpoint always falls back to the rebalancer cache so the JS
        poller always gets a 200 response with prices, never a 500.
        """
        cached = _cached("prices_fresh", ttl=30)
        if cached is not None:
            return jsonify(cached)

        st = app.config["bot_store"]
        reb = app.config["bot_rebalancer"]

        open_pos = _run_async(st.get_open_positions())
        token_ids = list({p["token_id"] for p in open_pos if p.get("token_id")})

        # Start with rebalancer cache as the guaranteed baseline.
        # The rebalancer's position check already fetches fresh Gamma prices every
        # 15 min, so this is at most 15 minutes stale — good enough for the UI.
        cached_prices = reb.get_gamma_prices() if hasattr(reb, "get_gamma_prices") else {}
        prices = dict(cached_prices)  # copy so we can update safely

        # Best-effort fresh Gamma fetch (shorter timeout to avoid blocking the
        # background asyncio loop and causing concurrent.futures.TimeoutError).
        # If it fails for any reason we silently use the rebalancer cache above.
        if token_ids:
            try:
                fresh = _run_async(_fetch_gamma_prices(token_ids), timeout=8)
                prices.update(fresh)  # overlay fresh prices on top of cached
            except Exception:
                pass  # rebalancer cache is already in prices — no further action

        _set_cache("prices_fresh", prices)
        return jsonify(prices)

    @app.route("/api/temperatures")
    def api_temperatures():
        """Temperature observation time series + forecasts for real-time refresh."""
        cached = _cached("temperatures", ttl=30)
        if cached is not None:
            return jsonify(cached)

        reb = app.config["bot_rebalancer"]
        state = reb.get_dashboard_state() if hasattr(reb, "get_dashboard_state") else {}

        data = {
            "observation_series": state.get("observation_series", {}),
            "forecasts": state.get("forecasts", {}),
            "daily_maxes": state.get("daily_maxes", {}),
            "error_dists": state.get("error_dists", {}),
        }
        _set_cache("temperatures", data)
        return jsonify(data)

    @app.route("/api/status")
    def api_status():
        """Lightweight status endpoint — uses cache, no heavy DB queries."""
        reb = app.config["bot_rebalancer"]
        state = reb.get_dashboard_state() if hasattr(reb, "get_dashboard_state") else {}

        # Only query exposure if cache is stale
        cached_exp = _cached("exposure", ttl=10)
        if cached_exp is None:
            cached_exp = _run_async(app.config["bot_store"].get_total_exposure())
            _set_cache("exposure", cached_exp)

        return jsonify({
            "mode": _mode(),
            "exposure": cached_exp,
            "unrealized": state.get("unrealized", 0.0),
            "active_events": state.get("active_events", 0),
            "signal_count": len(state.get("last_signals", [])),
            "last_run": state.get("last_run"),
            "last_error": state.get("last_error"),
            "trends": state.get("trends", {}),
        })

    def _admin_auth_check():
        """Gate for /api/trigger, /api/admin/pause and /api/admin/unpause.

        Blocker 5 (review): the previous implementation fail-OPENED on an
        empty TRIGGER_SECRET — fine for loopback-only dev, but the same
        binary runs on the VPS where `curl http://198.23.134.31:5001/...`
        is reachable from the public internet.  Without auth, anyone could
        flip the kill switch or fire a rebalance.

        New behaviour:
          - secret set, header matches  → allow (return None)
          - secret set, header missing/wrong  → 401
          - secret empty + ADMIN_NOAUTH=1 + paper/dry-run mode  → allow
            (explicit dev opt-in; never works in live mode)
          - secret empty otherwise  → 503 admin disabled

        Returns None when the caller is authorized.  Returns a Flask
        response tuple `(jsonify(...), status)` otherwise — the endpoint
        propagates it via `if err is not None: return err`.
        """
        import os
        cfg = app.config.get("bot_config")
        secret = getattr(cfg, "trigger_secret", "") if cfg else ""
        if not secret:
            # Dev-only opt-in; never honour in live mode.
            opt_in = os.environ.get("ADMIN_NOAUTH") == "1"
            is_non_prod = bool(
                getattr(cfg, "paper", False) or getattr(cfg, "dry_run", False)
            )
            if opt_in and is_non_prod:
                return None
            logger.warning(
                "Admin endpoint refused (no TRIGGER_SECRET; ADMIN_NOAUTH=%s, "
                "paper=%s, dry_run=%s) from %s",
                os.environ.get("ADMIN_NOAUTH"),
                getattr(cfg, "paper", False), getattr(cfg, "dry_run", False),
                request.remote_addr,
            )
            return (
                jsonify({"error": "admin endpoints disabled — TRIGGER_SECRET not configured"}),
                503,
            )
        auth_header = request.headers.get("Authorization", "")
        if hmac.compare_digest(auth_header, f"Bearer {secret}"):
            return None
        x_secret = request.headers.get("X-Trigger-Secret", "")
        if x_secret and hmac.compare_digest(x_secret, secret):
            return None
        return jsonify({"error": "unauthorized"}), 401

    @app.route("/api/admin/pause", methods=["POST"])
    def api_admin_pause():
        """FIX-11: flip the kill switch on — rebalancer will skip BUYs next
        cycle.  TRIM / EXIT / settlement continue so positions can still be
        closed.  Auth via TRIGGER_SECRET (Authorization: Bearer … or
        X-Trigger-Secret header)."""
        err = _admin_auth_check()
        if err is not None:
            return err
        s = app.config["bot_store"]
        try:
            _run_async(s.set_bot_paused(True), timeout=5)
            return jsonify({"ok": True, "paused": True})
        except Exception as e:
            logger.exception("admin pause failed")
            return jsonify({"ok": False, "error": str(e)}), 500

    @app.route("/api/admin/unpause", methods=["POST"])
    def api_admin_unpause():
        """FIX-11: clear the kill switch."""
        err = _admin_auth_check()
        if err is not None:
            return err
        s = app.config["bot_store"]
        try:
            _run_async(s.set_bot_paused(False), timeout=5)
            return jsonify({"ok": True, "paused": False})
        except Exception as e:
            logger.exception("admin unpause failed")
            return jsonify({"ok": False, "error": str(e)}), 500

    @app.route("/api/trigger", methods=["POST"])
    def api_trigger():
        # Token auth: if TRIGGER_SECRET is set, require "Authorization: Bearer <secret>"
        # so arbitrary callers cannot trigger expensive full rebalance cycles.
        err = _admin_auth_check()
        if err is not None:
            return err

        reb = app.config["bot_rebalancer"]
        _cache.clear()  # Invalidate all caches
        try:
            # Rebalance can take 30-60s (NWS/Gamma API calls), use longer timeout
            signals = _run_async(reb.run(), timeout=120)
            return jsonify({"ok": True, "signals": len(signals)})
        except TimeoutError:
            # Rebalance is still running in bg loop — report as accepted
            logger.warning("Manual rebalance timed out (still running in background)")
            return jsonify({"ok": True, "signals": -1, "note": "running in background"})
        except Exception as e:
            logger.exception("Manual rebalance trigger failed")
            return jsonify({"ok": False, "error": str(e)}), 500

    # --- Error handlers ---
    @app.errorhandler(500)
    def internal_error(e):
        logger.exception("Internal server error: %s", e)
        return render_template("error.html", active_page="", mode=_mode(),
                               code=500, message="Internal Server Error",
                               detail=str(e)), 500

    @app.errorhandler(404)
    def not_found(e):
        return render_template("error.html", active_page="", mode=_mode(),
                               code=404, message="Page Not Found",
                               detail="The requested page does not exist."), 404

    return app


def run_web_server(store, rebalancer, config, port: int = 5001):
    app = create_app(store, rebalancer, config)
    logger.info("Web dashboard starting at http://localhost:%d", port)
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)
