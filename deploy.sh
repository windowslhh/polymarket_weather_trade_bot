#!/usr/bin/env bash
set -euo pipefail

# ============================================================
# Polymarket Weather Bot — VPS Deployment Script
# Usage: ./deploy.sh <VPS_IP> [SSH_USER]
# Example: ./deploy.sh 123.45.67.89 root
# ============================================================

VPS_IP="${1:?Usage: ./deploy.sh <VPS_IP> [SSH_USER]}"
SSH_USER="${2:-root}"
REMOTE_DIR="/opt/weather-bot"
REPO_URL="git@github.com:windowslhh/polymarket_weather_trade_bot.git"

echo "=== Deploying Weather Bot to ${SSH_USER}@${VPS_IP} ==="

# 1. Install Docker on VPS if not present
echo "[1/5] Ensuring Docker is installed..."
ssh "${SSH_USER}@${VPS_IP}" 'command -v docker &>/dev/null || {
  apt-get update && apt-get install -y docker.io docker-compose-plugin
  systemctl enable --now docker
}'

# 2. Clone or pull repo
echo "[2/5] Syncing code..."
ssh "${SSH_USER}@${VPS_IP}" "
  if [ -d ${REMOTE_DIR} ]; then
    cd ${REMOTE_DIR} && git pull
  else
    git clone ${REPO_URL} ${REMOTE_DIR}
  fi
"

# 3. Copy .env file
echo "[3/5] Uploading .env..."
if [ -f .env ]; then
  scp .env "${SSH_USER}@${VPS_IP}:${REMOTE_DIR}/.env"
else
  echo "WARNING: No local .env file found. Create one on the VPS:"
  echo "  ssh ${SSH_USER}@${VPS_IP} 'cp ${REMOTE_DIR}/.env.example ${REMOTE_DIR}/.env && nano ${REMOTE_DIR}/.env'"
fi

# 4. Build and start
echo "[4/5] Building and starting containers..."
ssh "${SSH_USER}@${VPS_IP}" "
  cd ${REMOTE_DIR}
  docker compose up -d --build
"

# 5. Verify
echo "[5/5] Verifying deployment..."
sleep 5
ssh "${SSH_USER}@${VPS_IP}" "docker compose -f ${REMOTE_DIR}/docker-compose.yml logs --tail=20"

echo ""
echo "=== Deployment Complete ==="
echo "Dashboard: http://${VPS_IP}:5001"
echo ""
echo "Useful commands:"
echo "  Logs:    ssh ${SSH_USER}@${VPS_IP} 'cd ${REMOTE_DIR} && docker compose logs -f'"
echo "  Stop:    ssh ${SSH_USER}@${VPS_IP} 'cd ${REMOTE_DIR} && docker compose down'"
echo "  Restart: ssh ${SSH_USER}@${VPS_IP} 'cd ${REMOTE_DIR} && docker compose restart'"
echo "  Shell:   ssh ${SSH_USER}@${VPS_IP} 'docker exec -it weather-bot bash'"
