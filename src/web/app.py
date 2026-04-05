"""Flask web dashboard for the Polymarket Weather Trading Bot."""
from __future__ import annotations

import asyncio
import logging
import threading
from datetime import date
from pathlib import Path

from flask import Flask, jsonify, render_template

logger = logging.getLogger(__name__)


def _run_async(coro):
    """Run an async coroutine from sync Flask context."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


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

    @app.route("/")
    def dashboard():
        st = app.config["bot_store"]
        reb = app.config["bot_rebalancer"]
        cfg = app.config["bot_config"]

        positions = _run_async(st.get_open_positions())
        exposure = _run_async(st.get_total_exposure())
        daily_pnl_val = _run_async(st.get_daily_pnl(date.today().isoformat()))
        decision_log = _run_async(st.get_decision_log(limit=20))

        state = reb.get_dashboard_state() if hasattr(reb, "get_dashboard_state") else {}

        signals = []
        for s in state.get("last_signals", []):
            signals.append({
                "city": s.get("city", ""),
                "token_type": s.get("token_type", ""),
                "side": s.get("side", ""),
                "slot_label": s.get("slot_label", ""),
                "ev": s.get("expected_value", 0),
                "win_prob": s.get("estimated_win_prob", 0),
                "size_usd": s.get("suggested_size_usd", 0),
            })

        # Count cities with open positions
        cities_set = set(p["city"] for p in positions)

        return render_template(
            "dashboard.html",
            active_page="dashboard",
            mode=_mode(),
            exposure=exposure,
            max_exposure=cfg.strategy.max_total_exposure_usd,
            unrealized=state.get("unrealized", 0.0),
            active_events=state.get("active_events", 0),
            signals=signals,
            positions=positions,
            cities_with_positions=len(cities_set),
            trends=state.get("trends", {}),
            forecasts=state.get("forecasts", {}),
            daily_loss_remaining=cfg.strategy.daily_loss_limit_usd - abs(daily_pnl_val or 0),
            daily_loss_limit=cfg.strategy.daily_loss_limit_usd,
            decision_log=decision_log,
        )

    @app.route("/positions")
    def positions_page():
        st = app.config["bot_store"]
        cfg = app.config["bot_config"]

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
            next_scan_minutes=config.scheduling.rebalance_interval_minutes,
        )

    @app.route("/analytics")
    def analytics_page():
        st = app.config["bot_store"]
        edge_summary = _run_async(st.get_edge_summary())
        edge_history = _run_async(st.get_edge_history(limit=200))
        decision_log = _run_async(st.get_decision_log(limit=30))

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
        st = app.config["bot_store"]
        reb = app.config["bot_rebalancer"]
        exposure = _run_async(st.get_total_exposure())
        state = reb.get_dashboard_state() if hasattr(reb, "get_dashboard_state") else {}

        return jsonify({
            "mode": _mode(),
            "exposure": exposure,
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
