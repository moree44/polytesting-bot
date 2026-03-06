#!/usr/bin/env bash
set -euo pipefail
exec "$(dirname "$0")/venv/bin/python3" "$(dirname "$0")/polybot_web.py"