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
INSTALL_ROOT=/usr/lib/honor-touchpad
STAGE="$(mktemp -d /tmp/honor-touchpad-install.XXXXXX)"
DESTINATIONS=()
BACKUPS=()
EXISTED=()
COMMITTED=false
MANIFEST="$INSTALL_ROOT/installed-files.sha256"
LEGACY_INSTALL=false
if [[ -d "$DEST" && -f /etc/systemd/system/honor-touchpad-restore.service && \
      ! -f "$MANIFEST" ]]; then
    LEGACY_INSTALL=true
fi

backup_destination() {
    local destination=$1 index=${#DESTINATIONS[@]} backup existed=0
    backup="$STAGE/$index"
    if [[ -e "$destination" || -L "$destination" ]]; then
        cp -a -- "$destination" "$backup"
        existed=1
    fi
    DESTINATIONS+=("$destination")
    BACKUPS+=("$backup")
    EXISTED+=("$existed")
}

install_owned() {
    backup_destination "$2"
    install -D -m "$3" "$1" "$2"
}

assert_replaceable() {
    local source=$1 destination=$2 expected=""
    if [[ ! -e "$destination" && ! -L "$destination" ]]; then
        return
    fi
    if [[ -f "$destination" && ! -L "$destination" ]] && cmp -s "$source" "$destination"; then
        return
    fi
    if [[ -f "$MANIFEST" && -f "$destination" && ! -L "$destination" ]]; then
        expected="$(awk -v path="$destination" '$2 == path { print $1; exit }' "$MANIFEST")"
        if [[ -n "$expected" && "$(sha256sum "$destination" | cut -d' ' -f1)" == "$expected" ]]; then
            return
        fi
    fi
    if $LEGACY_INSTALL; then
        return
    fi
    echo "error: refusing to replace unrelated or modified $destination" >&2
    exit 2
}

cleanup() {
    local status=$? index destination
    set +e
    if [[ $status -ne 0 && $COMMITTED == false ]]; then
        for ((index=${#DESTINATIONS[@]}-1; index>=0; index--)); do
            destination=${DESTINATIONS[$index]}
            rm -f -- "$destination"
            if [[ ${EXISTED[$index]} == 1 ]]; then
                cp -a -- "${BACKUPS[$index]}" "$destination"
            fi
        done
    fi
    rm -rf "$STAGE"
    return "$status"
}
trap cleanup EXIT

if ! python3 -c 'import sys; raise SystemExit(sys.version_info < (3, 11))'; then
    echo "error: Python 3.11 or newer is required" >&2
    exit 2
fi
if ! command -v systemctl >/dev/null 2>&1 || [[ ! -d /run/systemd/system ]]; then
    echo "error: this installer requires a running systemd instance" >&2
    exit 2
fi
if [[ -e /opt/honor-control/current ]]; then
    echo "error: the full Honor Control installation owns these files" >&2
    exit 2
fi
assert_replaceable "$ROOT/packaging/touchpad/honor-touchpadctl" /usr/bin/honor-touchpadctl
assert_replaceable "$ROOT/honor_control/__init__.py" "$DEST/__init__.py"
assert_replaceable "$ROOT/honor_control/core/__init__.py" "$DEST/core/__init__.py"
assert_replaceable "$ROOT/honor_control/core/touchpad.py" "$DEST/core/touchpad.py"
assert_replaceable "$ROOT/honor_control/backend/__init__.py" "$DEST/backend/__init__.py"
assert_replaceable "$ROOT/honor_control/backend/touchpad_firmware.py" "$DEST/backend/touchpad_firmware.py"
assert_replaceable "$ROOT/honor_control/cli/__init__.py" "$DEST/cli/__init__.py"
assert_replaceable "$ROOT/honor_control/cli/touchpadctl.py" "$DEST/cli/touchpadctl.py"
assert_replaceable "$ROOT/packaging/systemd/honor-touchpad-restore.service" /etc/systemd/system/honor-touchpad-restore.service
assert_replaceable "$ROOT/packaging/systemd/honor-touchpad-system-sleep" /usr/lib/systemd/system-sleep/honor-touchpad-restore
assert_replaceable "$ROOT/packaging/touchpad/honor-touchpad.example.toml" /usr/share/doc/honor-control/honor-touchpad.example.toml

echo "==> Installing dependency-free touchpad controller"
install -d -m 0755 "$DEST/core" "$DEST/backend" "$DEST/cli"
install_owned "$ROOT/honor_control/__init__.py" "$DEST/__init__.py" 0644
install_owned "$ROOT/honor_control/core/__init__.py" "$DEST/core/__init__.py" 0644
install_owned "$ROOT/honor_control/core/touchpad.py" "$DEST/core/touchpad.py" 0644
install_owned "$ROOT/honor_control/backend/__init__.py" "$DEST/backend/__init__.py" 0644
install_owned "$ROOT/honor_control/backend/touchpad_firmware.py" "$DEST/backend/touchpad_firmware.py" 0644
install_owned "$ROOT/honor_control/cli/__init__.py" "$DEST/cli/__init__.py" 0644
install_owned "$ROOT/honor_control/cli/touchpadctl.py" "$DEST/cli/touchpadctl.py" 0644
install_owned "$ROOT/packaging/touchpad/honor-touchpadctl" /usr/bin/honor-touchpadctl 0755

echo "==> Installing profile restore files"
install_owned "$ROOT/packaging/systemd/honor-touchpad-restore.service" /etc/systemd/system/honor-touchpad-restore.service 0644
install_owned "$ROOT/packaging/systemd/honor-touchpad-system-sleep" /usr/lib/systemd/system-sleep/honor-touchpad-restore 0755
install_owned "$ROOT/packaging/touchpad/honor-touchpad.example.toml" /usr/share/doc/honor-control/honor-touchpad.example.toml 0644

python3 -m compileall -q /usr/lib/honor-touchpad
honor-touchpadctl list >/dev/null
honor-touchpadctl apply --dry-run \
    /usr/share/doc/honor-control/honor-touchpad.example.toml >/dev/null
systemctl daemon-reload
MANIFEST_STAGE="$STAGE/installed-files.sha256"
: > "$MANIFEST_STAGE"
for destination in "${DESTINATIONS[@]}"; do
    sha256sum "$destination" >> "$MANIFEST_STAGE"
done
backup_destination "$MANIFEST"
install -m 0644 "$MANIFEST_STAGE" "$MANIFEST"
COMMITTED=true

echo "Done. Run the read-only check with:"
echo "  sudo honor-touchpadctl --json probe"
echo "The restore service remains disabled until you create /etc/honor-touchpad.toml."
