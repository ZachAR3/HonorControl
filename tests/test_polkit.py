"""Tests for polkit policy/code/action consistency (WP-05, criterion 3)."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

from honor_control.backend.dbus.authorizer import (
    ACTION_CONFIGURE_POWER,
    ACTION_EXPORT_DEBUG,
    ACTION_RELOAD_CONFIG,
    ACTION_SET_CHARGE_LIMIT,
    ACTION_SET_FAN_CURVE,
    ACTION_SET_GESTURES,
    ACTION_SET_GPU_IRQ,
    ACTION_SET_POWER_PROFILE,
    ACTION_VIEW_LOGS,
    METHOD_ACTIONS,
    UNPRIVILEGED_METHODS,
)

#: All known action IDs defined in the code.
KNOWN_ACTIONS = frozenset(
    {
        ACTION_SET_CHARGE_LIMIT,
        ACTION_CONFIGURE_POWER,
        ACTION_SET_POWER_PROFILE,
        ACTION_SET_FAN_CURVE,
        ACTION_SET_GESTURES,
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

    def test_power_configuration_is_admin_auth(self) -> None:
        tree = ET.parse(POLKIT_PATH)
        root = tree.getroot()
        action = next(
            item for item in root.findall("action")
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
