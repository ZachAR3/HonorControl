#!/usr/bin/env bash
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
    echo "error: run this uninstaller with sudo" >&2
    exit 1
fi

systemctl disable --now honor-touchpad-restore.service 2>/dev/null || true
rm -f /etc/systemd/system/honor-touchpad-restore.service
rm -f /usr/lib/systemd/system-sleep/honor-touchpad-restore
rm -f /usr/bin/honor-touchpadctl
rm -rf /usr/lib/honor-touchpad
systemctl daemon-reload

echo "Removed the touchpad-only controller. /etc/honor-touchpad.toml was preserved."
