#!/bin/bash
# Full database reset + latest code deployment
# Usage: SSH to VPS, then run: bash scripts/full_reset_and_deploy.sh
#
# - Backs up the database with timestamp
# - Clears ALL 6 trading tables (positions, orders, daily_pnl, settlements, decision_log, edge_history)
# - Drops leftover old_strategy_summary table
# - Pulls latest code and rebuilds Docker

set -e

# FIX-07: default to the active deploy path but allow override. The old
# /opt/weather-bot dir still exists on the VPS as a staging area — accidentally
# blasting it would be catastrophic, so require the compose file to be present.
BOT_DIR="${BOT_DIR:-/opt/weather-bot-new}"
DB_PATH="$BOT_DIR/data/bot.db"
BACKUP_DIR="$BOT_DIR/data/backups"

[ -f "$BOT_DIR/docker-compose.yml" ] || { echo "BOT_DIR invalid: no docker-compose.yml at $BOT_DIR"; exit 1; }

# FIX-15: lock down .env to rw------- so the private key and API creds
# aren't world-readable.  Noop when .env doesn't exist (fresh install
# will place it later).
if [ -f "$BOT_DIR/.env" ]; then
    chmod 600 "$BOT_DIR/.env"
    echo "  chmod 600 applied to $BOT_DIR/.env"
fi

cd "$BOT_DIR"

# 1. Stop the bot
echo "=== Step 1: Stopping bot ==="
docker compose stop

# 2. Backup database
echo ""
echo "=== Step 2: Backing up database ==="
mkdir -p "$BACKUP_DIR"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
BACKUP_FILE="$BACKUP_DIR/bot_full_reset_${TIMESTAMP}.db"
cp "$DB_PATH" "$BACKUP_FILE"
echo "Backup saved: $BACKUP_FILE"

# 3. Full database reset
echo ""
echo "=== Step 3: Resetting database (all 6 tables) ==="
python3 -c "
import sqlite3

db = sqlite3.connect('$DB_PATH')
c = db.cursor()

# Show before counts
print('  Before:')
for t in ['positions','orders','daily_pnl','settlements','decision_log','edge_history']:
    c.execute(f'SELECT COUNT(*) FROM {t}')
    print(f'    {t}: {c.fetchone()[0]} rows')

# Clear all 6 data tables
for t in ['positions','orders','daily_pnl','settlements','decision_log','edge_history']:
    db.execute(f'DELETE FROM {t}')

# Drop old_strategy_summary if it exists
c.execute(\"SELECT name FROM sqlite_master WHERE type='table' AND name='old_strategy_summary'\")
if c.fetchone():
    db.execute('DROP TABLE old_strategy_summary')
    print('  Dropped old_strategy_summary table')

# Reset autoincrement counters
db.execute(\"DELETE FROM sqlite_sequence WHERE name IN ('positions','orders','settlements','decision_log','edge_history')\")

db.commit()

# Verify all empty
print('  After:')
for t in ['positions','orders','daily_pnl','settlements','decision_log','edge_history']:
    c.execute(f'SELECT COUNT(*) FROM {t}')
    count = c.fetchone()[0]
    assert count == 0, f'{t} not empty!'
    print(f'    {t}: 0 rows')

# Compact
db.execute('VACUUM')
db.commit()
db.close()
print('  Database reset and vacuumed OK.')
"

# 4. Clear monitor log
echo ""
echo "=== Step 4: Clearing monitor log ==="
> "$BOT_DIR/data/monitor_log.txt" 2>/dev/null || true
echo "  monitor_log.txt cleared"

# 5. Pull latest code
echo ""
echo "=== Step 5: Pulling latest code ==="
git pull

# 6. Rebuild and start
echo ""
echo "=== Step 6: Rebuilding and starting bot ==="
docker compose up -d --build

# 7. Verify
echo ""
echo "=== Step 7: Verifying (waiting 10s for startup) ==="
sleep 10

echo "Container status:"
docker compose ps

echo ""
echo "API status:"
curl -s http://localhost:5001/api/status | python3 -m json.tool 2>/dev/null || echo "  (API not ready yet, check logs)"

echo ""
echo "Recent logs:"
docker compose logs --tail=15

echo ""
echo "=========================================="
echo "  Done! Database fully reset."
echo "  New strategy running from clean state."
echo "  Backup: $BACKUP_FILE"
echo "=========================================="
