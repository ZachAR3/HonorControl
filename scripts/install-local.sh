#!/usr/bin/env bash
# Install honor-control as a system service.
#
# Creates a versioned venv under /opt/honor-control/releases, installs the
# Python package and its dependencies, symlinks entry points into
# /usr/bin, and installs systemd/D-Bus/polkit/desktop files.
#
# Requires a running systemd instance, Python 3.11+, venv, and pip.
# Does NOT touch the system Python environment (PEP 668 safe).
#
# Usage:
#   sudo bash scripts/install-local.sh [--wheelhouse DIR]
set -euo pipefail

DEV_MODE=false
WHEELHOUSE=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --dev) DEV_MODE=true ;;
        --wheelhouse)
            [[ $# -ge 2 ]] || { echo "error: --wheelhouse requires a path" >&2; exit 2; }
            WHEELHOUSE="$2"
            shift
            ;;
        *) echo "error: unknown option: $1" >&2; exit 2 ;;
    esac
    shift
done

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
LEGACY_HONOR_POWER_RULE=/etc/udev/rules.d/99-honor-power.rules
LEGACY_HONOR_POWER_RULE_CONTENT='SUBSYSTEM=="power_supply", RUN+="/usr/bin/honor power udev-event"'
LEGACY_BACKUP="$STATE_DIR/installer-backups/99-honor-power.rules"
STAGE="$(mktemp -d /tmp/honor-control-install.XXXXXX)"
ACTIVATED=false
COMMITTED=false
LEGACY_REMOVED=false
MANAGED_DESTS=()
MANAGED_BACKUPS=()
MANAGED_EXISTED=()

backup_destination() {
    local destination=$1 index backup existed=0
    index=${#MANAGED_DESTS[@]}
    backup="$STAGE/managed-$index"
    if [[ -e "$destination" || -L "$destination" ]]; then
        cp -a -- "$destination" "$backup"
        existed=1
    fi
    MANAGED_DESTS+=("$destination")
    MANAGED_BACKUPS+=("$backup")
    MANAGED_EXISTED+=("$existed")
}

install_managed() {
    local source=$1 destination=$2 mode=$3
    backup_destination "$destination"
    install -D -m "$mode" "$source" "$destination"
}

link_managed() {
    local target=$1 destination=$2
    backup_destination "$destination"
    install -d -m 0755 "$(dirname "$destination")"
    ln -sfn "$target" "$destination"
}

rollback_managed() {
    local index destination
    for ((index=${#MANAGED_DESTS[@]}-1; index>=0; index--)); do
        destination=${MANAGED_DESTS[$index]}
        rm -f -- "$destination"
        if [[ ${MANAGED_EXISTED[$index]} == 1 ]]; then
            cp -a -- "${MANAGED_BACKUPS[$index]}" "$destination"
        fi
    done
}

cleanup() {
    local exit_status=$?
    set +e
    if [[ $exit_status -ne 0 && $COMMITTED == false ]]; then
        if $ACTIVATED; then
            if [[ -n "$OLD_RELEASE" && -d "$OLD_RELEASE" ]]; then
                ln -sfn "$OLD_RELEASE" "$INSTALL_ROOT/current"
            else
                rm -f "$INSTALL_ROOT/current"
            fi
        fi
        rollback_managed
        if $LEGACY_REMOVED && [[ -f "$LEGACY_BACKUP" ]] && \
            [[ ! -e "$LEGACY_HONOR_POWER_RULE" ]]; then
            install -D -m 0644 "$LEGACY_BACKUP" "$LEGACY_HONOR_POWER_RULE"
            command -v udevadm >/dev/null 2>&1 && \
                udevadm control --reload-rules 2>/dev/null || true
        fi
        if $WAS_ACTIVE && [[ -n "$OLD_RELEASE" && -d "$OLD_RELEASE" ]]; then
            systemctl daemon-reload 2>/dev/null || true
            systemctl restart honor-control.service 2>/dev/null || true
        fi
    fi
    rm -rf "$STAGE"
    if [[ $COMMITTED == false ]]; then
        rm -rf "$RELEASE_DIR"
    fi
    return "$exit_status"
}
trap cleanup EXIT
OLD_RELEASE="$(readlink -f "$INSTALL_ROOT/current" 2>/dev/null || true)"
WAS_ACTIVE=false
CURRENT_MANIFEST=""
[[ -n "$OLD_RELEASE" ]] && CURRENT_MANIFEST="$OLD_RELEASE/installed-files.sha256"
LEGACY_INSTALL=false
if [[ -n "$OLD_RELEASE" && "$OLD_RELEASE" == "$INSTALL_ROOT"/releases/* && \
      ! -f "$CURRENT_MANIFEST" ]]; then
    LEGACY_INSTALL=true
fi

assert_replaceable() {
    local source=$1 destination=$2 expected=""
    if [[ ! -e "$destination" && ! -L "$destination" ]]; then
        return
    fi
    if [[ -f "$destination" && ! -L "$destination" ]] && cmp -s "$source" "$destination"; then
        return
    fi
    if [[ -f "$CURRENT_MANIFEST" && -f "$destination" && ! -L "$destination" ]]; then
        expected="$(awk -v path="$destination" '$2 == path { print $1; exit }' "$CURRENT_MANIFEST")"
        if [[ -n "$expected" && "$(sha256sum "$destination" | cut -d' ' -f1)" == "$expected" ]]; then
            return
        fi
    fi
    # Releases installed before ownership manifests existed can be adopted
    # once.  The exact active release path is the legacy ownership marker.
    if $LEGACY_INSTALL; then
        return
    fi
    echo "error: refusing to replace unrelated or modified $destination" >&2
    exit 2
}

if ! command -v systemctl >/dev/null 2>&1 || [[ ! -d /run/systemd/system ]]; then
    echo "error: this system installer requires a running systemd instance" >&2
    exit 2
fi
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
if [[ -n "$WHEELHOUSE" && ! -d "$WHEELHOUSE" ]]; then
    echo "error: wheelhouse directory does not exist: $WHEELHOUSE" >&2
    exit 2
fi
HONOR_TOOLS_ROOT="$(cd "$ROOT/../honor-tools" 2>/dev/null && pwd || true)"
if [[ ( -z "$HONOR_TOOLS_ROOT" || \
        ! -f "$HONOR_TOOLS_ROOT/pyproject.toml" ) && -z "$WHEELHOUSE" ]]; then
    echo "error: honor-tools 0.1.0 is not published on the package index" >&2
    echo "Place its source at $ROOT/../honor-tools or provide it in --wheelhouse." >&2
    exit 2
fi
for script in honor-control-service honorctl honor-control-gui honor-control-tray; do
    destination="/usr/bin/$script"
    if [[ -e "$destination" || -L "$destination" ]]; then
        target="$(readlink -f "$destination" 2>/dev/null || true)"
        if [[ -z "$OLD_RELEASE" || \
              "$target" != "$OLD_RELEASE/venv/bin/$script" ]]; then
            echo "error: refusing to replace unrelated $destination" >&2
            exit 2
        fi
    fi
done

assert_replaceable "$ROOT/packaging/touchpad/honor-touchpadctl" /usr/bin/honor-touchpadctl
assert_replaceable "$ROOT/packaging/systemd/honor-control.service" /etc/systemd/system/honor-control.service
assert_replaceable "$ROOT/packaging/dbus/org.honorlinux.Control1.conf" /usr/share/dbus-1/system.d/org.honorlinux.Control1.conf
assert_replaceable "$ROOT/packaging/dbus/org.honorlinux.Control1.service" /usr/share/dbus-1/system-services/org.honorlinux.Control1.service
assert_replaceable "$ROOT/packaging/polkit/org.honorlinux.control.policy" /usr/share/polkit-1/actions/org.honorlinux.control.policy
assert_replaceable "$ROOT/packaging/desktop/org.honorlinux.Control.desktop" /usr/share/applications/org.honorlinux.Control.desktop
assert_replaceable "$ROOT/packaging/desktop/org.honorlinux.Control.Tray.desktop" /usr/share/applications/org.honorlinux.Control.Tray.desktop
assert_replaceable "$ROOT/packaging/systemd/honor-touchpad-restore.service" /etc/systemd/system/honor-touchpad-restore.service
assert_replaceable "$ROOT/packaging/systemd/honor-touchpad-system-sleep" /usr/lib/systemd/system-sleep/honor-touchpad-restore
assert_replaceable "$ROOT/packaging/touchpad/honor-touchpad.example.toml" /usr/share/doc/honor-control/honor-touchpad.example.toml

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
PIP_ARGS=(--quiet)
if [[ -n "$WHEELHOUSE" ]]; then
    PIP_ARGS+=(--no-index --find-links "$WHEELHOUSE")
fi
if [[ -n "$HONOR_TOOLS_ROOT" && -f "$HONOR_TOOLS_ROOT/pyproject.toml" ]]; then
    mkdir -p "$STAGE/honor-tools"
    install -m 0644 "$HONOR_TOOLS_ROOT/pyproject.toml" \
        "$STAGE/honor-tools/"
    [[ -f "$HONOR_TOOLS_ROOT/README.md" ]] && \
        install -m 0644 "$HONOR_TOOLS_ROOT/README.md" "$STAGE/honor-tools/"
    cp -a "$HONOR_TOOLS_ROOT/honor" "$STAGE/honor-tools/"
    find "$STAGE/honor-tools/honor" -type d -name __pycache__ -prune \
        -exec rm -rf {} +
    "$PIP" install "${PIP_ARGS[@]}" "$STAGE/honor-tools"
else
    "$PIP" install "${PIP_ARGS[@]}" "honor-tools==0.1.0"
fi
"$PIP" install "${PIP_ARGS[@]}" "$STAGE/source[gui]"

echo "==> [3/7] Validating isolated installation"
"$PY" -m pip check
"$PY" -c 'import honor_control, honor, sdbus, PySide6'
"$PY" -c \
    'from honor_control.backend.hardware import HonorToolsAdapter; assert HonorToolsAdapter().check_dependency()'
"$PY" -m compileall -q "$VENV_DIR/lib"/python*/site-packages/honor_control
for script in honor-control-service honorctl honor-touchpadctl honor-control-gui honor-control-tray; do
    test -x "$VENV_DIR/bin/$script"
done
if command -v systemd-analyze >/dev/null 2>&1; then
    systemd-analyze verify \
        "$ROOT/packaging/systemd/honor-control.service" \
        "$ROOT/packaging/systemd/honor-touchpad-restore.service"
fi

echo "==> [4/7] Installing system packaging files"
install_managed "$ROOT/packaging/systemd/honor-control.service" /etc/systemd/system/honor-control.service 0644
install_managed "$ROOT/packaging/dbus/org.honorlinux.Control1.conf" /usr/share/dbus-1/system.d/org.honorlinux.Control1.conf 0644
install_managed "$ROOT/packaging/dbus/org.honorlinux.Control1.service" /usr/share/dbus-1/system-services/org.honorlinux.Control1.service 0644
install_managed "$ROOT/packaging/polkit/org.honorlinux.control.policy" /usr/share/polkit-1/actions/org.honorlinux.control.policy 0644
install_managed "$ROOT/packaging/desktop/org.honorlinux.Control.desktop" /usr/share/applications/org.honorlinux.Control.desktop 0644
install_managed "$ROOT/packaging/desktop/org.honorlinux.Control.Tray.desktop" /usr/share/applications/org.honorlinux.Control.Tray.desktop 0644
install_managed "$ROOT/packaging/systemd/honor-touchpad-restore.service" /etc/systemd/system/honor-touchpad-restore.service 0644
install_managed "$ROOT/packaging/systemd/honor-touchpad-system-sleep" /usr/lib/systemd/system-sleep/honor-touchpad-restore 0755
install_managed "$ROOT/packaging/touchpad/honor-touchpad.example.toml" /usr/share/doc/honor-control/honor-touchpad.example.toml 0644
install -d -m755 /usr/lib/honor-touchpad/honor_control/{core,backend,cli}
install_managed "$ROOT/honor_control/__init__.py" /usr/lib/honor-touchpad/honor_control/__init__.py 0644
install_managed "$ROOT/honor_control/core/__init__.py" /usr/lib/honor-touchpad/honor_control/core/__init__.py 0644
install_managed "$ROOT/honor_control/core/touchpad.py" /usr/lib/honor-touchpad/honor_control/core/touchpad.py 0644
install_managed "$ROOT/honor_control/backend/__init__.py" /usr/lib/honor-touchpad/honor_control/backend/__init__.py 0644
install_managed "$ROOT/honor_control/backend/touchpad_firmware.py" /usr/lib/honor-touchpad/honor_control/backend/touchpad_firmware.py 0644
install_managed "$ROOT/honor_control/cli/__init__.py" /usr/lib/honor-touchpad/honor_control/cli/__init__.py 0644
install_managed "$ROOT/honor_control/cli/touchpadctl.py" /usr/lib/honor-touchpad/honor_control/cli/touchpadctl.py 0644
install_managed "$ROOT/packaging/touchpad/honor-touchpadctl" /usr/bin/honor-touchpadctl 0755

echo "==> [5/7] Creating state and runtime directories"
install -d -m 0750 "$STATE_DIR"
install -d -m 0750 "$RUN_DIR"

echo "==> [6/7] Activating release and entry points"
ln -sfn "$RELEASE_DIR" "$INSTALL_ROOT/current"
ACTIVATED=true
for script in honor-control-service honorctl honor-control-gui honor-control-tray; do
    link_managed "$INSTALL_ROOT/current/venv/bin/$script" "/usr/bin/$script"
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

# Preserve and remove only the exact conflicting legacy rule.  The backup is
# restored by uninstall and is never overwritten on upgrades.
if [[ -f "$LEGACY_HONOR_POWER_RULE" ]] && \
    [[ "$(cat "$LEGACY_HONOR_POWER_RULE")" == "$LEGACY_HONOR_POWER_RULE_CONTENT" ]]; then
    install -d -m 0750 "$(dirname "$LEGACY_BACKUP")"
    if [[ ! -e "$LEGACY_BACKUP" ]]; then
        install -m 0644 "$LEGACY_HONOR_POWER_RULE" "$LEGACY_BACKUP"
    fi
    rm -f "$LEGACY_HONOR_POWER_RULE"
    LEGACY_REMOVED=true
    command -v udevadm >/dev/null 2>&1 && udevadm control --reload-rules || true
fi

MANIFEST="$RELEASE_DIR/installed-files.sha256"
for destination in "${MANAGED_DESTS[@]}"; do
    [[ -f "$destination" && ! -L "$destination" ]] && sha256sum "$destination" >> "$MANIFEST"
done
COMMITTED=true

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
