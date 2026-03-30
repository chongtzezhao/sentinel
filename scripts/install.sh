#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
SERVICE_FILE="$PROJECT_DIR/scripts/sentinel.service"
SYSTEMD_DIR="/etc/systemd/system"

echo "=== Sentinel Installer ==="
echo "Project directory: $PROJECT_DIR"

# 1. Install dependencies via uv
echo ""
echo "[1/4] Installing Python dependencies..."
cd "$PROJECT_DIR"
uv sync

# 2. Update service file paths to match actual install location
echo "[2/4] Generating systemd unit file..."
sed \
  -e "s|WorkingDirectory=.*|WorkingDirectory=$PROJECT_DIR|" \
  -e "s|ExecStart=.*|ExecStart=$PROJECT_DIR/.venv/bin/python -m sentinel run|" \
  -e "s|ReadWritePaths=.*|ReadWritePaths=$PROJECT_DIR|" \
  "$SERVICE_FILE" | sudo tee "$SYSTEMD_DIR/sentinel.service" > /dev/null

# 3. Reload and enable
echo "[3/4] Enabling systemd service..."
sudo systemctl daemon-reload
sudo systemctl enable sentinel

# 4. Start
echo "[4/4] Starting Sentinel..."
sudo systemctl start sentinel

echo ""
echo "Done! Check status with:"
echo "  sudo systemctl status sentinel"
echo "  journalctl -u sentinel -f"
