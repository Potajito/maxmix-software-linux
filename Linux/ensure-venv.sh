#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# ensure-venv.sh - self-healing venv bootstrap.
#
# OS updates that bump the Python minor version (e.g. Fedora 3.14 -> 3.15)
# silently break .venv: its interpreter symlink still resolves to the new
# system python, so checks like `[ -x .venv/bin/python ]` pass, but the
# interpreter then looks for lib/python3.15/site-packages inside a venv
# built for the old version and every import fails. This script detects
# that state and rebuilds from scratch.
#
# Usage:  ./ensure-venv.sh      (idempotent; a cheap no-op when healthy)
# ---------------------------------------------------------------------------
set -euo pipefail
cd "$(dirname "$0")"

if [ -x .venv/bin/python ] && .venv/bin/python -c 'import serial, pulsectl' 2>/dev/null; then
    exit 0
fi

echo "Rebuilding .venv (missing, or broken by an OS Python update)..." >&2
rm -rf .venv
python3 -m venv .venv
.venv/bin/pip install --quiet --upgrade pip
.venv/bin/pip install --quiet -r requirements.txt -r requirements-gui.txt pyinstaller
echo ".venv ready ($(.venv/bin/python --version))" >&2
