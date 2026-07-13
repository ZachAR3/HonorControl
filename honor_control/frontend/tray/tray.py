"""System-tray frontend (QSystemTrayIcon).

Snapshot-driven menu with one background transport.  Disables mutations
while offline or pending.  Gesture master toggle calls one atomic
``SetAllEnabled``.  Notifications use structured result messages.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable

from PySide6.QtCore import QTimer
from PySide6.QtGui import QAction, QActionGroup, QIcon
from PySide6.QtWidgets import QApplication, QMenu, QMessageBox, QSystemTrayIcon

from honor_control import __version__
from honor_control.core.models import POWER_PROFILES, SystemSnapshot
from honor_control.frontend.gui.controller import GuiController

#: Refresh interval (ms) for the tooltip + menu state.
TRAY_REFRESH_MS = 5000


def _load_icon() -> QIcon:
    for name in ("preferences-system", "computer", "battery"):
        icon = QIcon.fromTheme(name)
        if not icon.isNull():
            return icon
    from PySide6.QtWidgets import QStyle

    app = QApplication.instance()
    style = app.style() if app else None
    if style is not None:
        return style.standardIcon(QStyle.StandardPixmap.SP_ComputerIcon)
    return QIcon()


class HonorTray:
    """Tray icon + context-menu controller."""

    def __init__(
        self,
        bus_kind: str = "system",
        *,
        controller: GuiController | None = None,
        open_window: Callable[[], None] | None = None,
        quit_application: Callable[[], None] | None = None,
    ) -> None:
        self._owns_controller = controller is None
        self.controller = controller or GuiController(bus_kind=bus_kind)
        self._open_window = open_window
        self._quit_application = quit_application
        self._profiles: list[tuple[str, str]] = []

        self.tray = QSystemTrayIcon(_load_icon())
        self.tray.setToolTip("Honor Control")
        self.tray.setVisible(True)
        self._build_menu()
        self.tray.activated.connect(self._on_activated)
        self.controller.snapshot_received.connect(self._on_snapshot)
        self.controller.connection_changed.connect(self._set_online)
        self.controller.error.connect(self._notify)
        self.controller.operation_completed.connect(self._on_operation)
        self._set_online(False)
        if self._owns_controller:
            self.controller.start()

        self._timer = QTimer()
        self._timer.setInterval(TRAY_REFRESH_MS)
        self._timer.timeout.connect(self.refresh)
        self._timer.start()
        self.refresh()

    def _build_menu(self) -> None:
        self.menu = QMenu("Honor Control")
        self.menu.addAction("Open Control Center").triggered.connect(self._open_gui)
        self.menu.addSeparator()

        self.profile_menu: QMenu = self.menu.addMenu("Power profile")
        self.profile_group = QActionGroup(self.profile_menu)
        self.profile_group.setExclusive(True)
        self.profile_actions: dict[str, QAction] = {}

        self.battery_menu: QMenu = self.menu.addMenu("Charge limit")
        self.battery_group = QActionGroup(self.battery_menu)
        self.battery_group.setExclusive(True)
        self.battery_actions: dict[str, QAction] = {}
        for mode in ("off", "home", "travel", "storage"):
            act = QAction(mode.capitalize(), self.battery_menu, checkable=True)
            act.triggered.connect(
                lambda _checked=False, m=mode: self._set_charge_mode(m)
            )
            self.battery_group.addAction(act)
            self.battery_menu.addAction(act)
            self.battery_actions[mode] = act

        self.gestures_action = QAction(
            "Gesture dispatch daemon", self.menu, checkable=True
        )
        self.gestures_action.setChecked(False)
        self.gestures_action.toggled.connect(self._toggle_gesture_daemon)
        self.menu.addAction(self.gestures_action)

        self.touchpad_menu: QMenu = self.menu.addMenu("Touchpad shortcuts")
        self.touchpad_actions: dict[str, QAction] = {}
        for setting, label in (
            ("edge_volume", "Edge volume"),
            ("edge_brightness", "Edge brightness"),
            ("three_finger_drag", "Three-finger drag"),
            ("mouse_like_mode", "Mouse-like mode"),
        ):
            action = QAction(label, self.touchpad_menu, checkable=True)
            action.toggled.connect(
                lambda enabled, name=setting: self._set_touchpad_setting(name, enabled)
            )
            self.touchpad_menu.addAction(action)
            self.touchpad_actions[setting] = action

        self.menu.addSeparator()
        self.menu.addAction("Quit").triggered.connect(self._quit)
        self.tray.setContextMenu(self.menu)

    def refresh(self) -> None:
        """Request backend state without blocking the Qt main thread."""
        self.controller.refresh()

    def _on_snapshot(self, snap: SystemSnapshot) -> None:
        profile = snap.power.applied_profile or "unknown"
        self.tray.setToolTip(f"Honor Control — {profile} · seq {snap.sequence}")
        profile_entries = snap.power.profiles or POWER_PROFILES
        profiles = tuple(entry.name for entry in profile_entries)
        labels = {entry.name: entry.label for entry in profile_entries}
        profile_state = [(name, labels.get(name, name)) for name in profiles]
        if profile_state != self._profiles:
            self.profile_menu.clear()
            self.profile_actions.clear()
            for name in profiles:
                action = QAction(
                    labels.get(name, name), self.profile_menu, checkable=True
                )
                action.triggered.connect(
                    lambda _checked=False, value=name: self.controller.call(
                        f"profile:{value}", "set_profile", value
                    )
                )
                self.profile_group.addAction(action)
                self.profile_menu.addAction(action)
                self.profile_actions[name] = action
            self._profiles = profile_state
        for name, action in self.profile_actions.items():
            action.blockSignals(True)
            action.setChecked(name == snap.power.applied_profile)
            action.blockSignals(False)
        for name, action in self.battery_actions.items():
            action.blockSignals(True)
            action.setChecked(name == str(snap.battery.mode))
            action.blockSignals(False)
        self.gestures_action.blockSignals(True)
        self.gestures_action.setChecked(snap.gestures.daemon_enabled)
        self.gestures_action.blockSignals(False)
        for setting, action in self.touchpad_actions.items():
            action.blockSignals(True)
            configured = setting in snap.gestures.firmware_settings
            action.setChecked(bool(snap.gestures.firmware_settings.get(setting, 0)))
            action.blockSignals(False)
            action.setEnabled(
                self.controller.connected
                and snap.gestures.firmware_settings_supported
                and configured
            )
            action.setToolTip(
                ""
                if configured
                else "Configure this setting in the Touchpad page first"
            )
        self.touchpad_menu.setEnabled(
            self.controller.connected and snap.gestures.firmware_settings_supported
        )

    def _open_gui(self) -> None:
        if self._open_window is not None:
            self._open_window()
            return
        import os
        import subprocess

        cmd = ["honor-control-gui"]
        try:
            subprocess.Popen(cmd, env=dict(os.environ), start_new_session=True)
            # The GUI owns an integrated tray. Hand off instead of leaving two
            # tray icons and two independent D-Bus clients running.
            self._quit()
        except Exception as exc:  # noqa: BLE001
            self.tray.showMessage(
                "Honor Control",
                f"Could not launch the GUI: {exc}",
                QSystemTrayIcon.MessageIcon.Warning,
                3000,
            )

    def _set_charge_mode(self, mode: str) -> None:
        self.controller.call(f"charge:{mode}", "set_mode", mode)

    def _toggle_gesture_daemon(self, enabled: bool) -> None:
        self.controller.call("gestures:daemon", "set_daemon_enabled", enabled)

    def _set_touchpad_setting(self, setting: str, enabled: bool) -> None:
        answer = QMessageBox.warning(
            None,
            "Change touchpad firmware setting",
            "This writes to touchpad firmware without setting readback or rollback. "
            "Continue?",
            QMessageBox.StandardButton.Apply | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer != QMessageBox.StandardButton.Apply:
            action = self.touchpad_actions.get(setting)
            if action is not None:
                action.blockSignals(True)
                action.setChecked(not enabled)
                action.blockSignals(False)
            return
        self.controller.call(
            f"touchpad:{setting}", "set_touchpad_setting", setting, int(enabled)
        )

    def _set_online(self, online: bool) -> None:
        for menu in (self.profile_menu, self.battery_menu):
            menu.setEnabled(online)
        self.gestures_action.setEnabled(online)
        if not online:
            self.touchpad_menu.setEnabled(False)

    def _on_operation(self, _operation: str, result: object) -> None:
        self.controller.refresh()
        if result is not None and getattr(result, "message", ""):
            self._notify(str(result.message))

    def _on_activated(self, reason) -> None:
        if reason in (
            QSystemTrayIcon.ActivationReason.Trigger,
            QSystemTrayIcon.ActivationReason.DoubleClick,
            QSystemTrayIcon.ActivationReason.MiddleClick,
        ):
            self._open_gui()

    def _notify(self, message: str) -> None:
        self.tray.showMessage(
            "Honor Control", message, QSystemTrayIcon.MessageIcon.Information, 2000
        )

    def _quit(self) -> None:
        if self._quit_application is not None:
            self._quit_application()
            return
        self.shutdown()
        app = QApplication.instance()
        if app is not None:
            app.quit()

    @property
    def available(self) -> bool:
        """Return whether Qt currently has a system tray host."""
        return QSystemTrayIcon.isSystemTrayAvailable()

    def shutdown(self) -> None:
        """Stop tray activity without stopping a shared GUI controller."""
        self._timer.stop()
        self.tray.hide()
        if self._owns_controller:
            self.controller.stop()


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="honor-control-tray",
        description="Honor Control system tray indicator.",
    )
    p.add_argument(
        "--version", action="version", version=f"honor-control-tray {__version__}"
    )
    p.add_argument("--bus", choices=["system", "session"], default="system")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    app = QApplication.instance() or QApplication([sys.argv[0]])
    app.setApplicationName("Honor Control Tray")
    app.setQuitOnLastWindowClosed(False)
    if not QSystemTrayIcon.isSystemTrayAvailable():
        print(
            "honor-control-tray: no system tray available yet; the icon will "
            "appear when a host registers.",
            file=sys.stderr,
        )
    app._honor_tray = HonorTray(bus_kind=args.bus)  # type: ignore[attr-defined]
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
