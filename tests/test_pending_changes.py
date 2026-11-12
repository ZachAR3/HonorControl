"""The window's single Apply/Discard bar and the pages' staged edits."""

from __future__ import annotations

import os
from dataclasses import replace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from honor_control.core.models import (  # noqa: E402
    BatterySnapshot,
    ChargeMode,
    FanMode,
    FanSnapshot,
    GestureEntry,
    GesturesSnapshot,
    OperationResult,
    PowerProfileEntry,
    PowerSnapshot,
    SystemSnapshot,
)
from honor_control.frontend.gui.controller import GuiController  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def _entry(gesture_id: str, label: str, mapping: str) -> GestureEntry:
    return GestureEntry(
        id=gesture_id,
        label=label,
        enabled=True,
        mapping=mapping,
        default_mapping=mapping,
    )


def _snapshot(sequence: int = 1, **overrides) -> SystemSnapshot:
    base = SystemSnapshot(
        sequence=sequence,
        battery=BatterySnapshot(
            available=True, observed_end=80, observed_start=75, mode=ChargeMode.HOME
        ),
        fan=FanSnapshot(available=True, mode=FanMode.STOCK),
        power=PowerSnapshot(
            applied_profile="balanced",
            desired_profile="balanced",
            profiles=(
                PowerProfileEntry(name="balanced", label="Balanced", built_in=True),
                PowerProfileEntry(
                    name="performance", label="Performance", built_in=True
                ),
            ),
        ),
        gestures=GesturesSnapshot(
            firmware_settings_supported=True,
            mappings=(
                _entry("3:1", "Brightness up", "a"),
                _entry("3:2", "Brightness down", "b"),
            ),
        ),
    )
    return replace(base, **overrides)


@pytest.fixture
def window(qapp, monkeypatch):
    from honor_control.frontend.gui.main_window import MainWindow
    from honor_control.frontend.tray.tray import HonorTray

    monkeypatch.setenv("HONOR_CONTROL_DISABLE_DISPLAY_POLICY", "1")
    monkeypatch.setattr(GuiController, "start", lambda _self: None)
    monkeypatch.setattr(GuiController, "stop", lambda _self: None)
    monkeypatch.setattr(HonorTray, "available", property(lambda _self: True))
    calls: list[tuple[str, tuple]] = []
    monkeypatch.setattr(
        GuiController,
        "call",
        lambda _self, _op, method, *args: calls.append((method, args)) or True,
    )
    win = MainWindow(bus_kind="session")
    win.calls = calls
    win.state.set_connected(True)
    win.state.set_snapshot(_snapshot())
    qapp.processEvents()
    yield win
    win._shutdown_application()  # noqa: SLF001


def _page(window, name: str):
    return next(
        page
        for page in window._pages  # noqa: SLF001
        if type(page).__name__ == f"{name}Page"
    )


class TestActionBar:
    def test_starts_grayed_out(self, window) -> None:
        assert window.changes_label.text() == "No unsaved changes"
        assert not window.apply_button.isEnabled()
        assert not window.discard_button.isEnabled()

    def test_lights_up_for_an_edit_on_any_page_and_goes_dark_after_discard(
        self, window
    ) -> None:
        battery = _page(window, "Battery")
        battery.slider.setValue(60)
        assert window.apply_button.isEnabled() and window.discard_button.isEnabled()
        assert "1 unsaved change" in window.changes_label.text()
        assert "Charge limit: 60%" in window.changes_label.text()

        window.discard_button.click()
        assert not window.apply_button.isEnabled()
        assert window.changes_label.text() == "No unsaved changes"
        assert battery.slider.value() == 80  # back to the real value

    def test_one_apply_sends_every_page_s_edits(self, window) -> None:
        _page(window, "Battery").slider.setValue(60)
        fan = _page(window, "Fan")
        fan.rb_manual.click()
        fan.manual_slider.setValue(70)
        assert "2 unsaved changes" in window.changes_label.text()

        window.apply_button.click()
        assert ("set_thresholds", (60, 55)) in window.calls
        assert ("set_manual", (70, 300)) in window.calls

    def test_apply_waits_while_an_operation_is_in_flight(self, window) -> None:
        _page(window, "Battery").slider.setValue(60)
        window.state.mark_pending("set_thresholds:60:55")
        assert not window.apply_button.isEnabled()
        window.state.emit_completed(
            "set_thresholds:60:55", OperationResult.success(applied=True)
        )
        # The staged value was accepted, so there is nothing left to apply.
        assert not window.apply_button.isEnabled()

    def test_service_edits_cannot_be_applied_while_offline(self, window) -> None:
        _page(window, "Battery").slider.setValue(60)
        window.state.set_connected(False)
        assert not window.apply_button.isEnabled()
        assert "service offline" in window.changes_label.text()
        window.state.set_connected(True)
        assert window.apply_button.isEnabled()

    def test_ctrl_s_applies_only_when_there_is_something_to_apply(self, window) -> None:
        window._apply_all()  # noqa: SLF001 - nothing staged: harmless
        assert window.calls == []
        _page(window, "Battery").slider.setValue(60)
        window._apply_all()  # noqa: SLF001
        assert window.calls == [("set_thresholds", (60, 55))]


