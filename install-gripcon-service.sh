#!/usr/bin/env bash
set -euo pipefail

if [[ $(id -u) -ne 0 ]]; then
  echo "This script must be run as root. Use sudo."
  exit 1
fi

SERVICE_FILE_SOURCE="/home/hna/scripts/gripcon.service"
SERVICE_FILE_TARGET="/etc/systemd/system/gripcon.service"

if [[ ! -f "$SERVICE_FILE_SOURCE" ]]; then
  echo "Source service file not found: $SERVICE_FILE_SOURCE"
  exit 1
fi

cp "$SERVICE_FILE_SOURCE" "$SERVICE_FILE_TARGET"
chmod 644 "$SERVICE_FILE_TARGET"
systemctl daemon-reload
systemctl enable gripcon.service
systemctl restart gripcon.service
systemctl status gripcon.service --no-pager
