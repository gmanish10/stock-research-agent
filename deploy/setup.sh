#!/usr/bin/env bash
#
# Provision a fresh Ubuntu 24.04 host for the stock-research Telegram bot.
# Run as a sudo-capable NON-root user (e.g. `bot`):
#
#     REPO_URL=git@github.com:<you>/stock-research-agent.git bash deploy/setup.sh
#
# Idempotent — safe to re-run (e.g. after `git pull`). It installs system deps, Ollama +
# llama3.2 (the helper LLM), the Python venv, and the systemd service. It does NOT create
# .env — scp that from your Mac (see DEPLOY.md), then start the service.
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/stock-research-agent}"
REPO_URL="${REPO_URL:-}"          # required only for the very first clone
SERVICE_USER="$(id -un)"

echo "==> [1/6] apt base packages"
sudo apt-get update -y
sudo apt-get -y upgrade
sudo apt-get install -y python3-venv python3-dev python3-pip git build-essential curl unattended-upgrades

echo "==> [2/6] Ollama + llama3.2 (helper LLM; GLM itself is the cloud API, not pulled here)"
if ! command -v ollama >/dev/null 2>&1; then
  curl -fsSL https://ollama.com/install.sh | sh
fi
sudo systemctl enable --now ollama
for _ in $(seq 1 30); do curl -sf http://localhost:11434/api/tags >/dev/null 2>&1 && break; sleep 1; done
ollama pull llama3.2

echo "==> [3/6] App code at $APP_DIR"
if [ ! -d "$APP_DIR" ]; then
  sudo mkdir -p "$APP_DIR"
  sudo chown "$SERVICE_USER":"$SERVICE_USER" "$APP_DIR"
fi
if [ -d "$APP_DIR/.git" ]; then
  git -C "$APP_DIR" pull --ff-only
elif [ -n "$REPO_URL" ]; then
  git clone "$REPO_URL" "$APP_DIR"
else
  echo "!! No repo at $APP_DIR and REPO_URL not set. Set REPO_URL and re-run." >&2
  exit 1
fi

echo "==> [4/6] Python venv + dependencies"
cd "$APP_DIR"
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt

echo "==> [5/6] .env check"
if [ -f "$APP_DIR/.env" ]; then
  chmod 600 "$APP_DIR/.env"
else
  echo "!! $APP_DIR/.env is missing. scp it from your Mac (chmod 600), then run step 6 / start the service." >&2
fi

echo "==> [6/6] systemd service (user=$SERVICE_USER, dir=$APP_DIR)"
TMP_UNIT="$(mktemp)"
sed "s#/opt/stock-research-agent#${APP_DIR}#g; s/^User=.*/User=${SERVICE_USER}/" \
  "$APP_DIR/deploy/stock-research-bot.service" > "$TMP_UNIT"
sudo cp "$TMP_UNIT" /etc/systemd/system/stock-research-bot.service
rm -f "$TMP_UNIT"
sudo systemctl daemon-reload
sudo systemctl enable stock-research-bot

echo
echo "Setup complete."
echo "  Before starting: STOP any other bot polling this token (e.g. the Mac one) — only ONE poller allowed."
echo "  Start:   sudo systemctl start stock-research-bot"
echo "  Logs:    journalctl -u stock-research-bot -f"
