"""Tests for GUI construction and state store (WP-13, WP-14).

Uses QT_QPA_PLATFORM=offscreen.  Verifies that pages can be constructed
from typed snapshots and that the state store emits only on change.
"""

from __future__ import annotations

import asyncio
import gc
import os
import threading
import time
from dataclasses import replace

import pytest

# Set offscreen before importing Qt.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import shiboken6  # noqa: E402
from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from honor_control.core.models import (  # noqa: E402
    BatterySnapshot,
    BatteryStatusKind,
    ChargeMode,
    FanMode,
    FanSnapshot,
    GestureEntry,
    GesturesSnapshot,
    OperationResult,
    PlatformInfo,
    PowerProfileEntry,
    PowerSnapshot,
    ServiceHealth,
    SystemSnapshot,
)
from honor_control.frontend.gui.state import GuiState  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    """Single QApplication for all GUI tests."""
    app = QApplication.instance() or QApplication([])
    yield app
    app.closeAllWindows()
    app.processEvents()
    shiboken6.delete(app)
    gc.collect()


class TestGuiState:
    """Verify the typed GUI state store."""

    def test_initial_state_is_empty(self, qapp) -> None:
        state = GuiState()
        assert state.snapshot is None
        assert state.connected is False

    def test_set_snapshot_emits_on_change(self, qapp) -> None:
        state = GuiState()
        received: list = []
        state.snapshot_changed.connect(lambda s: received.append(s))
        snap1 = SystemSnapshot(sequence=1)
        state.set_snapshot(snap1)
        assert len(received) == 1
        # Same sequence — no emit.
        state.set_snapshot(snap1)
        assert len(received) == 1
        # New sequence — emit.
        snap2 = SystemSnapshot(sequence=2)
        state.set_snapshot(snap2)
        assert len(received) == 2

    def test_older_snapshot_is_rejected_until_reconnect(self, qapp) -> None:
        state = GuiState()
        received: list[SystemSnapshot] = []
        state.snapshot_changed.connect(received.append)
        state.set_connected(True)
        newer = SystemSnapshot(sequence=8)
        older = SystemSnapshot(sequence=3)

        state.set_snapshot(newer)
        state.set_snapshot(older)
        assert state.snapshot == newer

        state.set_connected(False)
        state.set_connected(True)
        state.set_snapshot(older)
        assert state.snapshot == older
        assert received == [newer, older]

    def test_connection_changed_emits(self, qapp) -> None:
        state = GuiState()
        received: list[bool] = []
        state.connection_changed.connect(lambda c: received.append(c))
        state.set_connected(True)
        assert received == [True]
        state.set_connected(True)  # no change
        assert received == [True]
        state.set_connected(False)
        assert received == [True, False]

    def test_stale_domains_from_snapshot(self, qapp) -> None:
        state = GuiState()
        snap = SystemSnapshot(stale_domains=("battery",))
        state.set_snapshot(snap)
        assert state.stale_domains == ("battery",)


class TestGuiController:
    """Verify frontend transport defaults cover profile operations."""

    def test_gui_dbus_timeout_has_backend_margin(self, qapp) -> None:
        from honor_control.frontend.gui.controller import (
            GUI_DBUS_TIMEOUT_SECONDS,
            GuiController,
            GuiWorker,
        )

        worker = GuiWorker()
        controller = GuiController()
        # HC-004: the frontend deadline must sit above the backend's
        # interactive polkit deadline (~60s for a human) plus operation time.
        assert GUI_DBUS_TIMEOUT_SECONDS == 90.0
        assert GUI_DBUS_TIMEOUT_SECONDS > 60.0
        assert worker._timeout == GUI_DBUS_TIMEOUT_SECONDS  # noqa: SLF001
        assert controller._worker._timeout == GUI_DBUS_TIMEOUT_SECONDS  # noqa: SLF001

    def test_poll_timeout_preserves_online_state_and_later_snapshot(self, qapp) -> None:
        from honor_control.client.errors import ClientError
        from honor_control.core.errors import TransportError
        from honor_control.frontend.gui.controller import GuiWorker

        class TransientClient:
            connected = True

            def __init__(self) -> None:
                self.calls = 0

            async def get_snapshot(self) -> SystemSnapshot:
                self.calls += 1
                if self.calls == 1:
                    raise ClientError(TransportError.TIMEOUT, "temporary timeout")
                return SystemSnapshot(sequence=7)

        worker = GuiWorker()
        client = TransientClient()
        worker._client = client  # type: ignore[assignment]  # noqa: SLF001
        connection_events: list[bool] = []
        errors: list[str] = []
        snapshots: list[SystemSnapshot] = []
        worker.connection_changed.connect(connection_events.append)
        worker.error.connect(errors.append)
        worker.snapshot_ready.connect(snapshots.append)

        async def scenario() -> None:
            worker._fetch_and_emit()  # noqa: SLF001
            await asyncio.gather(*tuple(worker._tasks))  # noqa: SLF001
            worker._fetch_and_emit()  # noqa: SLF001
            await asyncio.gather(*tuple(worker._tasks))  # noqa: SLF001

        asyncio.run(scenario())

        assert client.connected is True
        assert connection_events == []
        assert [e.message for e in errors] == ["temporary timeout"]
        assert errors[0].retryable is True
        assert [snapshot.sequence for snapshot in snapshots] == [7]


