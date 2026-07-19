"""Tests for polkit policy/code/action consistency (WP-05, criterion 3)."""

from __future__ import annotations

import asyncio
import subprocess
import sys
import textwrap
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from honor_control.backend.dbus.authorizer import (
    ACTION_CONFIGURE_POWER,
    ACTION_EXPORT_DEBUG,
    ACTION_RELOAD_CONFIG,
    ACTION_SET_CHARGE_LIMIT,
    ACTION_SET_FAN_CURVE,
    ACTION_SET_GESTURES,
    ACTION_SET_GPU_IRQ,
    ACTION_SET_POWER_PROFILE,
    ACTION_SET_TOUCHPAD_FIRMWARE,
    ACTION_VIEW_LOGS,
    METHOD_ACTIONS,
    UNPRIVILEGED_METHODS,
    CallerSubject,
    PolkitAuthorityInterface,
    PolkitAuthorizer,
    _parse_start_time,
)
from honor_control.core.errors import DomainError, DomainException

#: All known action IDs defined in the code.
KNOWN_ACTIONS = frozenset(
    {
        ACTION_SET_CHARGE_LIMIT,
        ACTION_CONFIGURE_POWER,
        ACTION_SET_POWER_PROFILE,
        ACTION_SET_FAN_CURVE,
        ACTION_SET_GESTURES,
        ACTION_SET_TOUCHPAD_FIRMWARE,
        ACTION_SET_GPU_IRQ,
        ACTION_RELOAD_CONFIG,
        ACTION_EXPORT_DEBUG,
        ACTION_VIEW_LOGS,
    }
)

#: Path to the polkit policy XML file.
POLKIT_PATH = (
    Path(__file__).parent.parent
    / "packaging"
    / "polkit"
    / "org.honorlinux.control.policy"
)


class TestPolkitConsistency:
    """Verify polkit code constants, method mappings, and XML agree."""

    def test_all_method_actions_are_known(self) -> None:
        for _method, action in METHOD_ACTIONS.items():
            assert action in KNOWN_ACTIONS

    def test_unprivileged_methods_have_no_action(self) -> None:
        for method in UNPRIVILEGED_METHODS:
            assert method not in METHOD_ACTIONS

    def test_polkit_xml_matches_code(self) -> None:
        """Every action in the XML must be in the code, and vice versa."""
        assert POLKIT_PATH.exists(), f"Polkit file not found: {POLKIT_PATH}"
        tree = ET.parse(POLKIT_PATH)
        root = tree.getroot()
        xml_actions = {action.get("id") for action in root.findall("action")}
        assert xml_actions == KNOWN_ACTIONS

    def test_fan_curve_is_admin_auth(self) -> None:
        """Fan curve writes must require admin authentication."""
        tree = ET.parse(POLKIT_PATH)
        root = tree.getroot()
        for action in root.findall("action"):
            if action.get("id") == ACTION_SET_FAN_CURVE:
                defaults = action.find("defaults")
                assert defaults is not None
                allow_active = defaults.find("allow_active")
                assert allow_active is not None
                assert allow_active.text == "auth_admin"


