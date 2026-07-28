#!/usr/bin/env bash
# Normalize a locally built source distribution for reproducible hashing.
set -euo pipefail

[[ $# -eq 2 ]] || {
    echo "usage: normalize-sdist.sh ARCHIVE SOURCE_DATE_EPOCH" >&2
    exit 2
}
ARCHIVE="$(realpath -- "$1")"
EPOCH="$2"
[[ -f "$ARCHIVE" && "$ARCHIVE" == *.tar.gz ]] || {
    echo "error: source distribution not found: $ARCHIVE" >&2
    exit 2
}
[[ "$EPOCH" =~ ^[0-9]+$ ]] || {
    echo "error: SOURCE_DATE_EPOCH must be an integer" >&2
    exit 2
}

WORK="$(mktemp -d /tmp/honor-control-sdist.XXXXXX)"
OUTPUT="$(mktemp "$(dirname "$ARCHIVE")/.normalized-sdist.XXXXXX")"
cleanup() {
    rm -rf -- "$WORK"
    rm -f -- "$OUTPUT"
}
trap cleanup EXIT

tar -xzf "$ARCHIVE" -C "$WORK"
mapfile -t ROOTS < <(
    find "$WORK" -mindepth 1 -maxdepth 1 -type d -printf '%f\n'
)
[[ ${#ROOTS[@]} -eq 1 ]] || {
    echo "error: expected one top-level source directory" >&2
    exit 2
}

tar \
    --sort=name \
    --format=posix \
    --pax-option=delete=atime,delete=ctime \
    --mtime="@$EPOCH" \
    --owner=0 \
    --group=0 \
    --numeric-owner \
    -C "$WORK" \
    -cf - "${ROOTS[0]}" |
    gzip -n -9 > "$OUTPUT"
chmod 0644 "$OUTPUT"
mv -f -- "$OUTPUT" "$ARCHIVE"
