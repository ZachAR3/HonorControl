#!/usr/bin/env bash
# Install only the standard-library Honor touchpad controller and restore unit.
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
    echo "error: run this installer with sudo" >&2
    exit 1
fi

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
DEST=/usr/lib/honor-touchpad/honor_control

if ! python3 -c 'import sys; raise SystemExit(sys.version_info < (3, 11))'; then
    echo "error: Python 3.11 or newer is required" >&2
    exit 2
fi

echo "==> Installing dependency-free touchpad controller"
install -d -m 0755 "$DEST/core" "$DEST/backend" "$DEST/cli"
install -m 0644 "$ROOT/honor_control/__init__.py" "$DEST/__init__.py"
install -m 0644 \
    "$ROOT/honor_control/core/__init__.py" \
    "$ROOT/honor_control/core/touchpad.py" \
    "$DEST/core/"
install -m 0644 \
    "$ROOT/honor_control/backend/__init__.py" \
    "$ROOT/honor_control/backend/touchpad_firmware.py" \
    "$DEST/backend/"
install -m 0644 \
    "$ROOT/honor_control/cli/__init__.py" \
    "$ROOT/honor_control/cli/touchpadctl.py" \
    "$DEST/cli/"
install -m 0755 "$ROOT/packaging/touchpad/honor-touchpadctl" \
    /usr/bin/honor-touchpadctl

echo "==> Installing profile restore files"
install -m 0644 "$ROOT/packaging/systemd/honor-touchpad-restore.service" \
    /etc/systemd/system/honor-touchpad-restore.service
install -D -m 0755 "$ROOT/packaging/systemd/honor-touchpad-system-sleep" \
    /usr/lib/systemd/system-sleep/honor-touchpad-restore
install -D -m 0644 "$ROOT/packaging/touchpad/honor-touchpad.example.toml" \
    /usr/share/doc/honor-control/honor-touchpad.example.toml

python3 -m compileall -q /usr/lib/honor-touchpad
honor-touchpadctl list >/dev/null
honor-touchpadctl apply --dry-run \
    /usr/share/doc/honor-control/honor-touchpad.example.toml >/dev/null
systemctl daemon-reload

echo "Done. Run the read-only check with:"
echo "  sudo honor-touchpadctl --json probe"
echo "The restore service remains disabled until you create /etc/honor-touchpad.toml."
