#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
SERVICE_FILE="$PROJECT_DIR/scripts/sentinel.service"
SYSTEMD_DIR="/etc/systemd/system"
JOURNALD_DIR="/etc/systemd/journald.conf.d"
JOURNALD_CONFIG="$PROJECT_DIR/scripts/journald-sentinel.conf"

echo "=== Sentinel Installer ==="
echo "Project directory: $PROJECT_DIR"

# 1. Install dependencies via uv
echo ""
echo "[1/5] Installing Python dependencies..."
cd "$PROJECT_DIR"
uv sync

# 2. Update service file paths to match actual install location
echo "[2/5] Generating systemd unit file..."
sed \
  -e "s|WorkingDirectory=.*|WorkingDirectory=$PROJECT_DIR|" \
  -e "s|EnvironmentFile=-.*|EnvironmentFile=-$PROJECT_DIR/.env|" \
  -e "s|ExecStart=.*|ExecStart=$PROJECT_DIR/.venv/bin/python -m sentinel run|" \
  -e "s|ReadWritePaths=.*|ReadWritePaths=$PROJECT_DIR|" \
  "$SERVICE_FILE" | sudo tee "$SYSTEMD_DIR/sentinel.service" > /dev/null

if [[ ! -f "$PROJECT_DIR/.env" ]]; then
  echo "  WARNING: $PROJECT_DIR/.env not found."
  echo "           Copy .env.example to .env and fill in your secrets,"
  echo "           otherwise Telegram/webhook notifications will be skipped."
else
  chmod 0600 "$PROJECT_DIR/.env"
fi

# 3. Install the bounded journald policy
echo "[3/5] Installing journald limits..."
sudo install -d -m 0755 "$JOURNALD_DIR"
sudo install -m 0644 "$JOURNALD_CONFIG" \
  "$JOURNALD_DIR/99-sentinel-limits.conf"
sudo systemctl restart systemd-journald

# 4. Reload and enable
echo "[4/5] Enabling systemd service..."
sudo systemctl daemon-reload
sudo systemctl enable sentinel

# 5. (Re)start so the new unit takes effect even on re-installs
echo "[5/5] (Re)starting Sentinel..."
sudo systemctl restart sentinel

echo ""
echo "Done! Check status with:"
echo "  sudo systemctl status sentinel"
echo "  journalctl -u sentinel -f"