class TestPolkitFailClosedBehavior:
    """Exercise repository-owned authorization failure semantics in isolation."""

    @staticmethod
    def _caller() -> CallerSubject:
        return CallerSubject(sender=":1.42", pid=1234, uid=1000, start_time=5678)

    def test_privileged_method_rejects_missing_caller(self) -> None:
        with pytest.raises(DomainException) as exc_info:
            asyncio.run(PolkitAuthorizer().check("SetManual", None))
        assert exc_info.value.code == DomainError.NOT_AUTHORIZED

    def test_unmapped_method_is_denied_before_polkit(self) -> None:
        with pytest.raises(DomainException) as exc_info:
            asyncio.run(PolkitAuthorizer().check("UnknownMutation", self._caller()))
        assert exc_info.value.code == DomainError.NOT_AUTHORIZED
        assert "No action mapping" in exc_info.value.message

    def test_polkit_denial_is_not_authorized(self, monkeypatch) -> None:
        calls: list[str] = []

        class Authority:
            async def CheckAuthorization(
                self, _subject, action_id, _details, _flags, _cancellation_id
            ):
                calls.append(action_id)
                return False, False, {}

        self._install_authority(monkeypatch, Authority())
        with pytest.raises(DomainException) as exc_info:
            asyncio.run(PolkitAuthorizer().check("SetManual", self._caller()))

        assert exc_info.value.code == DomainError.NOT_AUTHORIZED
        assert calls == [ACTION_SET_FAN_CURVE]

    def test_polkit_exception_fails_closed(self, monkeypatch) -> None:
        class Authority:
            async def CheckAuthorization(self, *_args):
                raise OSError("polkit unavailable")

        self._install_authority(monkeypatch, Authority())
        with pytest.raises(DomainException) as exc_info:
            asyncio.run(PolkitAuthorizer().check("SetManual", self._caller()))
        assert exc_info.value.code == DomainError.NOT_AUTHORIZED
        assert "unavailable" in exc_info.value.message

    def test_polkit_timeout_fails_closed(self, monkeypatch) -> None:
        class Authority:
            async def CheckAuthorization(self, *_args):
                await asyncio.sleep(0.05)
                return True, False, {}

        self._install_authority(monkeypatch, Authority())
        with pytest.raises(DomainException) as exc_info:
            asyncio.run(
                PolkitAuthorizer(timeout=0.001).check("SetManual", self._caller())
            )
        assert exc_info.value.code == DomainError.NOT_AUTHORIZED

    def test_api_denial_happens_before_application_invocation(self) -> None:
        script = textwrap.dedent(
            """
            import asyncio
            import honor_control.backend.dbus.api as api
            from honor_control.backend.dbus.api import ControlInterface, NotAuthorizedError
            from honor_control.backend.dbus.authorizer import CallerSubject
            from honor_control.core.errors import DomainError, DomainException

            class DenyingAuthorizer:
                async def check(self, _method, _caller):
                    raise DomainException(DomainError.NOT_AUTHORIZED, "denied")

            class App:
                called = False

                async def set_fan_manual(self, _speed, _ttl):
                    self.called = True
                    raise AssertionError("application must not be invoked")

            async def capture():
                return CallerSubject(
                    sender=":1.42", pid=1234, uid=1000, start_time=5678
                )

            async def main():
                app = App()
                interface = ControlInterface(app=app, authorizer=DenyingAuthorizer())
                api._capture_caller = capture
                method = ControlInterface.__dict__["SetManual"].original_method
                try:
                    await method(interface, 50, 300)
                except NotAuthorizedError:
                    pass
                else:
                    raise AssertionError("denial was not mapped")
                assert app.called is False

            asyncio.run(main())
            """
        )
        subprocess.run(
            [sys.executable, "-c", script],
            check=True,
            capture_output=True,
            text=True,
        )

    @staticmethod
    def _install_authority(monkeypatch, authority) -> None:
        import sdbus

        monkeypatch.setattr(sdbus, "get_default_bus", lambda: object())
        monkeypatch.setattr(
            PolkitAuthorityInterface,
            "new_proxy",
            staticmethod(lambda *_args, **_kwargs: authority),
        )


