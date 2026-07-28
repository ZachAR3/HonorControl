#!/usr/bin/env bash
# Build a complete, platform-specific offline release bundle.
#
# Usage:
#   bash scripts/make-release-bundle.sh [OUTPUT_DIR] [--honor-tools-wheel FILE]
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
OUTPUT_DIR=""
HONOR_TOOLS_WHEEL=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --honor-tools-wheel)
            [[ $# -ge 2 ]] || {
                echo "error: --honor-tools-wheel requires a file" >&2
                exit 2
            }
            HONOR_TOOLS_WHEEL="$2"
            shift
            ;;
        --*)
            echo "error: unknown option: $1" >&2
            exit 2
            ;;
        *)
            [[ -z "$OUTPUT_DIR" ]] || {
                echo "error: only one output directory may be supplied" >&2
                exit 2
            }
            OUTPUT_DIR="$1"
            ;;
    esac
    shift
done

cd "$ROOT"
if [[ -n "$(git status --porcelain --untracked-files=normal)" ]]; then
    echo "error: release bundles require a clean Git worktree" >&2
    exit 2
fi
VERSION="$(sed -n 's/^version *= *"\(.*\)"/\1/p' pyproject.toml | head -n1)"
[[ -n "$VERSION" ]] || {
    echo "error: could not read version from pyproject.toml" >&2
    exit 2
}
GIT_COMMIT="$(git rev-parse --verify HEAD)"
SOURCE_DATE_EPOCH="$(git show -s --format=%ct HEAD)"
BUILD_DATE="$(date -u -d "@$SOURCE_DATE_EPOCH" +%Y-%m-%dT%H:%M:%SZ)"
BUNDLE="${OUTPUT_DIR:-dist/release/honor-control-$VERSION}"
BUNDLE="$(realpath -m -- "$BUNDLE")"
ROOT_REAL="$(realpath -- "$ROOT")"
USER_HOME="$(realpath -- "${HOME:?}")"

case "$BUNDLE" in
    /|"$ROOT_REAL"|"$USER_HOME")
        echo "error: unsafe release output path: $BUNDLE" >&2
        exit 2
        ;;
esac
if [[ -e "$BUNDLE" || -L "$BUNDLE" ]]; then
    echo "error: release output already exists; refusing to overwrite: $BUNDLE" >&2
    exit 2
fi
if ! python3 -m build --version >/dev/null 2>&1; then
    echo "error: install the Python build frontend (python3 -m pip install build)" >&2
    exit 2
fi
if ! python3 -c '
import importlib.metadata as metadata
raise SystemExit(
    metadata.version("setuptools") != "83.0.0"
    or metadata.version("wheel") != "0.47.0"
)
'; then
    echo "error: reproducible builds require setuptools 83.0.0 and wheel 0.47.0" >&2
    exit 2
fi

if [[ -n "$HONOR_TOOLS_WHEEL" ]]; then
    HONOR_TOOLS_WHEEL="$(realpath -- "$HONOR_TOOLS_WHEEL")"
    [[ -f "$HONOR_TOOLS_WHEEL" ]] || {
        echo "error: honor-tools wheel not found: $HONOR_TOOLS_WHEEL" >&2
        exit 2
    }
    [[ "$(basename "$HONOR_TOOLS_WHEEL")" == honor_tools-0.1.0-*.whl ]] || {
        echo "error: expected a reviewed honor_tools-0.1.0 wheel" >&2
        exit 2
    }
else
    HONOR_TOOLS_ROOT="$(realpath -m -- "$ROOT/../honor-tools")"
    [[ -f "$HONOR_TOOLS_ROOT/pyproject.toml" ]] || {
        echo "error: provide --honor-tools-wheel or place reviewed source at" >&2
        echo "       $ROOT/../honor-tools" >&2
        exit 2
    }
fi

echo "==> Building honor-control $VERSION offline bundle"
echo "    commit: $GIT_COMMIT"
echo "    output: $BUNDLE"
COMPLETE=false
install -d -m 0755 "$BUNDLE/wheelhouse" "$BUNDLE/scripts"
BUILD_ROOT="$(mktemp -d /tmp/honor-control-release-build.XXXXXX)"
cleanup() {
    rm -rf -- "$BUILD_ROOT"
    if [[ "$COMPLETE" == false ]]; then
        rm -rf -- "$BUNDLE"
    fi
}
trap cleanup EXIT

# Fixed timestamps and a pinned build backend make repeated builds of the same
# commit byte-for-byte reproducible.
export SOURCE_DATE_EPOCH PYTHONHASHSEED=0 TZ=UTC LC_ALL=C
install -d -m 0755 "$BUILD_ROOT/honor-control"
install -m 0644 pyproject.toml README.md LICENSE "$BUILD_ROOT/honor-control/"
cp -a honor_control tests "$BUILD_ROOT/honor-control/"
python3 -m build --no-isolation \
    --outdir "$BUNDLE/wheelhouse" "$BUILD_ROOT/honor-control"
