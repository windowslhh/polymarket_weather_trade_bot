#!/usr/bin/env bash
# Foreground launcher for the weather bot.  Activates the venv, tees stdout
# to logs/bot.log, and forwards any flags to ``python -m src.main`` so you
# can run live (no flag), paper (--paper), or dry-run (--dry-run).
#
#   ./scripts/run_local.sh
#   ./scripts/run_local.sh --paper
#   ./scripts/run_local.sh --dry-run --verbose
set -euo pipefail

cd "$(dirname "$0")/.."

if [ ! -d .venv ]; then
  echo "ERROR: .venv missing — run ./scripts/setup_local.sh first" >&2
  exit 1
fi
# shellcheck disable=SC1091
source .venv/bin/activate

mkdir -p logs
# -u disables stdout/stderr buffering so log lines hit the file (and any
# tail / launchd reader) immediately instead of in 4 KB chunks every few
# minutes.  Block buffering is the default once stdout is a pipe (tee /
# launchd) and would otherwise hide the bot's progress for long stretches.
exec python -u -m src.main "$@" 2>&1 | tee -a logs/bot.log