class TestPolkitPolicyLevels:
    """Verify the intended active-user and administrator policy tiers."""

    def test_power_configuration_is_admin_auth(self) -> None:
        tree = ET.parse(POLKIT_PATH)
        root = tree.getroot()
        action = next(
            item
            for item in root.findall("action")
            if item.get("id") == ACTION_CONFIGURE_POWER
        )
        defaults = action.find("defaults")
        assert defaults is not None
        assert defaults.findtext("allow_active") == "auth_admin"

    def test_charge_limit_is_active_user(self) -> None:
        """Charge limit writes must be allowed for active local users."""
        tree = ET.parse(POLKIT_PATH)
        root = tree.getroot()
        for action in root.findall("action"):
            if action.get("id") == ACTION_SET_CHARGE_LIMIT:
                defaults = action.find("defaults")
                assert defaults is not None
                allow_active = defaults.find("allow_active")
                assert allow_active is not None
                assert allow_active.text == "yes"

    def test_gpu_irq_is_admin_auth(self) -> None:
        """GPU IRQ writes must require admin authentication."""
        tree = ET.parse(POLKIT_PATH)
        root = tree.getroot()
        for action in root.findall("action"):
            if action.get("id") == ACTION_SET_GPU_IRQ:
                defaults = action.find("defaults")
                assert defaults is not None
                allow_active = defaults.find("allow_active")
                assert allow_active is not None
                assert allow_active.text == "auth_admin"

    def test_touchpad_firmware_is_admin_auth(self) -> None:
        tree = ET.parse(POLKIT_PATH)
        root = tree.getroot()
        action = next(
            item
            for item in root.findall("action")
            if item.get("id") == ACTION_SET_TOUCHPAD_FIRMWARE
        )
        assert action.find("defaults").findtext("allow_active") == "auth_admin"
        assert METHOD_ACTIONS["ApplyTouchpadSettings"] == ACTION_SET_TOUCHPAD_FIRMWARE
        assert METHOD_ACTIONS["SetTouchpadSetting"] == ACTION_SET_TOUCHPAD_FIRMWARE

    def test_reload_config_is_admin_auth(self) -> None:
        """Config reload must require admin authentication."""
        tree = ET.parse(POLKIT_PATH)
        root = tree.getroot()
        for action in root.findall("action"):
            if action.get("id") == ACTION_RELOAD_CONFIG:
                defaults = action.find("defaults")
                assert defaults is not None
                allow_active = defaults.find("allow_active")
                assert allow_active is not None
                assert allow_active.text == "auth_admin"


def _stat_line(pid: int, comm: str, starttime: int) -> str:
    """Build a realistic ``/proc/<pid>/stat`` line.

    Fields 3-21 are placeholders; ``starttime`` is field 22 (the value
    ``_parse_start_time`` must extract). A couple of trailing fields are
    appended for realism.
    """
    middle = " ".join(
        [
            "S",
            "1",
            str(pid),
            str(pid),
            "0",
            "-1",
            "4194304",
            "100",
            "0",
            "0",
            "0",
            "10",
            "5",
            "0",
            "0",
            "20",
            "0",
            "1",
            "0",
        ]
    )
    return f"{pid} ({comm}) {middle} {starttime} 1234567 200 18446744073709551615"


class TestParseStartTime:
    """M-2: the security-critical caller starttime parse must be robust."""

    def test_parses_normal_comm(self) -> None:
        assert _parse_start_time(_stat_line(1234, "bash", 98765)) == 98765

    def test_parses_comm_with_spaces(self) -> None:
        assert _parse_start_time(_stat_line(200, "Web Content", 4242)) == 4242

    def test_parses_comm_containing_close_paren(self) -> None:
        # A process name containing ')' must not shift the field index: the
        # parser splits from the right on the final ')'.
        assert _parse_start_time(_stat_line(5678, "x)y", 55500)) == 55500

    def test_empty_line_is_fail_closed(self) -> None:
        assert _parse_start_time("") == 0

    def test_truncated_line_is_fail_closed(self) -> None:
        assert _parse_start_time("1234 (bash) S 1 2") == 0

    def test_missing_comm_paren_is_fail_closed(self) -> None:
        assert _parse_start_time("1234 bash S 1 2 3 4 5") == 0

    def test_non_integer_starttime_is_fail_closed(self) -> None:
        line = "1 (x) S 1 1 1 0 -1 0 0 0 0 0 0 0 0 0 20 0 1 0 notanum 0 0"
        assert _parse_start_time(line) == 0