class TestGuiPageConstruction:
    """Verify pages can be constructed and render snapshots."""

    def test_dashboard_page_construction(self, qapp) -> None:
        from honor_control.frontend.gui.pages.dashboard import DashboardPage

        state = GuiState()
        page = DashboardPage(state)
        snap = SystemSnapshot(
            sequence=1,
            platform=PlatformInfo(model="Test Model", matched=True),
            battery=BatterySnapshot(
                available=True,
                capacity_percent=75,
                status=BatteryStatusKind.CHARGING,
                ac_online=True,
                observed_end=90,
                observed_start=85,
                mode=ChargeMode.HOME,
            ),
            power=PowerSnapshot(available=True, applied_profile="balanced"),
            fan=FanSnapshot(available=True, temp_mc=45000, target_speed=30),
            service=ServiceHealth(overall="healthy", uptime=42),
        )
        page._on_snapshot(snap)
        assert page.profile_value.text() == "balanced"
        assert page.profile_combo.currentData() == "balanced"
        assert "75" in page.battery_value.text()

    def test_battery_page_construction(self, qapp) -> None:
        from honor_control.frontend.gui.pages.battery import BatteryPage

        state = GuiState()
        page = BatteryPage(state)
        snap = SystemSnapshot(
            battery=BatterySnapshot(
                available=True,
                capacity_percent=80,
                observed_end=90,
                observed_start=85,
                mode=ChargeMode.HOME,
            )
        )
        page._on_snapshot(snap)
        assert "80" in page.capacity_value.text()

    def test_battery_charge_modes_are_mutually_exclusive(self, qapp) -> None:
        from honor_control.frontend.gui.pages.battery import BatteryPage

        state = GuiState()
        state.set_connected(True)
        page = BatteryPage(state)

        page.mode_buttons["home"].click()
        page.mode_buttons["travel"].click()

        assert page.mode_buttons["travel"].isChecked()
        assert sum(button.isChecked() for button in page.mode_buttons.values()) == 1

    def test_power_page_construction(self, qapp) -> None:
        from honor_control.frontend.gui.pages.power import PowerPage

        state = GuiState()
        page = PowerPage(state)
        snap = SystemSnapshot(
            power=PowerSnapshot(available=True, applied_profile="balanced"),
            fan=FanSnapshot(temp_mc=45000, target_speed=30),
        )
        page._on_snapshot(snap)
        assert page.profile_combo.currentData() == "balanced"

    def test_power_editor_preserves_unsaved_values_during_refresh(self, qapp) -> None:
        from honor_control.frontend.gui.pages.power import PowerPage

        state = GuiState()
        page = PowerPage(state)
        profile = PowerProfileEntry(
            name="balanced",
            label="Balanced",
            pl1_uw=25_000_000,
            pl2_uw=35_000_000,
            built_in=True,
        )
        snap = SystemSnapshot(
            sequence=1,
            power=PowerSnapshot(
                applied_profile="balanced",
                desired_profile="balanced",
                profiles=(profile,),
                auto_switch_on_ac="balanced",
                auto_switch_on_battery="balanced",
            ),
        )
        page._on_snapshot(snap)
        page.pl1.setValue(31.0)
        page._on_snapshot(replace(snap, sequence=2))
        assert page.pl1.value() == 31.0
        assert page._profile_dirty is True  # noqa: SLF001

    def test_fan_page_construction(self, qapp) -> None:
        from honor_control.frontend.gui.pages.fan import FanPage

        state = GuiState()
        page = FanPage(state)
        snap = SystemSnapshot(
            fan=FanSnapshot(available=True, mode=FanMode.STOCK, temp_mc=45000),
        )
        page._on_snapshot(snap)
        assert page.rb_auto.isChecked()

    def test_fan_editor_survives_stock_refresh(self, qapp) -> None:
        from honor_control.frontend.gui.pages.fan import FanPage

        state = GuiState()
        page = FanPage(state)
        page.rb_curve.click()
        page.curve_graph.select(1)
        page.curve_graph.update_selected(65_000, 40)
        edited = page.curve_graph.points
        page._on_snapshot(
            SystemSnapshot(
                sequence=2,
                fan=FanSnapshot(available=True, mode=FanMode.STOCK, temp_mc=45_000),
            )
        )
        assert page.rb_curve.isChecked()
        assert not page.curve_widget.isHidden()
        assert page.curve_graph.points == edited

    def test_failed_fan_apply_reconciles_mode_to_snapshot(self, qapp) -> None:
        from honor_control.frontend.gui.pages.fan import FanPage

        state = GuiState()
        page = FanPage(state)
        state.set_snapshot(
            SystemSnapshot(
                sequence=1,
                fan=FanSnapshot(available=True, mode=FanMode.STOCK),
            )
        )
        page.rb_curve.click()
        assert page.rb_curve.isChecked()

        state.emit_completed(
            "set_curve:default",
            OperationResult.failed(code="write_failed", message="failed"),
        )

        assert page.rb_auto.isChecked()

    def test_saved_fan_curve_remains_selected_while_controller_applies(
        self, qapp
    ) -> None:
        from honor_control.frontend.gui.pages.fan import FanPage

        state = GuiState()
        page = FanPage(state)
        state.set_snapshot(
            SystemSnapshot(
                sequence=1,
                fan=FanSnapshot(available=True, mode=FanMode.STOCK),
            )
        )
        page.rb_curve.click()

        state.emit_completed(
            "set_curve:default",
            OperationResult.success(persisted=True, applied=False),
        )

        assert page.rb_curve.isChecked()

    def test_failed_power_apply_reconciles_selection_to_snapshot(self, qapp) -> None:
        from honor_control.frontend.gui.pages.power import PowerPage

        state = GuiState()
        page = PowerPage(state)
        profiles = (
            PowerProfileEntry(name="balanced", label="Balanced"),
            PowerProfileEntry(name="performance", label="Performance"),
        )
        state.set_snapshot(
            SystemSnapshot(
                sequence=1,
                power=PowerSnapshot(
                    applied_profile="balanced",
                    desired_profile="balanced",
                    profiles=profiles,
                ),
            )
        )
        page.profile_combo.setCurrentIndex(page.profile_combo.findData("performance"))
        assert page.profile_combo.currentData() == "performance"

        state.emit_completed(
            "set_profile:performance",
            OperationResult.failed(code="write_failed", message="failed"),
        )

        assert page.profile_combo.currentData() == "balanced"

    def test_failed_overview_apply_reconciles_selection_to_snapshot(self, qapp) -> None:
        from honor_control.frontend.gui.pages.dashboard import DashboardPage

        state = GuiState()
        page = DashboardPage(state)
        profiles = (
            PowerProfileEntry(name="balanced", label="Balanced"),
            PowerProfileEntry(name="performance", label="Performance"),
        )
        state.set_snapshot(
            SystemSnapshot(
                sequence=1,
                power=PowerSnapshot(
                    applied_profile="balanced",
                    desired_profile="balanced",
                    profiles=profiles,
                ),
            )
        )
        page.profile_combo.setCurrentIndex(page.profile_combo.findData("performance"))

        state.emit_completed(
            "set_profile:performance",
            OperationResult.failed(code="write_failed", message="failed"),
        )

        assert page.profile_combo.currentData() == "balanced"

    def test_power_profile_picker_also_loads_editor(self, qapp) -> None:
        from honor_control.frontend.gui.pages.power import PowerPage

        state = GuiState()
        page = PowerPage(state)
        profiles = (
            PowerProfileEntry(
                name="balanced",
                label="Balanced",
                pl1_uw=25_000_000,
                pl2_uw=35_000_000,
            ),
            PowerProfileEntry(
                name="custom",
                label="Custom",
                pl1_uw=30_000_000,
                pl2_uw=45_000_000,
                built_in=False,
            ),
        )
        page._on_snapshot(  # noqa: SLF001
            SystemSnapshot(
                power=PowerSnapshot(
                    applied_profile="balanced",
                    desired_profile="balanced",
                    profiles=profiles,
                )
            )
        )

        page.profile_combo.setCurrentIndex(page.profile_combo.findData("custom"))

        assert page.profile_name.text() == "custom"
        assert page.pl1.value() == 30.0
        assert page.pl2.value() == 45.0

    def test_pending_operation_disables_its_apply_control(self, qapp) -> None:
        from honor_control.frontend.gui.pages.battery import BatteryPage

        state = GuiState()
        state.set_connected(True)
        page = BatteryPage(state)

        state.mark_pending("set_mode:home")
        assert not page.mode_buttons["home"].isEnabled()
        state.emit_completed(
            "set_mode:home",
            OperationResult.success(applied=True),
        )
        assert page.mode_buttons["home"].isEnabled()

    def test_local_settings_remain_usable_without_backend(self, qapp) -> None:
        from honor_control.frontend.gui.pages.settings import SettingsPage

        page = SettingsPage(GuiState())
        page.set_backend_available(False)
        assert page.close_to_tray.isEnabled()

    def test_pending_signal_after_disconnect_does_not_disable_control_forever(self, qapp) -> None:
        from honor_control.frontend.gui.pages.battery import BatteryPage

        state = GuiState()
        page = BatteryPage(state)
        page.set_backend_available(False)
        state.mark_pending("set_mode:home")
        state.emit_completed("set_mode:home", OperationResult.success(applied=True))
        assert not page.mode_buttons["home"].isEnabled()
        page.set_backend_available(True)
        assert page.mode_buttons["home"].isEnabled()

    def test_pending_control_handles_disconnect_and_repeated_operations(self, qapp) -> None:
        from honor_control.frontend.gui.pages.battery import BatteryPage

        state = GuiState()
        state.set_connected(True)
        page = BatteryPage(state)

        page._update_pending_started("set_mode:home")  # noqa: SLF001
        page._update_pending_started("set_mode:travel")  # noqa: SLF001
        assert page.mode_buttons["home"].isEnabled() is False

        page.set_backend_available(False)
        page._update_pending_completed(  # noqa: SLF001
            "set_mode:home", None
        )
        page.set_backend_available(True)
        assert page.mode_buttons["home"].isEnabled() is False

        page._update_pending_completed(  # noqa: SLF001
            "set_mode:travel", None
        )
        assert page.mode_buttons["home"].isEnabled() is True

        # A later operation must capture the current enabled state rather than
        # reusing a stale baseline from the earlier pending operation.
        page._update_pending_started("set_mode:storage")  # noqa: SLF001
        page._update_pending_completed(  # noqa: SLF001
            "set_mode:storage", None
        )
        assert page.mode_buttons["home"].isEnabled() is True

    def test_dynamic_gesture_row_is_removed_from_pending_registry(self, qapp) -> None:
        from honor_control.frontend.gui.pages.touchpad import TouchpadPage

        state = GuiState()
        page = TouchpadPage(state)
        entry = GestureEntry(
            id="three_finger",
            label="Three finger",
            enabled=True,
            mapping="leftmeta,v",
            default_mapping="leftmeta,v",
        )
        page._on_snapshot(  # noqa: SLF001
            SystemSnapshot(gestures=GesturesSnapshot(mappings=(entry,)))
        )
        row = page._items[f"id:{entry.id}"]  # noqa: SLF001
        page._update_pending_started("set_mapping:three_finger:x")  # noqa: SLF001
        assert row.isEnabled() is False

        page._on_snapshot(SystemSnapshot(sequence=1))  # noqa: SLF001
        assert entry.id not in page._rows  # noqa: SLF001
        assert f"id:{entry.id}" not in page._items  # noqa: SLF001
        assert all(  # noqa: SLF001
            row not in widgets for widgets in page._pending_controls.values()
        )

    def test_battery_refresh_does_not_move_slider_during_drag(self, qapp) -> None:
        from honor_control.frontend.gui.pages.battery import BatteryPage

        state = GuiState()
        page = BatteryPage(state)
        page.slider.setValue(70)
        page.slider.setSliderDown(True)
        page._on_snapshot(  # noqa: SLF001
            SystemSnapshot(battery=BatterySnapshot(observed_end=90, observed_start=85))
        )
        assert page.slider.value() == 70
        page.slider.setSliderDown(False)

    def test_fan_add_point_preserves_curve_invariants(self, qapp) -> None:
        from honor_control.core.models import FanCurvePoint
        from honor_control.frontend.gui.fan_curve_editor import FanCurveGraph

        graph = FanCurveGraph()
        graph.set_points([FanCurvePoint(40_000, 0), FanCurvePoint(41_000, 10)])
        graph.add_point()
        assert [point.temp_mc for point in graph.points] == [40_000, 41_000]

        graph.set_points([FanCurvePoint(94_000, 50), FanCurvePoint(100_000, 100)])
        graph.add_point()
        added = graph.points[1]
        assert added.temp_mc == 97_000
        assert added.speed == 100

    def test_fan_curve_editor_supports_keyboard_editing(self, qapp) -> None:
        from honor_control.frontend.gui.fan_curve_editor import FanCurveGraph

        graph = FanCurveGraph()
        graph.select(1)
        before = graph.points[1].speed
        QTest.keyClick(graph, Qt.Key.Key_Up)
        assert graph.points[1].speed == before + 5
        assert graph.accessibleName() == "Fan curve editor"

    def test_diagnostics_page_construction(self, qapp) -> None:
        from honor_control.frontend.gui.pages.diagnostics import DiagnosticsPage

        state = GuiState()
        page = DiagnosticsPage(state)
        snap = SystemSnapshot(
            platform=PlatformInfo(model="Test", cpu_model="i5"),
            service=ServiceHealth(overall="healthy", uptime=10),
        )
        page._on_snapshot(snap)
        assert page.info_rows["model"].set_value is not None

    def test_experimental_gpu_capability_is_not_shown_as_an_error(self, qapp) -> None:
        from honor_control.core.errors import CapabilityStatus
        from honor_control.core.models import Capability
        from honor_control.frontend.gui.pages.diagnostics import DiagnosticsPage

        state = GuiState()
        page = DiagnosticsPage(state)
        page._on_snapshot(
            SystemSnapshot(
                capabilities={
                    "gpu": Capability(
                        status=CapabilityStatus.EXPERIMENTAL,
                        message="Restore is not yet supported",
                    )
                }
            )
        )

        assert page.cap_labels["gpu"].text() == "Experimental"
        assert page.cap_dots["gpu"].accessibleName() == "Status: warning"

    def test_diagnostics_can_save_loaded_logs(self, qapp, tmp_path) -> None:
        from honor_control.frontend.gui.pages.diagnostics import DiagnosticsPage

        page = DiagnosticsPage(GuiState())
        destination = tmp_path / "honor-control.log"
        page._save_logs(str(destination), ["first", "second"])  # noqa: SLF001

        assert destination.read_text(encoding="utf-8") == "first\nsecond\n"

    def test_settings_page_construction(self, qapp) -> None:
        from honor_control.frontend.gui.pages.settings import SettingsPage

        state = GuiState()
        page = SettingsPage(state)
        snap = SystemSnapshot(
            api_version=1,
            service=ServiceHealth(overall="healthy", uptime=100),
        )
        page._on_snapshot(snap)
        assert page.api_version_row is not None

    def test_gestures_page_construction(self, qapp) -> None:
        from honor_control.frontend.gui.pages.touchpad import TouchpadPage

        state = GuiState()
        page = TouchpadPage(state)
        snap = SystemSnapshot(
            gestures=GesturesSnapshot(
                firmware_settings_supported=True,
                firmware_settings={"edge_volume": 1, "sensitivity": 0},
            )
        )
        page._on_snapshot(snap)
        assert page._firmware_rows["sensitivity"].combo.currentData() == 0  # noqa: SLF001

    def test_touchpad_editor_batches_and_preserves_dirty_values(self, qapp) -> None:
        from honor_control.frontend.gui.pages.touchpad import TouchpadPage

        state = GuiState()
        state.set_connected(True)
        page = TouchpadPage(state)
        snap = SystemSnapshot(
            sequence=1,
            gestures=GesturesSnapshot(
                firmware_settings_supported=True,
                firmware_settings={"edge_volume": 1},
            ),
        )
        state.set_snapshot(snap)
        emitted: list[tuple[str, object]] = []
        page.intent.connect(lambda method, args: emitted.append((method, args)))

        row = page._firmware_rows["three_finger_drag"]  # noqa: SLF001
        row.combo.setCurrentIndex(row.combo.findData(1))
        page._on_snapshot(replace(snap, sequence=2))  # noqa: SLF001
        assert [c.summary for c in page.pending_changes()] == [
            "Touchpad behavior settings"
        ]
        page.apply_pending()

        assert row.combo.currentData() == 1
        assert emitted == [
            (
                "apply_touchpad_settings",
                ({"edge_volume": 1, "three_finger_drag": 1},),
            )
        ]

    def test_touchpad_editor_is_disabled_when_endpoint_is_unsupported(self, qapp) -> None:
        from honor_control.frontend.gui.pages.touchpad import TouchpadPage

        state = GuiState()
        state.set_connected(True)
        page = TouchpadPage(state)
        state.set_snapshot(
            SystemSnapshot(
                gestures=GesturesSnapshot(
                    firmware_settings_supported=False,
                    firmware_settings={"edge_volume": 1},
                )
            )
        )

        assert page.pending_changes() == []
        assert not page._firmware_rows["sensitivity"].combo.isEnabled()  # noqa: SLF001
        assert "unavailable" in page.firmware_summary.text().lower()

    def test_backend_reconnect_preserves_page_specific_disabled_controls(self, qapp) -> None:
        from honor_control.frontend.gui.pages.fan import FanPage
        from honor_control.frontend.gui.pages.power import PowerPage

        state = GuiState()
        state.set_connected(True)
        profiles = (
            PowerProfileEntry(name="balanced", label="Balanced", built_in=True),
        )
        power = PowerPage(state)
        state.set_snapshot(
            SystemSnapshot(
                power=PowerSnapshot(
                    applied_profile="balanced",
                    desired_profile="balanced",
                    profiles=profiles,
                )
            )
        )
        assert power.profile_name.isEnabled() is False

        fan = FanPage(state)
        state.set_snapshot(
            SystemSnapshot(
                sequence=2,
                fan=FanSnapshot(available=True, mode=FanMode.STOCK),
            )
        )
        assert fan.point_temp.isEnabled() is False

        for page in (power, fan):
            page.set_backend_available(False)
            page.set_backend_available(True)

        assert power.profile_name.isEnabled() is False
        assert fan.point_temp.isEnabled() is False

    def test_overview_quick_profile_action_preserves_controller_intent(
        self, qapp
    ) -> None:
        from honor_control.frontend.gui.pages.dashboard import DashboardPage

        state = GuiState()
        state.set_connected(True)
        page = DashboardPage(state)
        emitted: list[tuple[str, object]] = []
        page.intent.connect(lambda method, args: emitted.append((method, args)))

        page._on_snapshot(  # noqa: SLF001
            SystemSnapshot(
                power=PowerSnapshot(
                    applied_profile="balanced",
                    profiles=(
                        PowerProfileEntry(name="balanced", label="Balanced"),
                        PowerProfileEntry(name="performance", label="Performance"),
                    ),
                )
            )
        )
        page.profile_combo.setCurrentIndex(page.profile_combo.findData("performance"))
        assert emitted == []  # staged until the window's Apply bar
        assert [c.summary for c in page.pending_changes()] == [
            "Apply power profile: Performance"
        ]
        page.apply_pending()

        assert emitted == [("set_profile", ("performance",))]

    def test_toggle_switch_is_keyboard_operable(self, qapp) -> None:
        from honor_control.frontend.gui.widgets import ToggleSwitch

        toggle = ToggleSwitch(False)
        toggled: list[bool] = []
        toggle.toggled.connect(toggled.append)
        QTest.keyClick(toggle, Qt.Key.Key_Space)

        assert toggle.isChecked() is True
        assert toggled == [True]
        assert toggle.accessibleName()

    def test_wheel_over_value_control_scrolls_page_without_mutating(self, qapp) -> None:
        from PySide6.QtCore import QPoint, QPointF
        from PySide6.QtGui import QWheelEvent

        from honor_control.frontend.gui.design import WheelControlGuard
        from honor_control.frontend.gui.pages.power import PowerPage

        page = PowerPage(GuiState())
        page.resize(700, 260)
        page.show()
        qapp.processEvents()
        scroll_bar = page._scroll.verticalScrollBar()  # noqa: SLF001
        scroll_bar.setValue(min(20, scroll_bar.maximum()))
        before_scroll = scroll_bar.value()
        before_value = page.pl1.value()
        event = QWheelEvent(
            QPointF(2, 2),
            QPointF(2, 2),
            QPoint(),
            QPoint(0, -120),
            Qt.MouseButton.NoButton,
            Qt.KeyboardModifier.NoModifier,
            Qt.ScrollPhase.ScrollUpdate,
            False,
        )

        handled = WheelControlGuard().eventFilter(page.pl1, event)

        assert handled is True
        assert page.pl1.value() == before_value
        assert scroll_bar.value() > before_scroll


