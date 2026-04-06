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


def create_app(store, rebalancer, config) -> Flask:
    template_dir = Path(__file__).parent / "templates"
    app = Flask(__name__, template_folder=str(template_dir))
    app.config["bot_store"] = store
    app.config["bot_rebalancer"] = rebalancer
    app.config["bot_config"] = config

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
            daily_loss_remaining=cfg.strategy.daily_loss_limit_usd - abs(d["daily_pnl_val"] or 0),
            daily_loss_limit=cfg.strategy.daily_loss_limit_usd,
            decision_log=d["decision_log"],
            price_source=d["state"].get("price_source", "gamma"),
        )

    @app.route("/positions")
    def positions_page():
        cfg = app.config["bot_config"]
        st = app.config["bot_store"]

        open_pos = _run_async(st.get_open_positions())
        closed_pos = _run_async(st.get_closed_positions(limit=20))
        exposure = _run_async(st.get_total_exposure())

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
