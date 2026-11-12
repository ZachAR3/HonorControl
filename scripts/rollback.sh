#!/usr/bin/env bash
# Transactionally restore the predecessor release and its managed system files.
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
[[ -n "$CURRENT" && -d "$CURRENT" ]] || {
    echo "error: no active release at $INSTALL_ROOT/current" >&2
    exit 1
}
ROLLBACK_STATE="$CURRENT/rollback-state"
MANAGED="$ROLLBACK_STATE/managed.tsv"
ENV_FILE="$ROLLBACK_STATE/release.env"
[[ -f "$MANAGED" && -f "$ENV_FILE" ]] || {
    echo "error: active release has no complete rollback state" >&2
    echo "       refusing a partial code-only rollback" >&2
    exit 1
}

read_value() {
    local key=$1
    sed -n "s/^${key}=//p" "$ENV_FILE" | tail -n 1
}

PREDECESSOR="$(read_value previous_release)"
CONTROL_ENABLED="$(read_value honor_control_enabled)"
CONTROL_ACTIVE="$(read_value honor_control_active)"
TOUCHPAD_ENABLED="$(read_value touchpad_restore_enabled)"
TOUCHPAD_ACTIVE="$(read_value touchpad_restore_active)"

PREDECESSOR="$(realpath -- "$PREDECESSOR" 2>/dev/null || true)"
[[ "$PREDECESSOR" == "$INSTALL_ROOT"/releases/* && -d "$PREDECESSOR/venv" ]] || {
    echo "error: recorded predecessor is missing or invalid: $PREDECESSOR" >&2
    exit 1
}
for value in \
    "$CONTROL_ENABLED" "$CONTROL_ACTIVE" \
    "$TOUCHPAD_ENABLED" "$TOUCHPAD_ACTIVE"; do
    [[ "$value" == true || "$value" == false ]] || {
        echo "error: invalid rollback service-state metadata" >&2
        exit 1
    }
done

TRANSACTION="$(mktemp -d /tmp/honor-control-rollback.XXXXXX)"
CURRENT_CONTROL_ENABLED=false
CURRENT_CONTROL_ACTIVE=false
CURRENT_TOUCHPAD_ENABLED=false
CURRENT_TOUCHPAD_ACTIVE=false
systemctl is-enabled --quiet honor-control.service 2>/dev/null && \
    CURRENT_CONTROL_ENABLED=true
systemctl is-active --quiet honor-control.service 2>/dev/null && \
    CURRENT_CONTROL_ACTIVE=true
systemctl is-enabled --quiet honor-touchpad-restore.service 2>/dev/null && \
    CURRENT_TOUCHPAD_ENABLED=true
systemctl is-active --quiet honor-touchpad-restore.service 2>/dev/null && \
    CURRENT_TOUCHPAD_ACTIVE=true

validate_destination() {
    case "$1" in
        /usr/bin/honor-control-service|/usr/bin/honor-control-gui|\
        /usr/bin/honor-control-tray|/usr/bin/honor-touchpadctl|\
        /usr/bin/honorctl|\
        /etc/honor-control/touchpad-firmware-qualified|\
        /etc/systemd/system/honor-control.service|\
        /etc/systemd/system/honor-touchpad-restore.service|\
        /etc/xdg/autostart/org.honorlinux.Control.Tray.desktop|\
        /usr/lib/honor-touchpad/honor_control/__init__.py|\
        /usr/lib/honor-touchpad/honor_control/backend/__init__.py|\
        /usr/lib/honor-touchpad/honor_control/backend/touchpad_firmware.py|\
        /usr/lib/honor-touchpad/honor_control/cli/__init__.py|\
        /usr/lib/honor-touchpad/honor_control/cli/touchpadctl.py|\
        /usr/lib/honor-touchpad/honor_control/core/__init__.py|\
        /usr/lib/honor-touchpad/honor_control/core/touchpad.py|\
        /usr/lib/honor-touchpad/honor_control/core/touchpad_profile.py|\
        /usr/lib/modules-load.d/honor-control.conf|\
        /usr/lib/systemd/system-sleep/honor-touchpad-restore|\
        /usr/share/applications/org.honorlinux.Control.desktop|\
        /usr/share/applications/org.honorlinux.Control.Tray.desktop|\
        /usr/share/dbus-1/system.d/org.honorlinux.Control1.conf|\
        /usr/share/dbus-1/system-services/org.honorlinux.Control1.service|\
        /usr/share/doc/honor-control/honor-touchpad.example.toml|\
        /usr/share/polkit-1/actions/org.honorlinux.control.policy)
            ;;
        *)
            echo "error: unsafe rollback destination in manifest: $1" >&2
            return 1
            ;;
    esac
}

# Capture the current release's system files so every rollback step can itself
# be rolled back if the predecessor fails its health check.
while IFS=$'\t' read -r index _existed destination; do
    [[ "$index" =~ ^[0-9]+$ ]] || {
        echo "error: invalid rollback manifest index" >&2
        exit 1
    }
    validate_destination "$destination"
    if [[ -e "$destination" || -L "$destination" ]]; then
        cp -a -- "$destination" "$TRANSACTION/$index"
        printf '%s\t1\t%s\n' "$index" "$destination" >> "$TRANSACTION/current.tsv"
    else
        printf '%s\t0\t%s\n' "$index" "$destination" >> "$TRANSACTION/current.tsv"
    fi
done < "$MANAGED"

set_enabled() {
    local unit=$1 enabled=$2
    if [[ "$enabled" == true ]]; then
        systemctl enable "$unit" >/dev/null 2>&1
    else
        systemctl disable "$unit" >/dev/null 2>&1 || true
    fi
}

restore_files() {
    local manifest=$1 backups=$2 index existed destination
    while IFS=$'\t' read -r index existed destination; do
        validate_destination "$destination"
        rm -f -- "$destination"
        if [[ "$existed" == 1 ]]; then
            install -d -m 0755 "$(dirname "$destination")"
            cp -a -- "$backups/$index" "$destination"
        fi
    done < "$manifest"
}

restore_current() {
    set +e
    restore_files "$TRANSACTION/current.tsv" "$TRANSACTION"
    ln -sfn "$CURRENT" "$INSTALL_ROOT/current"
    systemctl daemon-reload
    set_enabled honor-control.service "$CURRENT_CONTROL_ENABLED"
    set_enabled honor-touchpad-restore.service "$CURRENT_TOUCHPAD_ENABLED"
    if [[ "$CURRENT_CONTROL_ACTIVE" == true ]]; then
        systemctl restart honor-control.service
    else
        systemctl stop honor-control.service
    fi
    if [[ "$CURRENT_TOUCHPAD_ACTIVE" == true ]]; then
        systemctl restart honor-touchpad-restore.service
    else
        systemctl stop honor-touchpad-restore.service
    fi
    set -e
}

COMMITTED=false
cleanup() {
    local status=$?
    trap - EXIT
    if [[ "$COMMITTED" == false ]]; then
        echo "error: rollback failed; restoring the original release" >&2
        restore_current
    fi
    rm -rf -- "$TRANSACTION"
    exit "$status"
}
trap cleanup EXIT

echo "==> Rolling back $(basename "$CURRENT") -> $(basename "$PREDECESSOR")"
systemctl stop honor-control.service 2>/dev/null || true
systemctl stop honor-touchpad-restore.service 2>/dev/null || true
restore_files "$MANAGED" "$ROLLBACK_STATE/backups"

TMP_LINK="$INSTALL_ROOT/.current.rollback.$$"
ln -sfn "$PREDECESSOR" "$TMP_LINK"
mv -T "$TMP_LINK" "$INSTALL_ROOT/current"

systemctl daemon-reload
systemctl reload dbus 2>/dev/null || true
set_enabled honor-control.service "$CONTROL_ENABLED"
set_enabled honor-touchpad-restore.service "$TOUCHPAD_ENABLED"

if [[ "$CONTROL_ACTIVE" == true ]]; then
    systemctl restart honor-control.service
    systemctl is-active --quiet honor-control.service
else
    systemctl stop honor-control.service 2>/dev/null || true
fi
if [[ "$TOUCHPAD_ACTIVE" == true ]]; then
    systemctl restart honor-touchpad-restore.service
    systemctl is-active --quiet honor-touchpad-restore.service
else
    systemctl stop honor-touchpad-restore.service 2>/dev/null || true
fi

COMMITTED=true
echo "==> Rollback complete: $(basename "$PREDECESSOR")"
