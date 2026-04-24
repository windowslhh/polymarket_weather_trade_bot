#!/bin/bash
# Backup old database and reset for new strategy run
# Usage: ssh to VPS, then run this script

set -e

# FIX-07: see full_reset_and_deploy.sh for rationale — must match the active
# deploy dir, overridable for dev, and guarded against wrong-dir runs.
BOT_DIR="${BOT_DIR:-/opt/weather-bot-new}"
DB_PATH="$BOT_DIR/data/bot.db"
BACKUP_DIR="$BOT_DIR/data/backups"

[ -f "$BOT_DIR/docker-compose.yml" ] || { echo "BOT_DIR invalid: no docker-compose.yml at $BOT_DIR"; exit 1; }

# 1. Stop the bot
echo "Stopping bot..."
cd "$BOT_DIR"
docker compose stop

# 2. Create backup directory
mkdir -p "$BACKUP_DIR"

# 3. Backup current database with timestamp
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
BACKUP_FILE="$BACKUP_DIR/bot_old_strategy_${TIMESTAMP}.db"
cp "$DB_PATH" "$BACKUP_FILE"
echo "Old database backed up to: $BACKUP_FILE"

# 4. Reset database using Python (sqlite3 CLI not available on VPS)
python3 -c "
import sqlite3
db = sqlite3.connect('$DB_PATH')

# Save settlement summary before clearing
db.execute('''CREATE TABLE IF NOT EXISTS old_strategy_summary AS
SELECT
    'old_strategy' as run_label,
    strategy,
    COUNT(*) as total_positions,
    SUM(CASE WHEN status = 'open' THEN 1 ELSE 0 END) as open_positions,
    SUM(CASE WHEN status = 'closed' THEN 1 ELSE 0 END) as closed_positions,
    SUM(CASE WHEN status = 'settled' THEN 1 ELSE 0 END) as settled_positions,
    ROUND(SUM(size_usd), 2) as total_invested,
    MIN(created_at) as first_trade,
    MAX(created_at) as last_trade
FROM positions
GROUP BY strategy''')

# Clear trading data for fresh start
for t in ['positions', 'orders', 'daily_pnl', 'settlements', 'decision_log']:
    db.execute(f'DELETE FROM {t}')

# Reset auto-increment counters
db.execute(\"DELETE FROM sqlite_sequence WHERE name IN ('positions','orders','settlements','decision_log')\")

db.commit()
db.close()
print('Database reset OK')
"

echo "Database reset complete. Old strategy summary saved in old_strategy_summary table."

# 5. Restart bot with new strategy
docker compose up -d --build
echo "Bot restarted with new strategy."

# 6. Wait and verify
sleep 10
echo "Verifying..."
curl -s http://localhost:5001/api/status | python3 -m json.tool

echo ""
echo "Done! New strategy is running clean."
echo "Old data backup: $BACKUP_FILE"
