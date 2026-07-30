#!/usr/bin/env bash
# Manually fire a Sentinel test alert *inside the systemd service context*.
#
# Sends SIGUSR1 to the running sentinel.service so the daemon dispatches a
# synthetic alert through its full action pipeline (Telegram, webhooks, kill
# rules, etc.).  This is the only way to confirm that the systemd-managed
# process actually has the env vars and config it needs — running the daemon
# by hand would pick up the operator's shell environment instead.
#
# Usage:
#   scripts/test_alert.sh                # uses sentinel.service
#   SENTINEL_SERVICE=my.service scripts/test_alert.sh
#
# Requires: root (for systemctl kill).  Re-execs under sudo if needed.

set -euo pipefail

SERVICE="${SENTINEL_SERVICE:-sentinel.service}"
SUDO=""
if [[ $EUID -ne 0 ]]; then
  SUDO="sudo"
fi

if ! $SUDO systemctl is-active --quiet "$SERVICE"; then
  echo "Error: $SERVICE is not active." >&2
  echo "Start it first:  sudo systemctl start $SERVICE" >&2
  exit 1
fi

MAIN_PID="$($SUDO systemctl show -p MainPID --value "$SERVICE")"
echo "Service:  $SERVICE (MainPID=$MAIN_PID)"
echo "Sending SIGUSR1..."
$SUDO systemctl kill --signal=SIGUSR1 "$SERVICE"
echo "Signal sent. Recent log lines:"
echo "---"
# Give the daemon a beat to log + dispatch (network call to Telegram, etc.).
sleep 2
$SUDO journalctl -u "$SERVICE" -n 40 --no-pager
echo "---"
echo "Look for:"
echo "  - 'Received SIGUSR1 – dispatching manual test alert'"
echo "  - 'Test-alert env sanity:' and 'Test-alert config:' lines"
echo "  - 'ALERT [warning] Manual test alert ...'"
echo "  - 'Telegram notification delivered' (or an error if env is wrong)"
echo ""
echo "Tail live with:  sudo journalctl -u $SERVICE -f"
