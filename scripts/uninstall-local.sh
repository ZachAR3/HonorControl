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

echo "==> Stopping service"
systemctl stop honor-control.service 2>/dev/null || true
systemctl disable honor-control.service 2>/dev/null || true

echo "==> Removing entry point symlinks"
for script in honor-control-service honorctl honor-touchpadctl honor-control-gui honor-control-tray; do
    rm -f "/usr/bin/$script"
done

echo "==> Removing system packaging files"
rm -f /etc/systemd/system/honor-control.service
rm -f /usr/share/dbus-1/system.d/org.honorlinux.Control1.conf
rm -f /usr/share/dbus-1/system-services/org.honorlinux.Control1.service
rm -f /usr/share/polkit-1/actions/org.honorlinux.control.policy
rm -f /usr/share/applications/org.honorlinux.Control.desktop
rm -f /usr/share/applications/org.honorlinux.Control.Tray.desktop
rm -f /etc/systemd/system/honor-touchpad-restore.service
rm -f /usr/lib/systemd/system-sleep/honor-touchpad-restore
rm -f /usr/share/doc/honor-control/honor-touchpad.example.toml
rm -rf /usr/lib/honor-touchpad

echo "==> Removing venv"
rm -rf "$INSTALL_ROOT"

if $PURGE; then
    echo "==> Purging state directory"
    rm -rf "$STATE_DIR"
else
    echo "==> Keeping state directory ($STATE_DIR)"
    echo "    Use --purge to remove it."
fi

echo "==> Reloading systemd"
systemctl daemon-reload

echo
echo "Done. honor-control has been removed."
