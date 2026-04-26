# Local-native macOS deployment

The weather bot runs natively on macOS (no Docker) for live trading.  Two
phases: **setup** (one-time) and **run** (foreground or launchd).

## Prerequisites

- macOS, `python3.11` on `PATH` (`brew install python@3.11` if needed)
- `polymarket_trade_bot` already installed and its private key already
  stored in macOS Keychain at `service=polymarket-bot, account=private-key`.
  Both bots share that one entry — no need to re-import.
- A `.env` at the repo root with the CLOB API credentials filled in
  (POLYMARKET_API_KEY / SECRET / PASSPHRASE).  `.env.example` lists every
  variable the bot reads.

## Setup (one-time)

```bash
cd ~/polymarket_weather_trade_bot
./scripts/setup_local.sh
```

The script:
1. Creates `.venv/` with python3.11 and installs `requirements.txt`.
2. Loads the private key from Keychain — the **first** run pops a system
   dialog *"weather-bot wants to use the polymarket-bot keychain entry"*.
   Click **Always Allow** so re-runs and the launchd job stay silent.
3. Creates `data/`, `data/backups/`, `data/history/`, `logs/`.
4. `chmod 600 .env` — owner-read only.
5. Runs the full pytest suite (~2 min).  Setup fails loud if anything
   breaks — fix it before starting the bot.

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

## Resetting

The bot keeps state in `data/bot.db`.  To wipe state and start over:

```bash
launchctl unload ~/Library/LaunchAgents/com.user.weather-bot.plist  # if loaded
mv data/bot.db data/backups/bot.$(date +%Y%m%d-%H%M%S).db
./scripts/run_local.sh                                              # recreates schema
```

Never delete `.env` or run anything that touches the Keychain entry — see
`/Users/marathon/polymarket_trade_bot` for re-importing the key.