class TestLocalSystemProbe:
    def test_collects_neofetch_style_specs_from_linux_files(self, tmp_path) -> None:
        from honor_control.frontend.gui.system_info import probe_local_system

        proc_root = tmp_path / "proc"
        etc_root = tmp_path / "etc"
        sys_root = tmp_path / "sys"
        proc_root.mkdir()
        etc_root.mkdir()
        (proc_root / "meminfo").write_text(
            "MemTotal:       8388608 kB\nMemAvailable:   2097152 kB\n",
            encoding="utf-8",
        )
        (etc_root / "os-release").write_text(
            'PRETTY_NAME="Test Linux 1"\n',
            encoding="utf-8",
        )
        gpu = sys_root / "class/drm/card0/device"
        hwmon = gpu / "hwmon/hwmon0"
        hwmon.mkdir(parents=True)
        (gpu / "uevent").write_text(
            "PCI_ID=8086:7D55\nDRIVER=i915\n",
            encoding="utf-8",
        )
        (hwmon / "temp1_input").write_text("56000\n", encoding="utf-8")
        battery = sys_root / "class/power_supply/BAT0"
        battery.mkdir(parents=True)
        (battery / "energy_full").write_text("54000000\n", encoding="utf-8")
        (battery / "energy_full_design").write_text("60000000\n", encoding="utf-8")
        (battery / "cycle_count").write_text("123\n", encoding="utf-8")

        info = probe_local_system(
            proc_root=proc_root,
            sys_root=sys_root,
            etc_root=etc_root,
            environ={
                "XDG_CURRENT_DESKTOP": "KDE",
                "XDG_SESSION_TYPE": "wayland",
            },
        )

        assert info.os_name == "Test Linux 1"
        assert info.desktop == "KDE · wayland"
        assert info.memory == "6.0 / 8.0 GiB used"
        assert info.graphics == "Intel integrated graphics · PCI 8086:7d55 · i915"
        assert info.gpu_temperature == "56 °C"
        assert info.battery_health == "90% of design capacity"
        assert info.battery_cycles == "123"


