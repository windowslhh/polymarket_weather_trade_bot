"""Flask web dashboard for the Polymarket Weather Trading Bot."""
from __future__ import annotations

import asyncio
import logging
import re
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


def _run_async(coro, timeout: float = 10):
    """Run async coroutine on the persistent background loop (fast)."""
    loop = _ensure_bg_loop()
    future = asyncio.run_coroutine_threadsafe(coro, loop)
    return future.result(timeout=timeout)


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
        daily_pnl_val = _run_async(st.get_daily_pnl(date.today().isoformat()))
        decision_log = _run_async(st.get_decision_log(limit=8))
        strategy_summary_raw = _run_async(st.get_strategy_summary()) if hasattr(st, 'get_strategy_summary') else []
        strategy_summary = [s for s in strategy_summary_raw if s.get("strategy") in {"A", "B", "C", "D"}]
        strat_realized = _run_async(st.get_strategy_realized_pnl()) if hasattr(st, 'get_strategy_realized_pnl') else {"A": 0.0, "B": 0.0, "C": 0.0, "D": 0.0}
        # Include realized P&L from closed/settled positions
        for p in closed_pos:
            rpnl = p.get("realized_pnl")
            if rpnl is not None:
                s = p.get("strategy", "B")
                if s in strat_realized:
                    strat_realized[s] += rpnl

        # Total realized = settlement P&L + SELL/TRIM/EXIT P&L
        total_realized = (daily_pnl_val or 0.0) + sum(
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
            strat_realized=d.get("strat_realized", {"A": 0.0, "B": 0.0, "C": 0.0, "D": 0.0}),
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
        strategy_summary_raw = _run_async(st.get_strategy_summary()) if hasattr(st, 'get_strategy_summary') else []
        strategy_summary = [s for s in strategy_summary_raw if s.get("strategy") in {"A", "B", "C", "D"}]
        strat_realized = _run_async(st.get_strategy_realized_pnl()) if hasattr(st, 'get_strategy_realized_pnl') else {"A": 0.0, "B": 0.0, "C": 0.0, "D": 0.0}
        # Include realized P&L from closed/settled positions (not just settlement table)
        for p in closed_pos:
            rpnl = p.get("realized_pnl")
            if rpnl is not None:
                s = p.get("strategy", "B")
                if s in strat_realized:
                    strat_realized[s] += rpnl

        # Get current prices for P&L calculation
        gamma_prices = reb.get_gamma_prices() if hasattr(reb, 'get_gamma_prices') else {}

        # Enrich positions with current price, unrealized P&L, and parsed slot info
        for p in open_pos:
            current = gamma_prices.get(p["token_id"])
            if current is not None:
                p["current_price"] = current
                p["unrealized_pnl"] = round((current - p["entry_price"]) * p["shares"], 4)
            else:
                p["current_price"] = None
                p["unrealized_pnl"] = None
            p["strategy"] = p.get("strategy", "B")
            p["buy_reason"] = p.get("buy_reason", "")
            p["slot_short"], p["market_date"] = _parse_slot_label(p.get("slot_label", ""))

        # Enrich closed positions with parsed slot info + remap legacy strategies
        for p in closed_pos:
            p["slot_short"], p["market_date"] = _parse_slot_label(p.get("slot_label", ""))
            p["buy_reason"] = p.get("buy_reason", "")
            p["exit_reason"] = p.get("exit_reason", "")
            s = p.get("strategy", "B")
            if s not in {"A", "B", "C", "D"}:
                p["strategy"] = "B"

        # Group by strategy — only A-D valid; legacy E/F remapped to B
        VALID_STRATS = ["A", "B", "C", "D"]
        strategies = {s: [] for s in VALID_STRATS}
        strat_pnl = {s: 0.0 for s in VALID_STRATS}  # unrealized
        strat_exposure = {s: 0.0 for s in VALID_STRATS}
        for p in open_pos:
            s = p.get("strategy", "B")
            if s not in strategies:
                s = "B"  # remap legacy strategies (E/F) to B
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
            entry = p["entry_price"]
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
                "entry": f"{p['entry_price']:.3f}",
                "exit": f"{p['exit_price']:.3f}" if p.get("exit_price") is not None else "-",
                "current": "-",
                "pnl": f"{'+'if p.get('realized_pnl',0)>0 else ''}${p['realized_pnl']:.3f}" if p.get("realized_pnl") is not None else "-",
                "reason": exit_reason,
                "type": "settled" if p.get("status") == "settled" else "closed",
                "sort_key": p.get("closed_at") or p.get("created_at", ""),
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

        # Per-strategy stats — only A-D valid; legacy E/F remapped to B
        VALID_STRATS = ["A", "B", "C", "D"]
        strat_pnl = {s: 0.0 for s in VALID_STRATS}
        strat_exposure = {s: 0.0 for s in VALID_STRATS}
        strat_counts = {s: {"open": 0, "settled": 0} for s in VALID_STRATS}
        for p in open_pos:
            s = p.get("strategy", "B")
            if s not in strat_pnl:
                s = "B"  # remap legacy
            strat_exposure[s] += p["size_usd"]
            strat_counts[s]["open"] += 1
            current = gamma_prices.get(p["token_id"])
            if current:
                strat_pnl[s] += (current - p["entry_price"]) * p["shares"]
        for p in closed_pos:
            s = p.get("strategy", "B")
            if s not in strat_counts:
                s = "B"
            if p.get("status") == "settled":
                strat_counts[s]["settled"] += 1
            if p.get("realized_pnl") is not None:
                strat_pnl[s] += p["realized_pnl"]

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

    @app.route("/temperatures")
    def temperatures_page():
        cfg = app.config["bot_config"]
        reb = app.config["bot_rebalancer"]
        state = reb.get_dashboard_state() if hasattr(reb, "get_dashboard_state") else {}

        obs_series = state.get("observation_series", {})
        forecasts = state.get("forecasts", {})
        daily_maxes = state.get("daily_maxes", {})
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
        )

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

    @app.route("/api/trigger", methods=["POST"])
    def api_trigger():
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
