#!/usr/bin/env bash
# Launches katomart with the correct virtual environment
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec "${SCRIPT_DIR}/venv/bin/python" "${SCRIPT_DIR}/main.py" "$@"
