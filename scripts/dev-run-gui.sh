#!/usr/bin/env bash
# Run the Honor Control GUI for local development.
#
# Usage:
#   scripts/dev-run-gui.sh                  # system bus
#   scripts/dev-run-gui.sh --bus session    # session bus (dev mode)
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"

PY=""
for candidate in "$ROOT/.venv/bin/python" /opt/honor-control/current/venv/bin/python; do
    if [[ -x "$candidate" ]]; then
        PY="$candidate"
        break
    fi
done
if [[ -z "$PY" ]]; then
    PY="$(command -v python3 || true)"
fi
if [[ -z "$PY" ]]; then
    echo "error: no python found (create .venv with PySide6 installed)" >&2
    exit 1
fi

exec "$PY" -m honor_control.frontend.gui.app "$@"
