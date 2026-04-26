#!/usr/bin/env bash
# Local-native setup for weather bot (macOS).  Run from the repo root.
#
#   ./scripts/setup_local.sh
#
# Steps: create .venv, install deps, prove the macOS Keychain entry resolves,
# create runtime directories, lock down .env, run pytest.  Idempotent — safe
# to re-run after pulling.
set -euo pipefail

cd "$(dirname "$0")/.."
ROOT="$(pwd)"
echo "Setup target: ${ROOT}"

# 1. venv
if [ ! -d .venv ]; then
  echo "→ creating .venv with python3.11"
  python3.11 -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate
python -m pip install --upgrade pip --quiet
pip install -r requirements.txt --quiet

# 2. Verify macOS Keychain → fingerprint of the private key.
#    The first run pops a system dialog ("Allow access to 'polymarket-bot'?")
#    — click Always Allow once and subsequent runs are silent.
echo "→ checking macOS Keychain (service=polymarket-bot, account=private-key)"
python -c "
from src.security import _fingerprint, load_eth_private_key
key = load_eth_private_key()
print(f'   Keychain OK — fingerprint {_fingerprint(key)}')
"

# 3. Runtime directories
mkdir -p data/backups data/history logs
echo "→ data/, data/backups/, data/history/, logs/ ready"

# 4. .env permissions — keep secrets owner-only.  Refuse to continue
#    without a .env so we don't accidentally start with empty CLOB creds.
if [ ! -f .env ]; then
  echo "ERROR: .env is missing.  Copy .env.example to .env and fill in"
  echo "       POLYMARKET_API_KEY / SECRET / PASSPHRASE before re-running."
  exit 1
fi
chmod 600 .env
echo "→ .env permissions set to 600"

# 5. Smoke test — full pytest, ignoring the offline backtest scripts.
echo "→ running pytest (this takes ~2 min)"
python -m pytest tests/ \
  --ignore=tests/dry_run_offline.py \
  --ignore=tests/run_backtest_offline.py \
  -q

echo
echo "Setup complete."
echo "Next: ./scripts/run_local.sh             (live trading)"
echo "      ./scripts/run_local.sh --paper     (paper / sim)"
echo "      ./scripts/run_local.sh --dry-run   (signals only, no positions)"
