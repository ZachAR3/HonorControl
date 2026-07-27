#!/usr/bin/env bash
# Assemble a versioned, hash-verified, offline release bundle.
#
# The bundle contains everything needed for a fully offline install on a
# supported host: the honor_control wheel + sdist (built with python -m build),
# the packaging tree, the install/uninstall/rollback scripts, a SHA256SUMS over
# every artifact, a MANIFEST (version / git commit / date), and a minimal
# sbom.json of the pinned dependencies. A drop-in directory is provided for the
# separately-reviewed honor-tools wheel.
#
# EXTERNAL STEPS (not performed here): obtaining the reviewed honor-tools 0.1.0
# wheel, and signing/publishing the bundle. This script only assembles and
# checksums the artifacts it can build locally.
#
# Usage:
#   bash scripts/make-release-bundle.sh [OUTPUT_DIR]
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
cd "$ROOT"

VERSION="$(sed -n 's/^version *= *"\(.*\)"/\1/p' pyproject.toml | head -n1)"
if [[ -z "$VERSION" ]]; then
    echo "error: could not read version from pyproject.toml" >&2
    exit 2
fi
GIT_COMMIT="$(git rev-parse HEAD 2>/dev/null || echo unknown)"
BUILD_DATE="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
BUNDLE="${1:-dist/release/honor-control-$VERSION}"

echo "==> Assembling release bundle for honor-control $VERSION"
echo "    commit: $GIT_COMMIT"
echo "    date:   $BUILD_DATE"
echo "    output: $BUNDLE"

rm -rf "$BUNDLE"
install -d -m 0755 "$BUNDLE/wheelhouse" "$BUNDLE/honor-tools" "$BUNDLE/scripts"

echo "==> [1/6] Building honor_control wheel + sdist"
if ! python3 -c "import build" 2>/dev/null; then
    echo "error: python -m build requires the build package (pip install build)" >&2
    exit 2
fi
python3 -m build --outdir "$BUNDLE/wheelhouse" >/dev/null

echo "==> [2/6] Copying packaging tree and installer scripts"
cp -a packaging "$BUNDLE/packaging"
install -m 0755 scripts/install-local.sh scripts/uninstall-local.sh scripts/rollback.sh "$BUNDLE/scripts/"

echo "==> [3/6] Creating honor-tools drop-in inbox"
cat > "$BUNDLE/honor-tools/README.txt" <<'NOTE'
Drop the REVIEWED honor-tools 0.1.0 wheel into the sibling wheelhouse/
directory, then regenerate the checksums before distributing:

  cd <bundle>/wheelhouse && sha256sum ./*.whl ./*.tar.gz > SHA256SUMS
  cd <bundle> && find . -type f ! -path ./SHA256SUMS -printf "%P\n" \
    | LC_ALL=C sort | while read -r f; do sha256sum "$f"; done > SHA256SUMS

Obtaining and reviewing the honor-tools wheel is an EXTERNAL step; this
bundle ships without it.
NOTE

echo "==> [4/6] Writing sbom.json (pinned dependencies)"
cat > "$BUNDLE/sbom.json" <<'JSON'
{
  "bomFormat": "honor-control-minimal-sbom",
  "specVersion": "1.0",
  "metadata": {
    "component": { "name": "honor-control", "type": "application" }
  },
  "components": [
    { "type": "library", "name": "sdbus",      "version": "0.14.2", "purl": "pkg:pypi/sdbus@0.14.2",      "scope": "runtime" },
    { "type": "library", "name": "PySide6",    "version": "6.11.1", "purl": "pkg:pypi/pyside6@6.11.1",    "scope": "runtime" },
    { "type": "library", "name": "setuptools", "version": "83.0.0", "purl": "pkg:pypi/setuptools@83.0.0", "scope": "build" },
    { "type": "library", "name": "wheel",      "version": "0.47.0", "purl": "pkg:pypi/wheel@0.47.0",      "scope": "build" }
  ]
}
JSON

echo "==> [5/6] Writing MANIFEST and wheelhouse checksums"
# Checksums for the offline wheelhouse (consumed by install-local.sh --wheelhouse).
(
    cd "$BUNDLE/wheelhouse"
    sha256sum ./*.whl ./*.tar.gz > SHA256SUMS
)
# MANIFEST records provenance plus the artifact list (paths relative to bundle).
(
    cd "$BUNDLE"
    {
        echo "package=honor-control"
        echo "version=$VERSION"
        echo "git_commit=$GIT_COMMIT"
        echo "date=$BUILD_DATE"
        echo "python=$(python3 --version 2>&1)"
        echo ""
        echo "# artifacts"
        find . -type f ! -path ./SHA256SUMS ! -path ./MANIFEST -printf "%P\n" | LC_ALL=C sort
    } > MANIFEST
)

echo "==> [6/6] Writing top-level SHA256SUMS over all artifacts"
(
    cd "$BUNDLE"
    find . -type f ! -path ./SHA256SUMS -printf "%P\n" | LC_ALL=C sort \
        | while read -r f; do sha256sum "$f"; done > SHA256SUMS
)

echo ""
echo "==> Release bundle assembled at: $BUNDLE"
echo ""
echo "EXTERNAL STEPS (not performed by this script):"
echo "  1. Obtain the REVIEWED honor-tools 0.1.0 wheel, drop it into"
echo "     $BUNDLE/wheelhouse/ , and regenerate the checksums (see"
echo "     $BUNDLE/honor-tools/README.txt)."
echo "  2. Sign and publish the bundle (for example, GPG-sign SHA256SUMS)."
echo ""
echo "Offline install on a supported host:"
echo "  sudo bash scripts/install-local.sh --wheelhouse $BUNDLE/wheelhouse"
