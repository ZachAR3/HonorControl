"""Static safety checks for the system installer and uninstaller."""

from __future__ import annotations

import pathlib
import subprocess

ROOT = pathlib.Path(__file__).parent.parent
INSTALL = ROOT / "scripts/install-local.sh"
UNINSTALL = ROOT / "scripts/uninstall-local.sh"
SMOKE = ROOT / "scripts/smoke-test.sh"
TOUCHPAD_WRAPPER = ROOT / "packaging/touchpad/honor-touchpadctl"
SLEEP_HOOK = ROOT / "packaging/systemd/honor-touchpad-system-sleep"


def test_install_scripts_parse_with_bash() -> None:
    subprocess.run(
        ["bash", "-n", str(INSTALL), str(UNINSTALL)],
        check=True,
        capture_output=True,
        text=True,
    )

    subprocess.run(
        ["sh", "-n", str(TOUCHPAD_WRAPPER), str(SLEEP_HOOK)],
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


def test_privileged_touchpad_wrapper_uses_isolated_trusted_imports() -> None:
    source = TOUCHPAD_WRAPPER.read_text(encoding="utf-8")
    assert "exec /usr/bin/python3 -I -c" in source
    assert 'sys.path.insert(0, "/usr/lib/honor-touchpad")' in source
    assert "PYTHONPATH" not in source
    assert "python3 -m" not in source


def test_isolated_bootstrap_ignores_shadow_package(tmp_path: pathlib.Path) -> None:
    trusted = tmp_path / "trusted"
    shadow = tmp_path / "shadow"
    for root, value in ((trusted, "trusted"), (shadow, "shadow")):
        package = root / "honor_control" / "cli"
        package.mkdir(parents=True)
        (root / "honor_control" / "__init__.py").write_text("", encoding="utf-8")
        (package / "__init__.py").write_text("", encoding="utf-8")
        (package / "touchpadctl.py").write_text(
            f'SOURCE = "{value}"\n', encoding="utf-8"
        )

    code = (
        "import sys; "
        f"sys.path.insert(0, {str(trusted)!r}); "
        "from honor_control.cli.touchpadctl import SOURCE; print(SOURCE)"
    )
    result = subprocess.run(
        ["/usr/bin/python3", "-I", "-c", code],
        cwd=shadow,
        env={"PYTHONPATH": str(shadow)},
        check=True,
        capture_output=True,
        text=True,
    )
    assert result.stdout.strip() == "trusted"


def test_resume_jobs_share_one_ordered_transaction() -> None:
    source = SLEEP_HOOK.read_text(encoding="utf-8")
    assert "Before=honor-control.service" in (
        ROOT / "packaging/systemd/honor-touchpad-restore.service"
    ).read_text(encoding="utf-8")
    assert "honor-touchpad-restore.service honor-control.service" in source
    assert source.count("systemctl --no-block restart") == 2


def test_smoke_suite_always_excludes_hardware_tests() -> None:
    source = SMOKE.read_text(encoding="utf-8")
    assert '-m "not hardware"' in source