class TestBatteryStaging:
    def test_preset_is_staged_not_applied(self, window) -> None:
        battery = _page(window, "Battery")
        battery.mode_buttons["travel"].click()
        assert window.calls == []
        assert [c.summary for c in battery.pending_changes()] == ["Charge mode: Travel"]
        window.apply_button.click()
        assert window.calls == [("set_mode", ("travel",))]

    def test_choosing_the_current_mode_is_not_a_change(self, window) -> None:
        _page(window, "Battery").mode_buttons["home"].click()
        assert not window.apply_button.isEnabled()

    def test_a_custom_limit_replaces_a_staged_preset(self, window) -> None:
        battery = _page(window, "Battery")
        battery.mode_buttons["travel"].click()
        battery.slider.setValue(65)
        assert not any(b.isChecked() for b in battery.mode_buttons.values())
        window.apply_button.click()
        assert window.calls == [("set_thresholds", (65, 60))]

    def test_a_refresh_does_not_overwrite_the_unapplied_slider(self, window) -> None:
        battery = _page(window, "Battery")
        battery.slider.setValue(60)
        window.state.set_snapshot(_snapshot(sequence=2))
        assert battery.slider.value() == 60
        assert battery.pending_changes()


class TestFanStaging:
    def test_stock_auto_is_no_longer_applied_by_the_click(self, window) -> None:
        window.state.set_snapshot(
            _snapshot(sequence=2, fan=FanSnapshot(available=True, mode=FanMode.CURVE))
        )
        fan = _page(window, "Fan")
        fan.rb_auto.click()
        assert window.calls == []
        assert [c.summary for c in fan.pending_changes()] == ["Fan mode: Stock auto"]
        window.apply_button.click()
        assert window.calls == [("set_stock_auto", ())]

    def test_selecting_the_active_mode_is_not_a_change(self, window) -> None:
        _page(window, "Fan").rb_auto.click()
        assert not window.apply_button.isEnabled()

    def test_discard_returns_to_the_real_mode(self, window) -> None:
        fan = _page(window, "Fan")
        fan.rb_manual.click()
        assert window.apply_button.isEnabled()
        window.discard_button.click()
        assert fan.rb_auto.isChecked()
        assert not window.apply_button.isEnabled()


class TestTouchpadStaging:
    def test_shortcut_and_enabled_edits_are_staged_per_row(self, window) -> None:
        touchpad = _page(window, "Touchpad")
        row = touchpad._rows["3:1"]  # noqa: SLF001
        row.mapping.setText("leftmeta,v")
        touchpad._rows["3:2"].enabled_box.setChecked(False)  # noqa: SLF001
        assert window.calls == []
        assert "2 edited" in window.changes_label.text()

        window.apply_button.click()
        assert ("set_mapping", ("3:1", "leftmeta,v")) in window.calls
        assert ("set_enabled", ("3:2", False)) in window.calls

    def test_a_refresh_keeps_an_unapplied_shortcut(self, window) -> None:
        touchpad = _page(window, "Touchpad")
        row = touchpad._rows["3:1"]  # noqa: SLF001
        row.mapping.setText("typed")
        window.state.set_snapshot(_snapshot(sequence=2))
        assert row.mapping.text() == "typed"

    def test_an_edit_the_service_confirms_stops_being_pending(self, window) -> None:
        touchpad = _page(window, "Touchpad")
        row = touchpad._rows["3:1"]  # noqa: SLF001
        row.mapping.setText("new")
        confirmed = _snapshot(
            sequence=2,
            gestures=GesturesSnapshot(
                firmware_settings_supported=True,
                mappings=(
                    _entry("3:1", "Brightness up", "new"),
                    _entry("3:2", "Brightness down", "b"),
                ),
            ),
        )
        window.state.set_snapshot(confirmed)
        assert touchpad.pending_changes() == []

    def test_enable_all_stages_every_row(self, window) -> None:
        touchpad = _page(window, "Touchpad")
        for row in touchpad._rows.values():  # noqa: SLF001
            row.set_enabled(False)
        window.discard_button.click()
        assert all(r.enabled_box.isChecked() for r in touchpad._rows.values())  # noqa: SLF001

    def test_discard_restores_firmware_and_shortcut_edits(self, window) -> None:
        touchpad = _page(window, "Touchpad")
        touchpad._rows["3:1"].mapping.setText("x")  # noqa: SLF001
        window.discard_button.click()
        assert touchpad._rows["3:1"].mapping.text() == "a"  # noqa: SLF001


class TestPowerStaging:
    def test_automatic_switching_edits_are_staged_then_applied(self, window) -> None:
        power = _page(window, "Power")
        power.auto_enabled.setChecked(True)
        assert window.calls == []
        assert any(
            c.summary == "Automatic AC / battery switching"
            for c in power.pending_changes()
        )
        window.apply_button.click()
        assert [call[0] for call in window.calls] == ["configure_auto_switch"]

    def test_profile_selection_is_staged(self, window) -> None:
        power = _page(window, "Power")
        power.profile_combo.setCurrentIndex(power.profile_combo.findData("performance"))
        summaries = [c.summary for c in power.pending_changes()]
        assert "Apply power profile: Performance" in summaries
        window.apply_button.click()
        assert ("set_profile", ("performance",)) in window.calls

    def test_discard_reselects_the_applied_profile(self, window) -> None:
        power = _page(window, "Power")
        power.profile_combo.setCurrentIndex(power.profile_combo.findData("performance"))
        window.discard_button.click()
        assert power.profile_combo.currentData() == "balanced"
        assert power.pending_changes() == []

    def test_no_scattered_save_buttons_remain(self, window) -> None:
        for name, attrs in (
            ("Power", ("profile_save", "profile_apply", "auto_save")),
            ("Battery", ("apply_btn",)),
            ("Fan", ("manual_apply", "curve_apply")),
            ("Touchpad", ("apply_button", "discard_button")),
            ("Dashboard", ("profile_apply",)),
        ):
            page = _page(window, name)
            for attr in attrs:
                assert not hasattr(page, attr), f"{name}.{attr} should be gone"
