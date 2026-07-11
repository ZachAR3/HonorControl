#!/usr/bin/env bash
# Run the Honor Control backend service for local development.
#
# By default starts on the session bus with FakeHardware (no root).
# Pass --system to use the real system bus (needs root).
#
# Usage:
#   scripts/dev-run-service.sh                  # session bus (FakeHardware, no root)
#   scripts/dev-run-service.sh --system         # system bus (needs root)
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"

# Prefer local .venv, then system venv, then system python3.
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
    echo "error: no python found (create .venv or install python3)" >&2
    exit 1
fi

ARGS=(--verbose)
if [[ "${1:-}" == "--system" ]]; then
    shift
else
    ARGS+=(--session-bus)
fi
[[ $# -gt 0 ]] && ARGS+=("$@")

exec "$PY" -m honor_control.backend.service "${ARGS[@]}"
