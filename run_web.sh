#!/usr/bin/env bash
set -euo pipefail
VENV_PY="/home/moree/poly-bot-tui/venv/bin/python3"
exec "$VENV_PY" "$(dirname "$0")/polybot_web.py"
