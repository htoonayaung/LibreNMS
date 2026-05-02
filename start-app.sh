#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$SCRIPT_DIR/venv"
LOCKFILE="$SCRIPT_DIR/gripcon.start.lock"

if [[ ! -f "$VENV_DIR/bin/activate" ]]; then
  echo "Virtual environment not found at $VENV_DIR"
  exit 1
fi

exec 200>"$LOCKFILE"
flock -n 200 || {
  echo "Another start-app.sh instance is already running. Exiting."
  exit 0
}

source "$VENV_DIR/bin/activate"
cd "$SCRIPT_DIR"
exec "$VENV_DIR/bin/python" "$SCRIPT_DIR/gripcon.py" --restart-delay 3 --max-restarts 0 -- "$VENV_DIR/bin/gunicorn" -w 4 -b 0.0.0.0:8000 app:app
