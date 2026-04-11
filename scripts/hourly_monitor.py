#!/usr/bin/env python3
"""Hourly strategy monitor — SSH into VPS, analyze bot performance, output insights.

Runs every hour via scheduled task. Connects once, collects all data, then
analyzes locally. Checks:
- Recent trades (entries, exits, locked wins)
- BUY->EXIT churn detection
- Per-strategy win rates and exposure
- Anomalies (errors, stale data, high drawdown)
- Actionable insights

Usage:
    .venv/bin/python scripts/hourly_monitor.py
"""
from __future__ import annotations

import base64
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

VPS_HOST = "root@198.23.134.31"
VPS_PASS = "81Hj6LRs5md2KXdu5Z"
REPORT_DIR = Path(__file__).resolve().parent.parent / "data" / "monitor_reports"

SSH_BASE = [
    "sshpass", "-p", VPS_PASS,
    "ssh", "-o", "StrictHostKeyChecking=no",
    "-o", "PreferredAuthentications=password",
    "-o", "ConnectTimeout=10",
    VPS_HOST,
]

# Python script to run on VPS — collects all data in one SSH call
REMOTE_COLLECTOR = r'''
import sqlite3, json, subprocess

DB = "/opt/weather-bot/data/bot.db"
result = {}

# API status
try:
    r = subprocess.run(["curl","-s","http://localhost:5001/api/status"],
                       capture_output=True, text=True, timeout=5)
    result["api"] = json.loads(r.stdout)
except Exception as e:
    result["api"] = {"error": str(e)}

# Database queries — each independent to avoid one failure blocking all
db = sqlite3.connect(DB)
db.row_factory = sqlite3.Row

def q(sql):
    return [dict(r) for r in db.execute(sql).fetchall()]

# Get actual column names
cols = [c[1] for c in db.execute("PRAGMA table_info(positions)").fetchall()]
has_pnl = "pnl" in cols

try:
    if has_pnl:
        result["positions_summary"] = q(
            "SELECT strategy, status, COUNT(*) as cnt, "
            "ROUND(SUM(size_usd),2) as total_size, "
            "ROUND(SUM(CASE WHEN pnl IS NOT NULL THEN pnl ELSE 0 END),2) as total_pnl "
            "FROM positions GROUP BY strategy, status ORDER BY strategy, status"
        )
    else:
        result["positions_summary"] = q(
            "SELECT strategy, status, COUNT(*) as cnt, "
            "ROUND(SUM(size_usd),2) as total_size, 0.0 as total_pnl "
            "FROM positions GROUP BY strategy, status ORDER BY strategy, status"
        )
except Exception as e:
    result["positions_summary"] = []
    result["err_summary"] = str(e)

try:
    result["recent_trades"] = q(
        "SELECT strategy, side, token_type, slot_label, entry_price, size_usd, "
        "created_at, buy_reason FROM positions "
        "WHERE created_at >= datetime('now','-2 hours') "
        "ORDER BY created_at DESC LIMIT 30"
    )
except Exception as e:
    result["recent_trades"] = []

try:
    result["churn"] = q(
        "SELECT token_id, strategy, COUNT(*) as cnt "
        "FROM positions WHERE created_at >= datetime('now','-6 hours') "
        "GROUP BY token_id, strategy HAVING cnt > 1 "
        "ORDER BY cnt DESC LIMIT 10"
    )
except Exception as e:
    result["churn"] = []

try:
    result["exits"] = q(
        "SELECT strategy, exit_reason, COUNT(*) as cnt "
        "FROM positions WHERE status IN ('exited','settled') "
        "AND exit_reason IS NOT NULL AND exit_reason != '' "
        "GROUP BY strategy, exit_reason ORDER BY cnt DESC LIMIT 15"
    )
except Exception as e:
    result["exits"] = []

try:
    if has_pnl:
        result["settlements"] = q(
            "SELECT strategy, COUNT(*) as total, "
            "SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as wins, "
            "SUM(CASE WHEN pnl <= 0 THEN 1 ELSE 0 END) as losses, "
            "ROUND(SUM(pnl),2) as net_pnl "
            "FROM positions WHERE status='settled' "
            "GROUP BY strategy ORDER BY strategy"
        )
    else:
        result["settlements"] = q(
            "SELECT strategy, COUNT(*) as total, 0 as wins, 0 as losses, 0.0 as net_pnl "
            "FROM positions WHERE status='settled' "
            "GROUP BY strategy ORDER BY strategy"
        )
except Exception as e:
    result["settlements"] = []

try:
    result["city_exposure"] = q(
        "SELECT city, COUNT(*) as positions, "
        "ROUND(SUM(size_usd),2) as exposure "
        "FROM positions WHERE status='open' "
        "GROUP BY city ORDER BY exposure DESC LIMIT 15"
    )
except Exception as e:
    result["city_exposure"] = []

try:
    result["price_dist"] = q(
        "SELECT CASE "
        "  WHEN entry_price < 0.50 THEN 'a_<0.50' "
        "  WHEN entry_price < 0.60 THEN 'b_0.50-0.60' "
        "  WHEN entry_price < 0.70 THEN 'c_0.60-0.70' "
        "  WHEN entry_price < 0.80 THEN 'd_0.70-0.80' "
        "  ELSE 'e_0.80+' END as price_range, "
        "COUNT(*) as cnt FROM positions "
        "WHERE side='BUY' AND token_type='NO' "
        "GROUP BY price_range ORDER BY price_range"
    )
except Exception as e:
    result["price_dist"] = []

try:
    result["total_positions"] = q("SELECT COUNT(*) as n FROM positions")[0]["n"]
except Exception as e:
    result["total_positions"] = 0

try:
    result["orders_count"] = q("SELECT COUNT(*) as n FROM orders")[0]["n"]
except Exception:
    result["orders_count"] = 0

# Detect locked-win positions that were trimmed/exited (critical bug check)
try:
    result["locked_trimmed"] = q(
        "SELECT id, strategy, city, slot_label, entry_price, size_usd, buy_reason, exit_reason "
        "FROM positions "
        "WHERE buy_reason LIKE '%LOCKED WIN%' AND status IN ('closed','exited') "
        "AND exit_reason != '' "
        "ORDER BY id"
    )
except Exception:
    result["locked_trimmed"] = []

# Detect expensive locked-win entries (price > 0.90)
try:
    result["expensive_locked"] = q(
        "SELECT id, strategy, city, slot_label, entry_price, buy_reason "
        "FROM positions "
        "WHERE buy_reason LIKE '%LOCKED WIN%' AND entry_price > 0.90 "
        "ORDER BY id"
    )
except Exception:
    result["expensive_locked"] = []

db.close()

# Docker logs (last 15 lines)
try:
    r = subprocess.run(
        ["docker","compose","-f","/opt/weather-bot/docker-compose.yml","logs","--tail=15"],
        capture_output=True, text=True, timeout=10
    )
    result["docker_logs"] = r.stdout + r.stderr
except Exception as e:
    result["docker_logs"] = str(e)

print(json.dumps(result, default=str))
'''


