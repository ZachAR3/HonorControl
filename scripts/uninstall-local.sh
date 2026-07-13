#!/usr/bin/env bash
# Remove all honor-control system files installed by install-local.sh.
#
# Stops the service, removes symlinks, system packaging files, and the
# dedicated venv.  It does NOT remove /var/lib/honor-control (state data)
# unless --purge is passed, and does not restore honor-tools' legacy power
# udev hook, which conflicts with PPD.
#
# Usage:
#   sudo bash scripts/uninstall-local.sh             # keep state
#   sudo bash scripts/uninstall-local.sh --purge      # remove state too
set -euo pipefail

PURGE=false
[[ "${1:-}" == "--purge" ]] && PURGE=true

INSTALL_ROOT=/opt/honor-control
STATE_DIR=/var/lib/honor-control
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"

if [[ $EUID -ne 0 ]]; then
    echo "error: run this uninstaller with sudo" >&2
    exit 1
fi
if ! command -v systemctl >/dev/null 2>&1 || [[ ! -d /run/systemd/system ]]; then
    echo "error: this system uninstaller requires a running systemd instance" >&2
    exit 2
fi

CURRENT_RELEASE="$(readlink -f "$INSTALL_ROOT/current" 2>/dev/null || true)"
MANIFEST="$CURRENT_RELEASE/installed-files.sha256"

remove_if_owned() {
    local destination=$1 source=${2:-}
    if [[ ! -e "$destination" && ! -L "$destination" ]]; then
        return
    fi
    if [[ -n "$source" && -f "$source" ]] && cmp -s "$destination" "$source"; then
        rm -f -- "$destination"
    else
        echo "warning: preserving modified or unowned $destination" >&2
    fi
}

echo "==> Stopping service"
systemctl stop honor-control.service 2>/dev/null || true
systemctl disable honor-control.service 2>/dev/null || true
systemctl disable --now honor-touchpad-restore.service 2>/dev/null || true

echo "==> Removing entry point symlinks"
for script in honor-control-service honorctl honor-touchpadctl honor-control-gui honor-control-tray; do
    destination="/usr/bin/$script"
    if [[ -L "$destination" ]]; then
        target="$(readlink -f "$destination" 2>/dev/null || true)"
        if [[ "$target" == "$INSTALL_ROOT"/* ]]; then
            rm -f -- "$destination"
        else
            echo "warning: preserving unowned $destination" >&2
        fi
    elif [[ "$script" == honor-touchpadctl ]]; then
        remove_if_owned "$destination" "$ROOT/packaging/touchpad/honor-touchpadctl"
    elif [[ -e "$destination" ]]; then
        echo "warning: preserving unowned $destination" >&2
    fi
done

echo "==> Removing system packaging files"
if [[ -f "$MANIFEST" ]]; then
    while read -r expected destination; do
        [[ -n "$expected" && -n "$destination" ]] || continue
        if [[ -f "$destination" ]] && \
            [[ "$(sha256sum "$destination" | cut -d' ' -f1)" == "$expected" ]]; then
            rm -f -- "$destination"
        elif [[ -e "$destination" || -L "$destination" ]]; then
            echo "warning: preserving modified $destination" >&2
        fi
    done < "$MANIFEST"
else
    remove_if_owned /etc/systemd/system/honor-control.service "$ROOT/packaging/systemd/honor-control.service"
    remove_if_owned /usr/share/dbus-1/system.d/org.honorlinux.Control1.conf "$ROOT/packaging/dbus/org.honorlinux.Control1.conf"
    remove_if_owned /usr/share/dbus-1/system-services/org.honorlinux.Control1.service "$ROOT/packaging/dbus/org.honorlinux.Control1.service"
    remove_if_owned /usr/share/polkit-1/actions/org.honorlinux.control.policy "$ROOT/packaging/polkit/org.honorlinux.control.policy"
    remove_if_owned /usr/share/applications/org.honorlinux.Control.desktop "$ROOT/packaging/desktop/org.honorlinux.Control.desktop"
    remove_if_owned /usr/share/applications/org.honorlinux.Control.Tray.desktop "$ROOT/packaging/desktop/org.honorlinux.Control.Tray.desktop"
    remove_if_owned /etc/systemd/system/honor-touchpad-restore.service "$ROOT/packaging/systemd/honor-touchpad-restore.service"
    remove_if_owned /usr/lib/systemd/system-sleep/honor-touchpad-restore "$ROOT/packaging/systemd/honor-touchpad-system-sleep"
    remove_if_owned /usr/share/doc/honor-control/honor-touchpad.example.toml "$ROOT/packaging/touchpad/honor-touchpad.example.toml"
fi
rmdir /usr/lib/honor-touchpad/honor_control/core 2>/dev/null || true
rmdir /usr/lib/honor-touchpad/honor_control/backend 2>/dev/null || true
rmdir /usr/lib/honor-touchpad/honor_control/cli 2>/dev/null || true
rmdir /usr/lib/honor-touchpad/honor_control 2>/dev/null || true
rmdir /usr/lib/honor-touchpad 2>/dev/null || true
rmdir /usr/share/doc/honor-control 2>/dev/null || true

LEGACY_BACKUP="$STATE_DIR/installer-backups/99-honor-power.rules"
LEGACY_RULE=/etc/udev/rules.d/99-honor-power.rules
if [[ -f "$LEGACY_BACKUP" ]]; then
    if [[ ! -e "$LEGACY_RULE" ]]; then
        install -D -m 0644 "$LEGACY_BACKUP" "$LEGACY_RULE"
        command -v udevadm >/dev/null 2>&1 && udevadm control --reload-rules || true
    else
        echo "warning: preserving legacy-rule backup because $LEGACY_RULE exists" >&2
    fi
fi

echo "==> Removing venv"
rm -rf "$INSTALL_ROOT"

if $PURGE; then
    echo "==> Purging state directory"
    rm -rf "$STATE_DIR"
    rm -f /etc/honor-touchpad.toml
else
    echo "==> Keeping state directory ($STATE_DIR)"
    echo "    Use --purge to remove it."
fi

echo "==> Reloading systemd"
systemctl daemon-reload

echo
echo "Done. honor-control has been removed."
