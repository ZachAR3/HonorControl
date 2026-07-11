#!/usr/bin/env bash
# Install honor-control as a system service.
#
# Creates a versioned venv under /opt/honor-control/releases, installs the
# Python package and its dependencies, symlinks entry points into
# /usr/bin, and installs systemd/D-Bus/polkit/desktop files.
#
# Works on any Linux distro with Python 3.11+ and pip.
# Does NOT touch the system Python environment (PEP 668 safe).
#
# Usage:
#   sudo bash scripts/install-local.sh [--dev]   # --dev installs from source tree
set -euo pipefail

DEV_MODE=false
[[ "${1:-}" == "--dev" ]] && DEV_MODE=true

if [[ $EUID -ne 0 ]]; then
    echo "error: run this installer with sudo" >&2
    exit 1
fi

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"

INSTALL_ROOT=/opt/honor-control
RELEASE_ID="$(date +%Y%m%d%H%M%S)-$$"
RELEASE_DIR="$INSTALL_ROOT/releases/$RELEASE_ID"
VENV_DIR="$RELEASE_DIR/venv"
STATE_DIR=/var/lib/honor-control
RUN_DIR=/run/honor-control
STAGE="$(mktemp -d /tmp/honor-control-install.XXXXXX)"
ACTIVATED=false
cleanup() {
    rm -rf "$STAGE"
    if ! $ACTIVATED; then
        rm -rf "$RELEASE_DIR"
    fi
}
trap cleanup EXIT
OLD_RELEASE="$(readlink -f "$INSTALL_ROOT/current" 2>/dev/null || true)"
WAS_ACTIVE=false
systemctl is-active --quiet honor-control.service && WAS_ACTIVE=true

if $DEV_MODE; then
    echo "error: editable root-service installs are unsupported." >&2
    echo "Use scripts/dev-run-service.sh and --bus session for development." >&2
    exit 2
fi

if ! python3 -c 'import sys; raise SystemExit(sys.version_info < (3, 11))'; then
    echo "error: Python 3.11 or newer is required" >&2
    exit 2
fi
if ! python3 -m venv --help >/dev/null 2>&1; then
    echo "error: Python venv support is missing (install python3-venv)" >&2
    exit 2
fi

echo "==> [1/7] Staging source and creating isolated venv"
mkdir -p "$STAGE/source"
install -m 0644 "$ROOT/pyproject.toml" "$ROOT/README.md" \
    "$ROOT/LICENSE" "$STAGE/source/"
cp -a "$ROOT/honor_control" "$STAGE/source/"
find "$STAGE/source/honor_control" -type d -name __pycache__ -prune \
    -exec rm -rf {} +
install -d -m 0755 "$RELEASE_DIR"
python3 -m venv "$VENV_DIR"
PIP="$VENV_DIR/bin/pip"
PY="$VENV_DIR/bin/python"

echo "==> [2/7] Installing Python dependencies"
HONOR_TOOLS_ROOT="$(cd "$ROOT/../honor-tools" 2>/dev/null && pwd || true)"
if [[ -n "$HONOR_TOOLS_ROOT" && -f "$HONOR_TOOLS_ROOT/pyproject.toml" ]]; then
    mkdir -p "$STAGE/honor-tools"
    install -m 0644 "$HONOR_TOOLS_ROOT/pyproject.toml" \
        "$STAGE/honor-tools/"
    [[ -f "$HONOR_TOOLS_ROOT/README.md" ]] && \
        install -m 0644 "$HONOR_TOOLS_ROOT/README.md" "$STAGE/honor-tools/"
    cp -a "$HONOR_TOOLS_ROOT/honor" "$STAGE/honor-tools/"
    find "$STAGE/honor-tools/honor" -type d -name __pycache__ -prune \
        -exec rm -rf {} +
    "$PIP" install --quiet "$STAGE/honor-tools"
fi
"$PIP" install --quiet "$STAGE/source[gui]"

echo "==> [3/7] Validating isolated installation"
"$PY" -m pip check
"$PY" -c 'import honor_control, honor, sdbus, PySide6'
"$PY" -m compileall -q "$VENV_DIR/lib"/python*/site-packages/honor_control
for script in honor-control-service honorctl honor-control-gui honor-control-tray; do
    test -x "$VENV_DIR/bin/$script"
done
if command -v systemd-analyze >/dev/null 2>&1; then
    systemd-analyze verify "$ROOT/packaging/systemd/honor-control.service"
fi

echo "==> [4/7] Installing system packaging files"
install -m644 "$ROOT/packaging/systemd/honor-control.service" \
    /etc/systemd/system/honor-control.service
install -m644 "$ROOT/packaging/dbus/org.honorlinux.Control1.conf" \
    /usr/share/dbus-1/system.d/org.honorlinux.Control1.conf
install -m644 "$ROOT/packaging/dbus/org.honorlinux.Control1.service" \
    /usr/share/dbus-1/system-services/org.honorlinux.Control1.service
install -m644 "$ROOT/packaging/polkit/org.honorlinux.control.policy" \
    /usr/share/polkit-1/actions/org.honorlinux.control.policy
install -m644 "$ROOT/packaging/desktop/org.honorlinux.Control.desktop" \
    /usr/share/applications/org.honorlinux.Control.desktop
install -m644 "$ROOT/packaging/desktop/org.honorlinux.Control.Tray.desktop" \
    /usr/share/applications/org.honorlinux.Control.Tray.desktop

echo "==> [5/7] Creating state and runtime directories"
install -d -m 0750 "$STATE_DIR"
install -d -m 0750 "$RUN_DIR"

echo "==> [6/7] Activating release and entry points"
ln -sfn "$RELEASE_DIR" "$INSTALL_ROOT/current"
ACTIVATED=true
for script in honor-control-service honorctl honor-control-gui honor-control-tray; do
    ln -sfn "$INSTALL_ROOT/current/venv/bin/$script" "/usr/bin/$script"
done

echo "==> [7/7] Reloading services"
systemctl daemon-reload
systemctl reload dbus 2>/dev/null || true
if $WAS_ACTIVE; then
    if ! systemctl restart honor-control.service; then
        echo "error: new service failed; restoring previous release" >&2
        if [[ -n "$OLD_RELEASE" && -d "$OLD_RELEASE" ]]; then
            ln -sfn "$OLD_RELEASE" "$INSTALL_ROOT/current"
            systemctl restart honor-control.service || true
        else
            rm -f "$INSTALL_ROOT/current"
        fi
        exit 1
    fi
fi

echo
echo "Done. Enable and start the service with:"
echo "  sudo systemctl enable --now honor-control.service"
echo
echo "Run the GUI/tray/CLI:"
echo "  honor-control-gui"
echo "  honor-control-tray"
echo "  honorctl status"
if $WAS_ACTIVE; then
    echo
    echo "The running service was restarted on release $RELEASE_ID."
fi
