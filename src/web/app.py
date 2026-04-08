"""Flask web dashboard for the Polymarket Weather Trading Bot."""
from __future__ import annotations

import asyncio
import logging
import threading
import time
from datetime import date
from pathlib import Path

from flask import Flask, jsonify, render_template

logger = logging.getLogger(__name__)

# Persistent event loop running in a background thread
_bg_loop: asyncio.AbstractEventLoop | None = None
_bg_thread: threading.Thread | None = None
_bg_lock = threading.Lock()


def _ensure_bg_loop() -> asyncio.AbstractEventLoop:
    """Start a background event loop thread (once) for running async DB queries."""
    global _bg_loop, _bg_thread
    with _bg_lock:
        if _bg_loop is None or _bg_thread is None or not _bg_thread.is_alive():
            _bg_loop = asyncio.new_event_loop()
            _bg_thread = threading.Thread(target=_bg_loop.run_forever, daemon=True)
            _bg_thread.start()
    return _bg_loop


def _run_async(coro):
    """Run async coroutine on the persistent background loop (fast)."""
    loop = _ensure_bg_loop()
    future = asyncio.run_coroutine_threadsafe(coro, loop)
    return future.result(timeout=10)


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
        exposure = _run_async(st.get_total_exposure())
        daily_pnl_val = _run_async(st.get_daily_pnl(date.today().isoformat()))
        decision_log = _run_async(st.get_decision_log(limit=8))
        strategy_summary = _run_async(st.get_strategy_summary()) if hasattr(st, 'get_strategy_summary') else []
        strat_realized = _run_async(st.get_strategy_realized_pnl()) if hasattr(st, 'get_strategy_realized_pnl') else {"A": 0.0, "B": 0.0, "C": 0.0}

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
            realized=d["daily_pnl_val"] or 0.0,
            strategy_summary=d.get("strategy_summary", []),
            strat_realized=d.get("strat_realized", {"A": 0.0, "B": 0.0, "C": 0.0}),
            daily_loss_remaining=cfg.strategy.daily_loss_limit_usd - abs(d["daily_pnl_val"] or 0),
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
        closed_pos = _run_async(st.get_closed_positions(limit=20))
        exposure = _run_async(st.get_total_exposure())
        strategy_summary = _run_async(st.get_strategy_summary()) if hasattr(st, 'get_strategy_summary') else []
        strat_realized = _run_async(st.get_strategy_realized_pnl()) if hasattr(st, 'get_strategy_realized_pnl') else {"A": 0.0, "B": 0.0, "C": 0.0}

        # Get current prices for P&L calculation
        gamma_prices = reb._last_gamma_prices if hasattr(reb, '_last_gamma_prices') else {}

        # Enrich positions with current price and unrealized P&L
        for p in open_pos:
            current = gamma_prices.get(p["token_id"])
            if current is not None:
                p["current_price"] = current
                p["unrealized_pnl"] = round((current - p["entry_price"]) * p["shares"], 4)
            else:
                p["current_price"] = None
                p["unrealized_pnl"] = None
            p["strategy"] = p.get("strategy", "B")

        # Group by strategy
        strategies = {"A": [], "B": [], "C": []}
        strat_pnl = {"A": 0.0, "B": 0.0, "C": 0.0}  # unrealized
        strat_exposure = {"A": 0.0, "B": 0.0, "C": 0.0}
        for p in open_pos:
            s = p.get("strategy", "B")
            if s in strategies:
                strategies[s].append(p)
                strat_exposure[s] += p["size_usd"]
                if p["unrealized_pnl"] is not None:
                    strat_pnl[s] += p["unrealized_pnl"]

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
        gamma_prices = reb._last_gamma_prices if hasattr(reb, '_last_gamma_prices') else {}

        # Fetch all data sources
        decisions = _run_async(st.get_decision_log(limit=100))
        open_pos = _run_async(st.get_open_positions())
        closed_pos = _run_async(st.get_closed_positions(limit=50))
        settlements = _run_async(st.get_settlements())

        # Build lookup: (city, slot_label) → latest BUY decision reason
        buy_reasons = {}
        for d in decisions:
            if d.get("action") == "BUY":
                key = (d.get("city", ""), d.get("slot_label", ""))
                if key not in buy_reasons:
                    buy_reasons[key] = d.get("reason", "") or f"EV={d.get('expected_value', 0):.3f}, win={d.get('win_prob', 0)*100:.0f}%"

        # Build unified timeline
        timeline = []

        # 1. Open positions
        for p in open_pos:
            current = gamma_prices.get(p["token_id"])
            entry = p["entry_price"]
            pnl = round((current - entry) * p["shares"], 3) if current else None
            reason_key = (p["city"], p.get("slot_label", ""))
            reason = buy_reasons.get(reason_key, "")
            timeline.append({
                "time": p["created_at"][:16] if p.get("created_at") else "",
                "city": p["city"],
                "slot": p["slot_label"][:40] if p.get("slot_label") else "",
                "strategy": p.get("strategy", "B"),
                "action": "BUY",
                "forecast": "",
                "win_prob": "",
                "ev": "",
                "entry": f"{entry:.3f}",
                "current": f"{current:.3f}" if current else "-",
                "pnl": f"{'+'if pnl and pnl>0 else ''}${pnl:.3f}" if pnl is not None else "-",
                "reason": reason,
                "type": "open",
                "sort_key": p.get("created_at", ""),
            })

        # 2. Settled/closed positions
        # Build sell reason lookup
        sell_reasons = {}
        for d in decisions:
            if d.get("action") == "SELL":
                key = (d.get("city", ""), d.get("slot_label", ""))
                if key not in sell_reasons:
                    sell_reasons[key] = d.get("reason", "") or "Trim/Exit"

        for p in closed_pos:
            reason_key = (p["city"], p.get("slot_label", ""))
            timeline.append({
                "time": p.get("closed_at", "")[:16] if p.get("closed_at") else "",
                "city": p["city"],
                "slot": p["slot_label"][:40] if p.get("slot_label") else "",
                "strategy": p.get("strategy", "B"),
                "action": "SELL",
                "forecast": "", "win_prob": "", "ev": "",
                "entry": f"{p['entry_price']:.3f}",
                "current": "-",
                "pnl": "-",
                "reason": sell_reasons.get(reason_key, "Settled" if p.get("status") == "settled" else "Closed"),
                "type": "settled" if p.get("status") == "settled" else "closed",
                "sort_key": p.get("closed_at") or p.get("created_at", ""),
            })

        # 3. SKIP decisions
        for d in decisions:
            if d.get("action") == "SKIP":
                timeline.append({
                    "time": d["cycle_at"][:16] if d.get("cycle_at") else "",
                    "city": d.get("city", ""),
                    "slot": d["slot_label"][:40] if d.get("slot_label") else "",
                    "strategy": "",
                    "action": "SKIP",
                    "forecast": f"{d['forecast_high_f']:.0f}°F" if d.get("forecast_high_f") else "-",
                    "win_prob": f"{d['win_prob']*100:.0f}%" if d.get("win_prob") else "-",
                    "ev": f"{d['expected_value']:.3f}" if d.get("expected_value") else "-",
                    "entry": "-", "current": "-", "pnl": "-",
                    "reason": d.get("reason", ""),
                    "type": "decision",
                    "sort_key": d.get("cycle_at", ""),
                })

        # Sort by time descending
        timeline.sort(key=lambda x: x.get("sort_key", ""), reverse=True)

        # Compute per-strategy stats
        strat_pnl = {"A": 0.0, "B": 0.0, "C": 0.0}
        strat_exposure = {"A": 0.0, "B": 0.0, "C": 0.0}
        strat_counts = {"A": {"open": 0, "settled": 0}, "B": {"open": 0, "settled": 0}, "C": {"open": 0, "settled": 0}}
        for p in open_pos:
            s = p.get("strategy", "B")
            if s in strat_pnl:
                strat_exposure[s] += p["size_usd"]
                strat_counts[s]["open"] += 1
                current = gamma_prices.get(p["token_id"])
                if current:
                    strat_pnl[s] += (current - p["entry_price"]) * p["shares"]
        for p in closed_pos:
            s = p.get("strategy", "B")
            if s in strat_counts and p.get("status") == "settled":
                strat_counts[s]["settled"] += 1

        return render_template(
            "trades.html",
            active_page="trades",
            mode=_mode(),
            timeline=timeline[:80],  # limit for performance
            strat_pnl=strat_pnl,
            strat_exposure=strat_exposure,
            strat_counts=strat_counts,
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
        )

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

    @app.route("/api/trigger", methods=["POST"])
    def api_trigger():
        reb = app.config["bot_rebalancer"]
        _cache.clear()  # Invalidate all caches
        try:
            signals = _run_async(reb.run())
            return jsonify({"ok": True, "signals": len(signals)})
        except Exception as e:
            logger.exception("Manual rebalance trigger failed")
            return jsonify({"ok": False, "error": str(e)}), 500

    return app


def run_web_server(store, rebalancer, config, port: int = 5001):
    app = create_app(store, rebalancer, config)
    logger.info("Web dashboard starting at http://localhost:%d", port)
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)
