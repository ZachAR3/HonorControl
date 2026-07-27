#!/usr/bin/env bash
# Roll back honor-control to the retained predecessor release.
#
# The system installer keeps exactly two releases under
# /opt/honor-control/releases: the active one (the target of the
# /opt/honor-control/current symlink) and one known-good predecessor.
# This script atomically repoints the current symlink to that predecessor
# and restarts the service. It refuses if no predecessor is retained.
#
# Usage:
#   sudo bash scripts/rollback.sh
#   sudo bash scripts/install-local.sh --rollback   # equivalent
set -euo pipefail

INSTALL_ROOT=/opt/honor-control

if [[ $EUID -ne 0 ]]; then
    echo "error: run this rollback with sudo" >&2
    exit 1
fi
if ! command -v systemctl >/dev/null 2>&1 || [[ ! -d /run/systemd/system ]]; then
    echo "error: rollback requires a running systemd instance" >&2
    exit 2
fi

CURRENT="$(readlink -f "$INSTALL_ROOT/current" 2>/dev/null || true)"
if [[ -z "$CURRENT" || ! -d "$CURRENT" ]]; then
    echo "error: no active release at $INSTALL_ROOT/current" >&2
    exit 1
fi

# The predecessor is the most recent retained release that is not active.
PREDECESSOR="$(
    for candidate in "$INSTALL_ROOT"/releases/*; do
        [[ -d "$candidate" ]] || continue
        [[ "$candidate" == "$CURRENT" ]] && continue
        echo "$candidate"
    done | sort | tail -n 1
)"
if [[ -z "$PREDECESSOR" || ! -d "$PREDECESSOR/venv" ]]; then
    echo "error: no retained predecessor release to roll back to" >&2
    echo "       (the installer retains only the active release and one predecessor)" >&2
    exit 1
fi

echo "==> Rolling back: $(basename "$CURRENT") -> $(basename "$PREDECESSOR")"
# Atomic switch: create a temporary symlink, then rename() it over current.
TMP_LINK="$INSTALL_ROOT/.current.rollback.$$"
ln -sfn "$PREDECESSOR" "$TMP_LINK"
mv -T "$TMP_LINK" "$INSTALL_ROOT/current"

systemctl daemon-reload 2>/dev/null || true
if systemctl restart honor-control.service; then
    echo "==> Rolled back to $(basename "$PREDECESSOR"); honor-control.service restarted."
else
    echo "error: honor-control.service failed to restart after rollback" >&2
    exit 1
fi
