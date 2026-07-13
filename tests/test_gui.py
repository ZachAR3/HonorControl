"""Tests for GUI construction and state store (WP-13, WP-14).

Uses QT_QPA_PLATFORM=offscreen.  Verifies that pages can be constructed
from typed snapshots and that the state store emits only on change.
"""

from __future__ import annotations

import os
from dataclasses import replace

import pytest

# Set offscreen before importing Qt.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from honor_control.core.models import (  # noqa: E402
    BatterySnapshot,
    BatteryStatusKind,
    ChargeMode,
    FanMode,
    FanSnapshot,
    GesturesSnapshot,
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
        assert GUI_DBUS_TIMEOUT_SECONDS == 15.0
        assert worker._timeout == GUI_DBUS_TIMEOUT_SECONDS  # noqa: SLF001
        assert controller._worker._timeout == GUI_DBUS_TIMEOUT_SECONDS  # noqa: SLF001


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

    def test_power_page_construction(self, qapp) -> None:
        from honor_control.frontend.gui.pages.power import PowerPage

        state = GuiState()
        page = PowerPage(state)
        snap = SystemSnapshot(
            power=PowerSnapshot(available=True, applied_profile="balanced"),
            fan=FanSnapshot(temp_mc=45000, target_speed=30),
        )
        page._on_snapshot(snap)
        assert page.active_combo.currentData() == "balanced"

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
        assert page._firmware_rows["edge_volume"].combo.currentData() == 1  # noqa: SLF001
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
        page.apply_button.click()

        assert row.combo.currentData() == 1
        assert emitted == [
            (
                "apply_touchpad_settings",
                ({"edge_volume": 1, "three_finger_drag": 1},),
            )
        ]


class TestIntegratedTray:
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
        monkeypatch.setattr(
            HonorTray, "available", property(lambda _self: True)
        )
        window = MainWindow(bus_kind="session")
        window._close_to_tray = True  # noqa: SLF001
        window.show()
        window.close()
        qapp.processEvents()
        assert window.isHidden()
        assert window._shutdown is False  # noqa: SLF001
        window._shutdown_application()  # noqa: SLF001