def collect_data() -> dict:
    """SSH into VPS, run collector script, return all data as dict."""
    # Base64 encode the script to avoid any quoting issues
    encoded = base64.b64encode(REMOTE_COLLECTOR.encode()).decode()
    cmd = f"python3 -c \"import base64; exec(base64.b64decode('{encoded}').decode())\""

    try:
        result = subprocess.run(
            SSH_BASE + [cmd],
            capture_output=True, text=True, timeout=45,
        )
        if result.returncode != 0:
            return {"error": f"SSH failed: {result.stderr.strip()}"}
        return json.loads(result.stdout.strip())
    except subprocess.TimeoutExpired:
        return {"error": "SSH timed out (45s)"}
    except json.JSONDecodeError as e:
        return {"error": f"JSON parse failed: {e}"}


def build_report(data: dict) -> str:
    """Analyze collected data and build report."""
    lines: list[str] = []
    now = datetime.now(timezone.utc)
    lines.append(f"{'='*70}")
    lines.append(f"  HOURLY MONITOR REPORT — {now.strftime('%Y-%m-%d %H:%M UTC')}")
    lines.append(f"{'='*70}")

    if "error" in data:
        lines.append(f"\n  [CRITICAL] Data collection failed: {data['error']}")
        return "\n".join(lines)

    # ── 1. API Status ──
    status = data.get("api", {})
    lines.append(f"\n--- System Status ---")
    if "error" in status:
        lines.append(f"  [CRITICAL] API unreachable: {status['error']}")
    else:
        lines.append(f"  Mode:          {status.get('mode', '?')}")
        lines.append(f"  Last run:      {status.get('last_run', '?')}")
        lines.append(f"  Active events: {status.get('active_events', 0)}")
        lines.append(f"  Exposure:      ${status.get('exposure', 0):.2f}")
        lines.append(f"  Signals:       {status.get('signal_count', 0)}")
        lines.append(f"  Last error:    {status.get('last_error') or 'None'}")

        # Staleness check
        last_run = status.get("last_run", "")
        if last_run:
            try:
                lr = datetime.strptime(last_run, "%Y-%m-%d %H:%M:%S %Z").replace(tzinfo=timezone.utc)
                age_min = (now - lr).total_seconds() / 60
                if age_min > 75:
                    lines.append(f"  [WARNING] Last run {age_min:.0f} min ago — scheduler may be stalled!")
            except ValueError:
                pass

    # ── 2. Position Summary ──
    rows = data.get("positions_summary", [])
    total_pos = data.get("total_positions", 0)
    lines.append(f"\n--- Positions by Strategy (total: {total_pos}) ---")
    if rows:
        lines.append(f"  {'Strat':<6} {'Status':<12} {'Count':>6} {'Size($)':>10} {'P&L($)':>10}")
        for r in rows:
            lines.append(
                f"  {r['strategy']:<6} {r['status']:<12} {r['cnt']:>6} "
                f"  ${r['total_size']:>8}   ${r['total_pnl']:>8}"
            )
    else:
        lines.append("  No positions yet.")

    # ── 3. Recent Trades (last 2 hours) ──
    recent = data.get("recent_trades", [])
    lines.append(f"\n--- Recent Trades (last 2h) ---")
    if recent:
        for r in recent:
            reason = (r.get('buy_reason') or '')[:35]
            label = (r.get('slot_label') or '')[:42]
            lines.append(
                f"  {r.get('strategy','?'):>2} {r.get('side','?'):<5} "
                f"NO@{r.get('entry_price',0):.3f} ${r.get('size_usd',0):<6.2f} "
                f"{label}  [{reason}]"
            )
        lines.append(f"  ({len(recent)} trades)")
    else:
        lines.append("  No trades in last 2 hours.")

    # ── 4. Churn Detection ──
    churn = data.get("churn", [])
    lines.append(f"\n--- Churn Detection (same token >1x in 6h) ---")
    if churn:
        lines.append(f"  [WARNING] BUY->EXIT->BUY churn detected!")
        for r in churn:
            tid = str(r.get('token_id', '?'))
            lines.append(f"  Strategy {r['strategy']}: token {tid[:20]}... x{r['cnt']}")
    else:
        lines.append("  No churn detected. (Good)")

    # ── 5. Exit Analysis ──
    exits = data.get("exits", [])
    lines.append(f"\n--- Exit Reasons ---")
    if exits:
        for r in exits:
            lines.append(f"  {r.get('strategy','?'):>2}  {(r.get('exit_reason') or '?'):<35} x{r['cnt']}")
    else:
        lines.append("  No exits yet.")

    # ── 6. Settlement Results ──
    settlements = data.get("settlements", [])
    lines.append(f"\n--- Settlement Results ---")
    if settlements:
        lines.append(f"  {'Strat':<6} {'Total':>6} {'Wins':>6} {'Loss':>6} {'WinRate':>8} {'Net P&L':>10}")
        for r in settlements:
            total = r['total']
            wins = r.get('wins') or 0
            wr = wins / total * 100 if total > 0 else 0
            lines.append(
                f"  {r['strategy']:<6} {total:>6} {wins:>6} {r.get('losses',0) or 0:>6} "
                f"{wr:>7.1f}%   ${r.get('net_pnl',0):>8}"
            )
    else:
        lines.append("  No settlements yet — too early to evaluate win rates.")

    # ── 7. Per-City Exposure ──
    city_exp = data.get("city_exposure", [])
    lines.append(f"\n--- Open Exposure by City ---")
    if city_exp:
        for r in city_exp:
            lines.append(f"  {r['city']:<18} {r['positions']:>3} pos   ${r['exposure']:>8}")
    else:
        lines.append("  No open positions.")

    # ── 8. Price Distribution ──
    prices = data.get("price_dist", [])
    lines.append(f"\n--- NO Entry Price Distribution ---")
    if prices:
        for r in prices:
            label = r['price_range'][2:]  # strip sort prefix
            lines.append(f"  {label:<12} {r['cnt']:>5} trades")
    else:
        lines.append("  No buy trades yet.")

    # ── 9. Locked-Win Integrity Check ──
    locked_trimmed = data.get("locked_trimmed", [])
    expensive_locked = data.get("expensive_locked", [])
    lines.append(f"\n--- Locked-Win Integrity ---")
    if locked_trimmed:
        lines.append(f"  [CRITICAL] {len(locked_trimmed)} locked-win positions were SOLD:")
        for r in locked_trimmed:
            lines.append(
                f"    #{r['id']} {r['strategy']} {r['city']} @{r['entry_price']:.3f} "
                f"→ {(r.get('exit_reason') or '?')[:40]}"
            )
    else:
        lines.append("  No locked wins improperly exited. (Good)")
    if expensive_locked:
        lines.append(f"  [WARNING] {len(expensive_locked)} locked wins at price >$0.90 (thin margin):")
        for r in expensive_locked:
            lines.append(f"    #{r['id']} {r['strategy']} {r['city']} @{r['entry_price']:.3f}")

    # ── 10. Docker Health ──
    logs = data.get("docker_logs", "")
    error_lines = [
        l for l in logs.split('\n')
        if any(k in l.lower() for k in ['error', 'exception', 'traceback', 'critical'])
        and 'last_error' not in l.lower()
        and l.strip()
    ]
    lines.append(f"\n--- Docker Health ---")
    if error_lines:
        lines.append(f"  [WARNING] {len(error_lines)} error lines in recent logs:")
        for el in error_lines[-5:]:
            lines.append(f"    {el.strip()[:100]}")
    else:
        lines.append("  No errors in recent logs. (Good)")

    # ── 10. Insights ──
    lines.append(f"\n{'='*70}")
    lines.append(f"  INSIGHTS & RECOMMENDATIONS")
    lines.append(f"{'='*70}")

    insights = []
    exposure = status.get('exposure', 0) if isinstance(status, dict) else 0

    # Insight: exposure level
    if exposure == 0:
        insights.append("[INFO] Zero exposure — bot may not have found opportunities yet or all settled.")
    elif exposure > 500:
        insights.append(f"[WATCH] High total exposure ${exposure:.0f} — approaching risk limits.")
    elif exposure > 0:
        insights.append(f"[GOOD] Active exposure ${exposure:.2f} — bot is trading normally.")

    # Insight: churn
    if churn:
        insights.append("[BAD] Churn detected — exit thresholds may still be too aggressive. "
                        "Check evaluator.py exit_distance multipliers.")
    else:
        insights.append("[GOOD] No BUY->EXIT churn — exit logic fix is working.")

    # Insight: locked-win integrity
    if locked_trimmed:
        insights.append(
            f"[CRITICAL] {len(locked_trimmed)} locked-win positions were improperly exited! "
            f"TRIM/EXIT should NEVER close guaranteed winners."
        )
    if expensive_locked:
        insights.append(
            f"[WARNING] {len(expensive_locked)} locked wins entered at price >$0.90 — "
            f"margin too thin after fees."
        )

    # Insight: strategy differentiation
    strat_open = {}
    for r in rows:
        if r.get('status') == 'open':
            strat_open[r['strategy']] = r['cnt']
    if len(strat_open) > 1:
        most = max(strat_open, key=strat_open.get)
        least = min(strat_open, key=strat_open.get)
        if strat_open[most] != strat_open[least]:
            insights.append(
                f"[INFO] Strategy {most} ({strat_open[most]} pos) vs {least} ({strat_open[least]} pos) "
                f"— variants are differentiating."
            )
        else:
            insights.append(
                f"[INFO] All strategies have {strat_open[most]} positions — differentiation "
                f"will show at exit/settlement."
            )

    # Insight: win rate
    if settlements:
        total_wins = sum(r.get('wins') or 0 for r in settlements)
        total_all = sum(r['total'] for r in settlements)
        if total_all >= 10:
            wr = total_wins / total_all * 100
            if wr >= 95:
                insights.append(f"[GOOD] Overall win rate {wr:.1f}% — strong edge holding up.")
            elif wr >= 85:
                insights.append(f"[OK] Win rate {wr:.1f}% — decent but watch for deterioration.")
            else:
                insights.append(f"[BAD] Win rate only {wr:.1f}% — consider tightening distance/EV.")

    # Insight: price discipline
    if prices:
        cheap = sum(r['cnt'] for r in prices if r['price_range'] in ('a_<0.50', 'b_0.50-0.60'))
        expensive = sum(r['cnt'] for r in prices if r['price_range'] in ('d_0.70-0.80', 'e_0.80+'))
        total_t = sum(r['cnt'] for r in prices)
        if total_t > 5:
            cheap_pct = cheap / total_t * 100
            if cheap_pct > 50:
                insights.append(f"[GOOD] {cheap_pct:.0f}% of entries at <$0.60 — good price discipline.")
            elif expensive > cheap:
                insights.append(f"[WATCH] More expensive entries ({expensive}) than cheap ({cheap}) "
                                f"— consider lowering max_no_price.")

    # Insight: locked wins
    locked_count = sum(1 for r in recent if 'LOCKED' in (r.get('buy_reason') or '').upper())
    if locked_count > 0:
        insights.append(f"[GOOD] {locked_count} locked-win entries in last 2h — capturing guaranteed profits.")

    # Insight: no recent activity
    if not recent:
        insights.append("[INFO] No trades in last 2h — may be off-hours or no qualifying opportunities.")

    # Insight: errors
    if isinstance(status, dict) and status.get('last_error'):
        insights.append(f"[BAD] Last error: {status['last_error'][:80]}")
    if error_lines:
        insights.append(f"[WATCH] {len(error_lines)} errors in Docker logs — review for recurring issues.")

    # Insight: trends
    trends = status.get('trends', {}) if isinstance(status, dict) else {}
    breakouts = [c for c, t in trends.items() if 'BREAKOUT' in str(t)]
    settling = [c for c, t in trends.items() if 'SETTLING' in str(t)]
    if breakouts:
        insights.append(f"[INFO] Breakout in: {', '.join(breakouts)} — tighter exits, boosted opposite side.")
    if settling:
        insights.append(f"[INFO] Settling in: {', '.join(settling)} — higher EV threshold active.")

    # Insight: city concentration
    if city_exp:
        top_city = city_exp[0]
        if top_city['exposure'] > 30 and len(city_exp) > 1:
            pct = top_city['exposure'] / sum(c['exposure'] for c in city_exp) * 100
            if pct > 40:
                insights.append(
                    f"[WATCH] {top_city['city']} has {pct:.0f}% of total exposure "
                    f"(${top_city['exposure']:.0f}) — high city concentration."
                )

    if not insights:
        insights.append("[GOOD] Everything looks normal. Strategy running as expected.")

    for insight in insights:
        lines.append(f"  {insight}")

    lines.append(f"\n{'='*70}\n")
    return "\n".join(lines)


def main():
    data = collect_data()
    report = build_report(data)
    print(report)

    # Save report
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M")
    report_file = REPORT_DIR / f"monitor_{ts}.txt"
    report_file.write_text(report)
    (REPORT_DIR / "latest.txt").write_text(report)
    print(f"Report saved: {report_file}")


if __name__ == "__main__":
    main()