class TestIntegratedTray:
    def test_shared_controller_owns_no_duplicate_refresh_loop(
        self, qapp, monkeypatch
    ) -> None:
        from honor_control.frontend.gui.controller import GuiController
        from honor_control.frontend.tray.tray import HonorTray

        controller = GuiController()
        refreshes: list[bool] = []
        monkeypatch.setattr(controller, "refresh", lambda: refreshes.append(True))

        tray = HonorTray(controller=controller)
        tray._on_operation("test", None)  # noqa: SLF001

        assert tray._timer.isActive() is False  # noqa: SLF001
        assert refreshes == []
        tray.shutdown()

    def test_left_click_uses_existing_window_callback(self, qapp) -> None:
        from PySide6.QtWidgets import QSystemTrayIcon

        from honor_control.frontend.gui.controller import GuiController
        from honor_control.frontend.tray.tray import HonorTray

        opened: list[bool] = []
        controller = GuiController()
        tray = HonorTray(
            controller=controller,
            open_window=lambda: opened.append(True),
        )
        tray._on_activated(QSystemTrayIcon.ActivationReason.Trigger)  # noqa: SLF001
        tray.shutdown()
        assert opened == [True]

    def test_standalone_gui_launch_runs_off_main_thread(
        self, qapp, monkeypatch
    ) -> None:
        from honor_control.frontend.gui.controller import GuiController
        from honor_control.frontend.tray.tray import HonorTray

        main_thread = threading.get_ident()
        spawned_from: list[int] = []
        spawned = threading.Event()
        quit_requested = threading.Event()
        quit_requested_from: list[int] = []

        def request_quit() -> None:
            quit_requested_from.append(threading.get_ident())
            quit_requested.set()

        def fake_popen(*_args, **_kwargs):
            spawned_from.append(threading.get_ident())
            spawned.set()
            return object()

        monkeypatch.setattr(
            "honor_control.frontend.tray.tray.subprocess.Popen", fake_popen
        )
        tray = HonorTray(
            controller=GuiController(),
            quit_application=request_quit,
        )
        tray._open_gui()  # noqa: SLF001

        assert spawned.wait(timeout=1)
        deadline = time.monotonic() + 1
        while not quit_requested.is_set() and time.monotonic() < deadline:
            qapp.processEvents()
            time.sleep(0.01)

        assert spawned_from and spawned_from[0] != main_thread
        assert quit_requested.is_set()
        assert quit_requested_from == [main_thread]
        tray.shutdown()

    def test_touchpad_shortcuts_follow_snapshot(self, qapp) -> None:
        from honor_control.frontend.gui.controller import GuiController
        from honor_control.frontend.tray.tray import HonorTray

        tray = HonorTray(controller=GuiController())
        tray._on_snapshot(  # noqa: SLF001
            SystemSnapshot(
                gestures=GesturesSnapshot(
                    firmware_settings_supported=True,
                    firmware_settings={"edge_volume": 1},
                )
            )
        )
        assert tray.touchpad_actions["edge_volume"].isChecked()
        assert not tray.touchpad_actions["edge_brightness"].isChecked()
        tray.shutdown()

    def test_window_close_hides_to_available_tray(self, qapp, monkeypatch) -> None:
        from honor_control.frontend.gui.controller import GuiController
        from honor_control.frontend.gui.main_window import MainWindow
        from honor_control.frontend.tray.tray import HonorTray

        monkeypatch.setattr(GuiController, "start", lambda _self: None)
        monkeypatch.setattr(GuiController, "stop", lambda _self: None)
        monkeypatch.setattr(HonorTray, "available", property(lambda _self: True))
        window = MainWindow(bus_kind="session")
        window._close_to_tray = True  # noqa: SLF001
        window.show()
        window.close()
        qapp.processEvents()
        assert window.isHidden()
        assert window._shutdown is False  # noqa: SLF001
        window._shutdown_application()  # noqa: SLF001

    def test_tray_notice_shown_once_per_version(
        self, qapp, monkeypatch, tmp_path
    ) -> None:
        from PySide6.QtCore import QSettings

        from honor_control import __version__
        from honor_control.frontend.gui.controller import GuiController
        from honor_control.frontend.gui.main_window import MainWindow
        from honor_control.frontend.tray.tray import HonorTray

        monkeypatch.setattr(GuiController, "start", lambda _self: None)
        monkeypatch.setattr(GuiController, "stop", lambda _self: None)
        monkeypatch.setattr(HonorTray, "available", property(lambda _self: True))
        shown: list[str] = []
        monkeypatch.setattr(
            "PySide6.QtWidgets.QSystemTrayIcon.showMessage",
            lambda _self, title, *_a: shown.append(title),
        )
        QSettings.setDefaultFormat(QSettings.Format.IniFormat)
        QSettings.setPath(
            QSettings.Format.IniFormat, QSettings.Scope.UserScope, str(tmp_path)
        )
        QSettings().remove("tray/notice_version")

        def close_once() -> None:
            window = MainWindow(bus_kind="session")
            window._close_to_tray = True  # noqa: SLF001
            window.show()
            window.close()
            qapp.processEvents()
            window._shutdown_application()  # noqa: SLF001

        close_once()
        close_once()
        assert len(shown) == 1
        assert QSettings().value("tray/notice_version") == __version__
        QSettings().setValue("tray/notice_version", "older")
        close_once()
        assert len(shown) == 2
        QSettings().remove("tray/notice_version")

    def test_window_polls_only_while_foreground(self, qapp, monkeypatch) -> None:
        from honor_control.frontend.gui.controller import GuiController
        from honor_control.frontend.gui.main_window import (
            FOREGROUND_LEASE_SECONDS,
            MainWindow,
        )

        leases: list[int] = []
        refreshes: list[int] = []
        monkeypatch.setattr(GuiController, "start", lambda _self: None)
        monkeypatch.setattr(GuiController, "stop", lambda _self: None)
        monkeypatch.setattr(
            GuiController,
            "request_fast_refresh",
            lambda _self, seconds: leases.append(seconds),
        )
        monkeypatch.setattr(GuiController, "refresh", lambda _self: refreshes.append(1))
        foreground = {"value": False}
        monkeypatch.setattr(
            MainWindow, "_is_foreground", lambda _self: foreground["value"]
        )
        window = MainWindow(bus_kind="session")
        window._update_polling()  # noqa: SLF001
        assert window._timer.isActive() is False  # noqa: SLF001
        assert leases == []

        foreground["value"] = True
        window._update_polling()  # noqa: SLF001
        assert window._timer.isActive() is True  # noqa: SLF001
        assert leases == [FOREGROUND_LEASE_SECONDS]
        assert refreshes == [1]

        foreground["value"] = False
        window._update_polling()  # noqa: SLF001
        assert window._timer.isActive() is False  # noqa: SLF001
        assert leases == [FOREGROUND_LEASE_SECONDS, 0]
        window._shutdown_application()  # noqa: SLF001

    def test_window_shell_compacts_at_supported_narrow_width(
        self, qapp, monkeypatch
    ) -> None:
        from honor_control.frontend.gui.controller import GuiController
        from honor_control.frontend.gui.main_window import MainWindow

        monkeypatch.setattr(GuiController, "start", lambda _self: None)
        monkeypatch.setattr(GuiController, "stop", lambda _self: None)
        window = MainWindow(bus_kind="session")
        window.show()
        window.resize(760, 520)
        qapp.processEvents()

        assert window.sidebar_frame.width() == 136
        assert window.device_label.isHidden()

        window.resize(900, 600)
        qapp.processEvents()
        assert window.sidebar_frame.width() == 176
        assert not window.device_label.isHidden()
        window._shutdown_application()  # noqa: SLF001


def test_single_instance_lock_rejects_duplicate(qapp) -> None:
    from honor_control.frontend.single_instance import acquire_single_instance

    name = f"test-{os.getpid()}-{time.monotonic_ns()}"
    first = acquire_single_instance(name)
    assert first is not None
    try:
        assert acquire_single_instance(name) is None
    finally:
        first.unlock()


def test_protocol_mismatch_uses_persistent_banner(qapp, monkeypatch) -> None:
    from honor_control.frontend.gui.controller import GuiController
    from honor_control.frontend.gui.main_window import MainWindow

    monkeypatch.setattr(GuiController, "start", lambda _self: None)
    monkeypatch.setattr(GuiController, "stop", lambda _self: None)
    window = MainWindow(bus_kind="session")
    window.controller.protocol_mismatch.emit("schema mismatch")
    assert window.banner.isVisibleTo(window)
    assert "Incompatible service version" in window.banner_label.text()
    window._shutdown_application()  # noqa: SLF001
