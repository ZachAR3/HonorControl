"""Gesture runtime status and daemon-level key mappings."""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from honor_control.core.models import GestureEntry, SystemSnapshot
from honor_control.frontend.gui.pages import PageBase
from honor_control.frontend.gui.widgets import Card, InfoRow


class _GestureRow(QWidget):
    """Stable, editable row keyed by gesture ID."""

    intent = Signal(str, object)

    def __init__(self, entry: GestureEntry) -> None:
        super().__init__()
        self._gesture_id = entry.id
        self._enabled = entry.enabled
        layout = QHBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        self.name = QLabel()
        self.name.setMinimumWidth(260)
        layout.addWidget(self.name)
        self.mapping = QLineEdit()
        self.mapping.setPlaceholderText("e.g. leftmeta,v")
        self.mapping.setToolTip("Comma-separated Linux key names")
        self.mapping.returnPressed.connect(self._apply)
        layout.addWidget(self.mapping, 1)
        self.apply_button = QPushButton("Apply")
        self.apply_button.clicked.connect(self._apply)
        layout.addWidget(self.apply_button)
        self.toggle_button = QPushButton()
        self.toggle_button.clicked.connect(self._toggle)
        layout.addWidget(self.toggle_button)
        self.update_entry(entry)

    def update_entry(self, entry: GestureEntry) -> None:
        self._enabled = entry.enabled
        self.name.setText(f"{entry.label} ({entry.id})")
        if not self.mapping.hasFocus():
            self.mapping.setText(entry.mapping)
        self.toggle_button.setText("Disable" if entry.enabled else "Enable")
        self.setToolTip(entry.error)

    def _apply(self) -> None:
        self.intent.emit("set_mapping", (self._gesture_id, self.mapping.text()))

    def _toggle(self) -> None:
        self.intent.emit("set_enabled", (self._gesture_id, not self._enabled))


class GesturesPage(PageBase):
    """Touchpad report dispatch and mapping configuration."""

    title = "Gestures"
    icon = "input-touchpad"

    def _build(self) -> None:
        self._rows: dict[str, _GestureRow] = {}
        status = Card("Touchpad status")
        self.device_row = InfoRow("HID device", "—")
        self.daemon_row = InfoRow("Gesture dispatch", "—")
        self.events_row = InfoRow("Reports / emitted", "0 / 0")
        self.firmware_row = InfoRow("Firmware settings", "Unsupported")
        for row in (
            self.device_row,
            self.daemon_row,
            self.events_row,
            self.firmware_row,
        ):
            status.layout.addWidget(row)
        daemon_controls = QHBoxLayout()
        self.daemon_button = QPushButton("Enable gesture dispatch")
        self.daemon_button.clicked.connect(self._toggle_daemon)
        self.register_control(self.daemon_button)
        daemon_controls.addWidget(self.daemon_button)
        daemon_controls.addStretch(1)
        status.layout.addLayout(daemon_controls)
        self.add_widget(status)

        fw_card = Card("Firmware-level settings require more research")
        fw_label = QLabel(
            "Linux can consume confirmed vendor gesture reports, but it cannot yet "
            "enable sensitivity, palm rejection, edge gestures, knuckle gestures, "
            "or haptics in firmware. The captured 61-byte setting packets are a "
            "Windows UI-to-service protocol—not safe WMI/HID commands. Configure "
            "those options in Windows until the service-to-driver transport is "
            "captured and validated."
        )
        fw_label.setWordWrap(True)
        fw_label.setStyleSheet("color: rgba(128, 128, 128, 200);")
        fw_card.layout.addWidget(fw_label)
        self.add_widget(fw_card)

        self.add_heading("Daemon mappings")
        controls = QHBoxLayout()
        enable_all = QPushButton("Enable all")
        disable_all = QPushButton("Disable all")
        enable_all.clicked.connect(lambda: self.intent.emit("set_all_enabled", (True,)))
        disable_all.clicked.connect(
            lambda: self.intent.emit("set_all_enabled", (False,))
        )
        self.register_control(enable_all)
        self.register_control(disable_all)
        controls.addWidget(enable_all)
        controls.addWidget(disable_all)
        controls.addStretch(1)
        controls_host = QWidget()
        controls_host.setLayout(controls)
        self.add_widget(controls_host)
        self.list_host = QWidget()
        self.list_layout = QVBoxLayout(self.list_host)
        self.list_layout.setContentsMargins(0, 0, 0, 0)
        self.list_layout.setSpacing(2)
        self.list_layout.addStretch(1)
        self.add_widget(self.list_host)

        self.state.snapshot_changed.connect(self._on_snapshot)

    def _toggle_daemon(self) -> None:
        snap = self.state.snapshot
        enabled = snap.gestures.daemon_enabled if snap else False
        self.intent.emit("set_daemon_enabled", (not enabled,))

    def _on_snapshot(self, snap: SystemSnapshot) -> None:
        ges = snap.gestures
        if ges.device_found:
            device = ges.device_path or "Found"
            if ges.permission_denied:
                device += " (permission denied)"
        else:
            device = "Not found"
        self.device_row.set_value(device)
        if ges.daemon_running:
            daemon = "Running"
        elif ges.daemon_enabled:
            daemon = f"Enabled, not running: {ges.last_error or 'not ready'}"
        else:
            daemon = "Disabled"
        self.daemon_row.set_value(daemon)
        self.daemon_button.setText(
            "Disable gesture dispatch"
            if ges.daemon_enabled
            else "Enable gesture dispatch"
        )
        self.events_row.set_value(f"{ges.reports_seen} / {ges.gestures_emitted}")
        self.firmware_row.set_value(
            "Supported" if ges.firmware_settings_supported else "Unsupported"
        )

        incoming = {entry.id: entry for entry in ges.mappings}
        for gesture_id in tuple(self._rows):
            if gesture_id not in incoming:
                row = self._rows.pop(gesture_id)
                self.unregister_control(row)
                self.list_layout.removeWidget(row)
                row.deleteLater()
        for entry in ges.mappings:
            row = self._rows.get(entry.id)
            if row is None:
                row = _GestureRow(entry)
                row.intent.connect(self.intent.emit)
                self._rows[entry.id] = row
                self.register_control(row)
                self.list_layout.insertWidget(self.list_layout.count() - 1, row)
            else:
                row.update_entry(entry)
        self.set_backend_available(self.state.connected)

    def refresh(self) -> None:
        snap = self.state.snapshot
        if snap is not None:
            self._on_snapshot(snap)
