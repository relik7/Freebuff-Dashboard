#!/bin/sh
cd "$(dirname "$0")" || exit 1
if command -v python3 >/dev/null 2>&1; then
    exec python3 fb-dashboard.py "$@"
fi
exec python fb-dashboard.py "$@"
