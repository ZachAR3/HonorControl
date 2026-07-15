#!/usr/bin/env bash
# Non-destructive smoke test for honor-control.
#
# Runs read-only checks against fake hardware and the test suite.
# Does NOT write to hardware or require root.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
PY="$ROOT/.venv/bin/python"
[[ -x "$PY" ]] || PY="$(command -v python3 || true)"
if [[ -z "$PY" ]]; then
    echo "error: no python found" >&2
    exit 1
fi

RUFF="$ROOT/.venv/bin/ruff"
[[ -x "$RUFF" ]] || RUFF="$(command -v ruff || true)"
PYTEST="$ROOT/.venv/bin/pytest"
[[ -x "$PYTEST" ]] || PYTEST="$(command -v pytest || true)"
if [[ -z "$RUFF" || -z "$PYTEST" ]]; then
    echo "error: development tools missing; install the test extra" >&2
    echo "       python3 -m pip install -e '$ROOT[dev]'" >&2
    exit 1
fi

export QT_QPA_PLATFORM=offscreen

PASS=0
FAIL=0

check() {
    local label="$1"
    shift
    if "$@" >/dev/null 2>&1; then
        echo "  [ OK ] $label"
        PASS=$((PASS + 1))
    else
        echo "  [FAIL ] $label"
        FAIL=$((FAIL + 1))
    fi
}

echo "=== compileall ==="
check "python -m compileall honor_control" \
    "$PY" -m compileall "$ROOT/honor_control"

echo
echo "=== lint ==="
check "ruff check honor_control tests" \
    "$RUFF" check "$ROOT/honor_control" "$ROOT/tests"

echo
echo "=== import checks ==="
check "gui import" \
    "$PY" -c "from honor_control.frontend.gui.app import main; print('ok')"
check "tray import" \
    "$PY" -c "from honor_control.frontend.tray.tray import main; print('ok')"
check "backend import" \
    "$PY" -c "from honor_control.backend.service import main; print('ok')"
check "cli import" \
    "$PY" -c "from honor_control.cli.honorctl import main; print('ok')"

echo
echo "=== unit tests ==="
check "pytest" \
    "$PYTEST" "$ROOT/tests" -q -m "not hardware"

echo
echo "=== Summary ==="
echo "  Passed: $PASS"
echo "  Failed: $FAIL"
exit $((FAIL > 0 ? 1 : 0))
