#!/usr/bin/env python3
"""Collect raw trading data from VPS — output JSON for Claude analysis.

This script ONLY collects data, no analysis. Claude reads the output
and applies reasoning to detect issues, patterns, and opportunities.

Usage:
    .venv/bin/python scripts/collect_vps_data.py
"""
from __future__ import annotations

import base64
import json
import subprocess
import sys
from datetime import datetime, timezone

VPS_HOST = "root@198.23.134.31"
VPS_PASS = "81Hj6LRs5md2KXdu5Z"

SSH_BASE = [
    "sshpass", "-p", VPS_PASS,
    "ssh", "-o", "StrictHostKeyChecking=no",
    "-o", "PreferredAuthentications=password",
    "-o", "ConnectTimeout=10",
    VPS_HOST,
]

REMOTE_COLLECTOR = r'''
import sqlite3, json, subprocess

DB = "/opt/weather-bot/data/bot.db"
result = {}

# 1. API status
try:
    r = subprocess.run(["curl","-s","http://localhost:5001/api/status"],
                       capture_output=True, text=True, timeout=5)
    result["api_status"] = json.loads(r.stdout)
except Exception as e:
    result["api_status"] = {"error": str(e)}

# 2. Full database dump
db = sqlite3.connect(DB)
db.row_factory = sqlite3.Row

def q(sql):
    try:
        return [dict(r) for r in db.execute(sql).fetchall()]
    except Exception as e:
        return [{"_error": str(e)}]

# All positions with full fields
result["all_positions"] = q(
    "SELECT id, event_id, token_id, strategy, status, city, slot_label, side, token_type, "
    "entry_price, exit_price, size_usd, shares, realized_pnl, buy_reason, exit_reason, "
    "created_at, closed_at FROM positions ORDER BY id"
)

# Recent orders
result["recent_orders"] = q(
    "SELECT * FROM orders ORDER BY id DESC LIMIT 30"
)

# Decision log (last 50 entries)
result["decision_log"] = q(
    "SELECT * FROM decision_log ORDER BY id DESC LIMIT 50"
)

# Settlements
result["settlements"] = q(
    "SELECT * FROM settlements ORDER BY id DESC LIMIT 20"
)

# Daily P&L
result["daily_pnl"] = q(
    "SELECT * FROM daily_pnl ORDER BY date DESC LIMIT 7"
)

db.close()

# 3. Docker logs (last 30 lines)
try:
    r = subprocess.run(
        ["docker","compose","-f","/opt/weather-bot/docker-compose.yml","logs","--tail=30"],
        capture_output=True, text=True, timeout=10
    )
    result["docker_logs"] = (r.stdout + r.stderr)[-3000:]  # cap size
except Exception as e:
    result["docker_logs"] = str(e)

# 4. Container uptime
try:
    r = subprocess.run(
        ["docker","compose","-f","/opt/weather-bot/docker-compose.yml","ps","--format","json"],
        capture_output=True, text=True, timeout=5
    )
    result["container_info"] = r.stdout.strip()
except Exception:
    result["container_info"] = ""

print(json.dumps(result, default=str))
'''


def main():
    encoded = base64.b64encode(REMOTE_COLLECTOR.encode()).decode()
    cmd = f"python3 -c \"import base64; exec(base64.b64decode('{encoded}').decode())\""

    try:
        result = subprocess.run(
            SSH_BASE + [cmd],
            capture_output=True, text=True, timeout=60,
        )
        if result.returncode != 0:
            print(json.dumps({"error": f"SSH failed: {result.stderr.strip()}"}))
            sys.exit(1)
        # Validate JSON and print
        data = json.loads(result.stdout.strip())
        data["_collected_at"] = datetime.now(timezone.utc).isoformat()
        print(json.dumps(data, indent=2, default=str))
    except subprocess.TimeoutExpired:
        print(json.dumps({"error": "SSH timed out (60s)"}))
        sys.exit(1)
    except json.JSONDecodeError as e:
        print(json.dumps({"error": f"JSON parse failed: {e}", "raw": result.stdout[:500]}))
        sys.exit(1)


if __name__ == "__main__":
    main()
