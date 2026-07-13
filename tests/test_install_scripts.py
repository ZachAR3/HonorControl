"""Static safety checks for the system installer and uninstaller."""

from __future__ import annotations

import pathlib
import subprocess

ROOT = pathlib.Path(__file__).parent.parent
INSTALL = ROOT / "scripts/install-local.sh"
UNINSTALL = ROOT / "scripts/uninstall-local.sh"


def test_install_scripts_parse_with_bash() -> None:
    subprocess.run(
        ["bash", "-n", str(INSTALL), str(UNINSTALL)],
        check=True,
        capture_output=True,
        text=True,
    )


def test_installer_has_transaction_and_offline_mode() -> None:
    source = INSTALL.read_text(encoding="utf-8")
    assert "--wheelhouse" in source
    assert '"honor-tools==0.1.0"' in source
    assert "not published on the package index" in source
    assert "rollback_managed" in source
    assert "assert_replaceable" in source
    assert "refusing to replace unrelated" in source
    assert "COMMITTED=true" in source


def test_uninstaller_checks_ownership_and_stops_touchpad_service() -> None:
    source = UNINSTALL.read_text(encoding="utf-8")
    assert "installed-files.sha256" in source
    assert "preserving modified or unowned" in source
    assert "disable --now honor-touchpad-restore.service" in source
    assert "rm -f /etc/honor-touchpad.toml" in source
