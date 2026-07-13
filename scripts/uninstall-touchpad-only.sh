#!/usr/bin/env bash
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
    echo "error: run this uninstaller with sudo" >&2
    exit 1
fi
if ! command -v systemctl >/dev/null 2>&1 || [[ ! -d /run/systemd/system ]]; then
    echo "error: this uninstaller requires a running systemd instance" >&2
    exit 2
fi
if [[ -e /opt/honor-control/current ]]; then
    echo "error: use scripts/uninstall-local.sh for the full installation" >&2
    exit 2
fi

systemctl disable --now honor-touchpad-restore.service 2>/dev/null || true
MANIFEST=/usr/lib/honor-touchpad/installed-files.sha256
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
    rm -f "$MANIFEST"
else
    echo "warning: ownership manifest missing; preserving installed files" >&2
fi
rmdir /usr/lib/honor-touchpad/honor_control/core 2>/dev/null || true
rmdir /usr/lib/honor-touchpad/honor_control/backend 2>/dev/null || true
rmdir /usr/lib/honor-touchpad/honor_control/cli 2>/dev/null || true
rmdir /usr/lib/honor-touchpad/honor_control 2>/dev/null || true
rmdir /usr/lib/honor-touchpad 2>/dev/null || true
rmdir /usr/share/doc/honor-control 2>/dev/null || true
systemctl daemon-reload

echo "Removed the touchpad-only controller. /etc/honor-touchpad.toml was preserved."
