# Local-native macOS deployment

The weather bot runs natively on macOS (no Docker) for live trading.  Two
phases: **setup** (one-time) and **run** (foreground or launchd).

## Prerequisites

- macOS, `python3.11` on `PATH` (`brew install python@3.11` if needed)
- `polymarket_trade_bot` already installed and its private key already
  stored in macOS Keychain at `service=polymarket-bot, account=private-key`.
  Both bots share that one entry — no need to re-import.
- A `.env` at the repo root.  Only `FUNDER_ADDRESS` is required for
  live trading; everything else (API creds, signing key) is auto-derived
  at startup.  Copy `.env.example` to `.env` and fill in `FUNDER_ADDRESS`:

  - **polymarket.com web user**: log in to polymarket.com, click your
    avatar → **Profile** → **Wallet**.  Copy the *"Polymarket address"*
    (a Gnosis Safe — that's where your deposited USDC lives).  Paste it
    after `FUNDER_ADDRESS=`.
  - **direct EOA setup**: leave `FUNDER_ADDRESS=0x` (the default
    placeholder) or empty.  ClobClient runs in EOA mode and signs as
    the wallet whose private key is in Keychain.

  See `.env.example` for the full list of optional fields
  (`TRIGGER_SECRET`, `ALERT_WEBHOOK_URL`, etc.).

## Setup (one-time)

```bash
cd ~/polymarket_weather_trade_bot
./scripts/setup_local.sh
```

The script:
1. Creates `.venv/` and installs the package (`pip install -e ".[dev]"`
   — pulls runtime deps from pyproject.toml plus pytest).
2. Loads the private key from Keychain — the **first** run pops a system
   dialog *"weather-bot wants to use the polymarket-bot keychain entry"*.
   Click **Always Allow** so re-runs and the launchd job stay silent.
3. Creates `data/`, `data/backups/`, `data/history/`, `logs/`.
4. `chmod 600 .env` — owner-read only.
5. Runs the full pytest suite (~2 min).  Setup fails loud if anything
   breaks — fix it before starting the bot.

## What gets auto-derived

You do **not** need to fill these into `.env`:

- `ETH_PRIVATE_KEY` — read from Keychain at startup.
- `POLYMARKET_API_KEY` / `SECRET` / `PASSPHRASE` — derived by
  `py-clob-client.create_or_derive_api_creds()` on first live request.
  Free, deterministic, signs once with the L1 key.  Pre-provisioning the
  three values still works (back-compat) and short-circuits the derive.

## Pre-live — USDC balance check

Before the first non-`--paper` run, confirm your funds are where the
bot can spend them.  The bot does not auto-fund anything; if balances
are zero the first BUY hits CLOB → comes back rejected → reconciler
flags the order failed → no position opens.  Same outcome as a
`--paper` run, but with operator confusion baked in.

Open polymarket.com → Profile → Wallet:

- **USDC ≥ your intended bankroll.**  At minimum the
  `daily_loss_limit_usd` (default $75) plus a buffer for open
  positions.  $200 USDC for the first live week is the recommended
  starting point.
- **Some MATIC** for transaction gas — usually a few cents' worth, and
  polymarket.com auto-funds new accounts for the first few trades.
  Top up via the deposit page if the dashboard's `gas_low` alert fires.

If `FUNDER_ADDRESS` is empty (direct EOA mode), the same check applies
to your EOA wallet on Polygon — view it on polygonscan.com.

## Run — foreground

```bash
./scripts/run_local.sh             # live (default — uses real USDC)
./scripts/run_local.sh --paper     # paper / sim, no orders
./scripts/run_local.sh --dry-run   # signals only, no positions recorded
```

stdout is teed to `logs/bot.log`.  `Ctrl-C` stops the bot.

## Run — launchd (auto-restart)

Drop a per-user LaunchAgent into `~/Library/LaunchAgents/`:

```bash
sed "s|__BOT_DIR__|$HOME/polymarket_weather_trade_bot|g" \
  launchd/com.user.weather-bot.plist.template \
  > ~/Library/LaunchAgents/com.user.weather-bot.plist
launchctl load -w ~/Library/LaunchAgents/com.user.weather-bot.plist
```

The job runs `scripts/run_local.sh` in **live** mode (no flag).  Edit
the plist's `ProgramArguments` if you want a different mode.  `KeepAlive`
restarts on crash; `RunAtLoad` starts on login.

Stop / unload:

```bash
launchctl unload ~/Library/LaunchAgents/com.user.weather-bot.plist
```

## Operations

| Task | Command |
|------|---------|
| Tail bot log | `tail -f logs/bot.log` |
| Tail launchd stdout/stderr | `tail -f logs/launchd.{out,err}` |
| Dashboard | open http://localhost:5001 |
| Pause entries (live) | `curl -X POST -H "X-Trigger-Secret: $TRIGGER_SECRET" http://localhost:5001/api/admin/pause` |
| Resume | `curl -X POST -H "X-Trigger-Secret: $TRIGGER_SECRET" http://localhost:5001/api/admin/resume` |
| Force a cycle | `curl -X POST -H "X-Trigger-Secret: $TRIGGER_SECRET" http://localhost:5001/api/trigger` |
| Stop a foreground run | Ctrl-C |
| Stop a launchd run | `launchctl unload ~/Library/LaunchAgents/com.user.weather-bot.plist` |
| Pull latest + restart | `git pull && launchctl kickstart -k gui/$(id -u)/com.user.weather-bot` |

The local dashboard listens on **5001** (the VPS paper deploy uses 5002 —
they don't collide).

## First live BUY — fee sanity check

The first time the bot opens a live position, capture the actual USDC
debit and compare it against the model's predicted fee:

```
Model fee = 0.05 × size_shares × price × (1 - price)
```

Find the order on Polymarket's UI or via Polygonscan, read the USDC
delta, and compare.  If the real fee is more than ~50% larger than the
model, **pause immediately**:

```bash
curl -X POST -H "X-Trigger-Secret: $TRIGGER_SECRET" \
  http://localhost:5001/api/admin/pause
```

The fee model is the only EV input that can't be unit-tested against
the real market — verifying it on the first fill is cheap and catches
a class of bugs that is otherwise invisible until daily P&L drifts.

## First live BUY — verify the debit comes from FUNDER, not EOA

The wrong signing path silently drains the wrong wallet.  After the
first fill, do a 30-second polygonscan sanity check.

1. Find your **EOA address** (the wallet that the Keychain private key
   corresponds to — *not* the Safe):

   ```bash
   .venv/bin/python -c "
   from eth_account import Account
   from src.security import load_eth_private_key
   print(Account.from_key(load_eth_private_key()).address)
   "
   ```

2. Open https://polygonscan.com/address/<EOA_ADDRESS> .  Expected
   activity: only a small **MATIC** outflow for gas.  **No USDC
   movement** — the EOA is just signing.

3. Open https://polygonscan.com/address/<FUNDER_ADDRESS> (the
   Safe address from `.env`).  Expected: a **USDC outflow equal to
   `size_usd + fee`**.  This is where the bot actually spent money.

If it's flipped — USDC drained from the EOA, FUNDER untouched — the
bot is signing as the EOA against a Safe that doesn't trust it.  Two
likely causes:

- `FUNDER_ADDRESS` in `.env` is wrong (typo or stale address)
- `signature_type` got picked wrong somehow (shouldn't happen since
  P-A10 derives it from `funder` presence — but verify)

Pause immediately and investigate:

```bash
curl -X POST -H "X-Trigger-Secret: $TRIGGER_SECRET" \
  http://localhost:5001/api/admin/pause
```

Then restart with a corrected `.env`.  Existing positions stay open;
only new BUYs are blocked while paused.

## Resetting

The bot keeps state in `data/bot.db`.  To wipe state and start over:

```bash
launchctl unload ~/Library/LaunchAgents/com.user.weather-bot.plist  # if loaded
mv data/bot.db data/backups/bot.$(date +%Y%m%d-%H%M%S).db
./scripts/run_local.sh                                              # recreates schema
```

Never delete `.env` or run anything that touches the Keychain entry — see
`/Users/marathon/polymarket_trade_bot` for re-importing the key.