shopt -s nullglob
APP_SDISTS=("$BUNDLE/wheelhouse"/honor_control-*.tar.gz)
shopt -u nullglob
[[ ${#APP_SDISTS[@]} -eq 1 ]] || {
    echo "error: expected exactly one honor-control source distribution" >&2
    exit 2
}
bash "$HERE/normalize-sdist.sh" "${APP_SDISTS[0]}" "$SOURCE_DATE_EPOCH"

if [[ -n "$HONOR_TOOLS_WHEEL" ]]; then
    install -m 0644 "$HONOR_TOOLS_WHEEL" "$BUNDLE/wheelhouse/"
else
    cp -a "$HONOR_TOOLS_ROOT" "$BUILD_ROOT/honor-tools"
    rm -rf -- \
        "$BUILD_ROOT/honor-tools/build" \
        "$BUILD_ROOT/honor-tools/honor_tools.egg-info"
    python3 -m build --no-isolation --wheel \
        --outdir "$BUNDLE/wheelhouse" "$BUILD_ROOT/honor-tools"
fi

# Resolve all runtime and GUI dependencies to wheels now. Installation never
# reaches a package index and never runs a build backend.
python3 -m pip download \
    --dest "$BUNDLE/wheelhouse" \
    --only-binary=:all: \
    "sdbus==0.14.2" \
    "PySide6==6.11.1"

# Exercise the same wheel-only install path used by the system installer in a
# brand-new virtual environment before publishing any manifest.
python3 -m venv "$BUILD_ROOT/verify-venv"
"$BUILD_ROOT/verify-venv/bin/pip" install --quiet \
    --no-index \
    --find-links "$BUNDLE/wheelhouse" \
    "honor-tools==0.1.0" \
    "honor-control[gui]==$VERSION"
"$BUILD_ROOT/verify-venv/bin/python" -m pip check
"$BUILD_ROOT/verify-venv/bin/python" -c \
    'import honor_control, honor, sdbus, PySide6'
for command in \
    honor-control-service honorctl honor-touchpadctl \
    honor-control-gui honor-control-tray; do
    test -x "$BUILD_ROOT/verify-venv/bin/$command"
done

cp -a packaging "$BUNDLE/packaging"
cp -a honor_control "$BUNDLE/honor_control"
find "$BUNDLE/honor_control" -type d -name __pycache__ -prune -exec rm -rf {} +
install -m 0644 pyproject.toml README.md LICENSE "$BUNDLE/"
install -m 0755 \
    scripts/install-local.sh \
    scripts/uninstall-local.sh \
    scripts/rollback.sh \
    "$BUNDLE/scripts/"

cat > "$BUNDLE/sbom.json" <<'JSON'
{
  "bomFormat": "CycloneDX",
  "specVersion": "1.5",
  "version": 1,
  "metadata": {
    "component": {"type": "application", "name": "honor-control"}
  },
  "components": [
    {"type": "library", "name": "honor-tools", "version": "0.1.0"},
    {"type": "library", "name": "sdbus", "version": "0.14.2"},
    {"type": "library", "name": "PySide6", "version": "6.11.1"}
  ]
}
JSON

(
    cd "$BUNDLE/wheelhouse"
    find . -maxdepth 1 -type f ! -name SHA256SUMS -printf '%P\n' |
        LC_ALL=C sort |
        while IFS= read -r artifact; do
            sha256sum -- "$artifact"
        done > "$BUILD_ROOT/wheelhouse.SHA256SUMS"
    mv "$BUILD_ROOT/wheelhouse.SHA256SUMS" SHA256SUMS
)

(
    cd "$BUNDLE"
    {
        echo "package=honor-control"
        echo "version=$VERSION"
        echo "git_commit=$GIT_COMMIT"
        echo "date=$BUILD_DATE"
        echo "source_date_epoch=$SOURCE_DATE_EPOCH"
        echo "python=$(python3 --version 2>&1)"
        echo
        echo "# artifacts"
        find . -type f ! -path ./SHA256SUMS ! -path ./MANIFEST -printf "%P\n" |
            LC_ALL=C sort
    } > MANIFEST
    find . -type f ! -path ./SHA256SUMS -printf '%P\n' |
        LC_ALL=C sort |
        while IFS= read -r artifact; do
            sha256sum -- "$artifact"
        done > "$BUILD_ROOT/bundle.SHA256SUMS"
    mv "$BUILD_ROOT/bundle.SHA256SUMS" SHA256SUMS
    sha256sum --strict -c SHA256SUMS >/dev/null
)

COMPLETE=true
echo "==> Complete offline bundle assembled at $BUNDLE"
echo "    Sign SHA256SUMS before distribution."
echo "    Install with: sudo bash scripts/install-local.sh --wheelhouse wheelhouse"
