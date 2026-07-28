"""Static safety checks for the system installer and uninstaller."""

from __future__ import annotations

import pathlib
import subprocess

ROOT = pathlib.Path(__file__).parent.parent
INSTALL = ROOT / "scripts/install-local.sh"
UNINSTALL = ROOT / "scripts/uninstall-local.sh"
INSTALL_TOUCHPAD = ROOT / "scripts/install-touchpad-only.sh"
UNINSTALL_TOUCHPAD = ROOT / "scripts/uninstall-touchpad-only.sh"
RELEASE = ROOT / "scripts/make-release-bundle.sh"
ROLLBACK = ROOT / "scripts/rollback.sh"
NORMALIZE_SDIST = ROOT / "scripts/normalize-sdist.sh"
SMOKE = ROOT / "scripts/smoke-test.sh"
TOUCHPAD_WRAPPER = ROOT / "packaging/touchpad/honor-touchpadctl"
SLEEP_HOOK = ROOT / "packaging/systemd/honor-touchpad-system-sleep"


def test_install_scripts_parse_with_bash() -> None:
    subprocess.run(
        [
            "bash",
            "-n",
            str(INSTALL),
            str(UNINSTALL),
            str(INSTALL_TOUCHPAD),
            str(UNINSTALL_TOUCHPAD),
            str(RELEASE),
            str(ROLLBACK),
            str(NORMALIZE_SDIST),
        ],
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
    assert "Keep the active release and one known-good predecessor" in source
    assert "Adopt a prior touchpad-only install" in source
    assert "wheelhouse contents do not exactly match SHA256SUMS" in source
    assert '"${HONOR_CONTROL_WHEEL}[gui]"' in source
    assert "rollback-state" in source


def test_uninstaller_checks_ownership_and_stops_touchpad_service() -> None:
    source = UNINSTALL.read_text(encoding="utf-8")
    assert "installed-files.sha256" in source
    assert "preserving modified or unowned" in source
    assert "disable --now honor-touchpad-restore.service" in source
    assert "rm -f /etc/honor-touchpad.toml" in source
    assert "touchpad-firmware-qualified" in source


def test_release_bundle_is_complete_and_never_overwrites_output() -> None:
    source = RELEASE.read_text(encoding="utf-8")
    assert "release output already exists; refusing to overwrite" in source
    assert 'cp -a honor_control "$BUNDLE/honor_control"' in source
    assert "pip download" in source
    assert "honor_tools-0.1.0" in source
    assert "verify-venv" in source
    assert "python\" -m pip check" in source
    assert 'rm -rf "$BUNDLE"' not in source
    assert "normalize-sdist.sh" in source


def test_rollback_restores_system_files_and_reverts_on_failure() -> None:
    source = ROLLBACK.read_text(encoding="utf-8")
    assert "active release has no complete rollback state" in source
    assert "restore_files" in source
    assert "restore_current" in source
    assert "rollback failed; restoring the original release" in source


def test_touchpad_installers_persist_qualification() -> None:
    full = INSTALL.read_text(encoding="utf-8")
    touchpad = INSTALL_TOUCHPAD.read_text(encoding="utf-8")
    for source in (full, touchpad):
        assert "--enable-touchpad-firmware" in source
        assert "/etc/honor-control/touchpad-firmware-qualified" in source


def test_privileged_touchpad_wrapper_uses_isolated_trusted_imports() -> None:
    source = TOUCHPAD_WRAPPER.read_text(encoding="utf-8")
    assert "exec /usr/bin/python3 -I -P -c" in source
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


def test_smoke_suite_runs_the_offline_suite_without_a_noop_marker() -> None:
    source = SMOKE.read_text(encoding="utf-8")
    assert '-m "not hardware"' not in source


def test_installer_provisions_required_modules_and_tray_autostart() -> None:
    source = INSTALL.read_text(encoding="utf-8")
    assert "/usr/lib/modules-load.d/honor-control.conf" in source
    assert "/etc/xdg/autostart/org.honorlinux.Control.Tray.desktop" in source
    modules = (ROOT / "packaging/modules-load.d/honor-control.conf").read_text(
        encoding="utf-8"
    )
    assert "acpi_call" in modules
    assert "uinput" in modules
