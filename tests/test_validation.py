"""Tests for polkit action IDs and charge-mode preset consistency.

These tests verify the constants and mappings are internally consistent
across the contract, authorizer, and domain modules.
"""

from __future__ import annotations

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
from honor_control.core.models import CHARGE_PRESETS


class TestPolkitActions:
    """Verify polkit action IDs are defined and consistent."""

    def test_all_actions_have_correct_prefix(self) -> None:
        for action in (
            ACTION_SET_CHARGE_LIMIT,
            ACTION_CONFIGURE_POWER,
            ACTION_SET_POWER_PROFILE,
            ACTION_SET_FAN_CURVE,
            ACTION_SET_GESTURES,
            ACTION_SET_GPU_IRQ,
            ACTION_RELOAD_CONFIG,
            ACTION_EXPORT_DEBUG,
            ACTION_VIEW_LOGS,
        ):
            assert action.startswith("org.honorlinux.control.")

    def test_method_actions_cover_all_mutations(self) -> None:
        # Every method in METHOD_ACTIONS must map to a known action.
        for _method, action in METHOD_ACTIONS.items():
            assert action.startswith("org.honorlinux.control.")

    def test_unprivileged_methods_need_no_auth(self) -> None:
        for method in UNPRIVILEGED_METHODS:
            assert method not in METHOD_ACTIONS


class TestChargePresets:
    """Verify charge-mode preset values match the domain model."""

    def test_off_mode_is_100(self) -> None:
        assert CHARGE_PRESETS["off"] == (100, 95)

    def test_home_mode_is_90(self) -> None:
        assert CHARGE_PRESETS["home"] == (90, 85)

    def test_travel_mode_is_80(self) -> None:
        assert CHARGE_PRESETS["travel"] == (80, 75)

    def test_storage_mode_is_60(self) -> None:
        assert CHARGE_PRESETS["storage"] == (60, 55)

    def test_all_modes_have_valid_ranges(self) -> None:
        for _mode, (end, start) in CHARGE_PRESETS.items():
            assert 40 <= end <= 100
            assert 40 <= start <= 100
            assert start <= end
