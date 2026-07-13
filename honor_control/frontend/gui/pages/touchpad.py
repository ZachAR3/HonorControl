"""Touchpad firmware controls, runtime status, and desktop mappings."""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from honor_control.core.models import GestureEntry, SystemSnapshot
from honor_control.core.touchpad import TOUCHPAD_SETTING_SPECS, TouchpadSetting
from honor_control.frontend.gui.pages import PageBase
from honor_control.frontend.gui.widgets import Card, InfoRow, StatusDot

_SETTING_COPY: dict[TouchpadSetting, tuple[str, str]] = {
    TouchpadSetting.SENSITIVITY: (
        "Touch sensitivity",
        "Choose how readily the surface responds to finger movement.",
    ),
    TouchpadSetting.VIBRATION_INTENSITY: (
        "Haptic feedback",
        "Set the strength of click and pressure feedback.",
    ),
    TouchpadSetting.PRESS_TEXT: (
        "Pressure actions in text",
        "Allow firm presses while working with text.",
    ),
    TouchpadSetting.PRESS_PICTURE: (
        "Pressure actions on images",
        "Allow firm presses while working with pictures.",
    ),
    TouchpadSetting.THREE_FINGER_DRAG: (
        "Three-finger drag",
        "Move windows and selected items with three fingers.",
    ),
    TouchpadSetting.MOUSE_LIKE_MODE: (
        "Mouse-like behavior",
        "Use the firmware's alternate pointer response mode.",
    ),
    TouchpadSetting.EDGE_BRIGHTNESS: (
        "Left edge brightness",
        "Swipe along the edge to adjust display brightness.",
    ),
    TouchpadSetting.EDGE_VOLUME: (
        "Right edge volume",
        "Swipe along the edge to adjust speaker volume.",
    ),
    TouchpadSetting.EDGE_CONTROL_CENTER: (
        "Open Control Center",
        "Enable the firmware edge gesture for Control Center.",
    ),
    TouchpadSetting.EDGE_CLOSE_OR_MINIMIZE: (
        "Close or minimize windows",
        "Enable the firmware edge gesture for window management.",
    ),
    TouchpadSetting.KNUCKLE_SCREENSHOT: (
        "Knuckle screenshot",
        "Capture the screen with the supported knuckle gesture.",
    ),
    TouchpadSetting.KNUCKLE_SCREEN_RECORD: (
        "Knuckle screen recording",
        "Start screen recording with the supported knuckle gesture.",
    ),
}


class _FirmwareSettingRow(QWidget):
    """One explicit typed firmware setting with an unknown-state option."""

    changed = Signal(str, int)

    def __init__(self, setting: TouchpadSetting) -> None:
        super().__init__()
        self.setting = setting
        title, description = _SETTING_COPY[setting]
        spec = TOUCHPAD_SETTING_SPECS[setting]

        layout = QHBoxLayout(self)
        layout.setContentsMargins(2, 7, 2, 7)
        layout.setSpacing(16)

        copy_host = QWidget()
        copy_layout = QVBoxLayout(copy_host)
        copy_layout.setContentsMargins(0, 0, 0, 0)
        copy_layout.setSpacing(2)
        self.title_label = QLabel(title)
        self.title_label.setStyleSheet("font-weight: 600;")
        copy_layout.addWidget(self.title_label)
        description_label = QLabel(description)
        description_label.setWordWrap(True)
        description_label.setStyleSheet(
            "color: rgba(128, 128, 128, 210); font-size: 11px;"
        )
        copy_layout.addWidget(description_label)
        layout.addWidget(copy_host, 1)

        self.combo = QComboBox()
        self.combo.setMinimumWidth(170)
        self.combo.addItem("Not configured", None)
        if spec.labels:
            for value, label in enumerate(spec.labels):
                self.combo.addItem(label.replace("_", " ").title(), value)
        else:
            self.combo.addItem("Off", 0)
            self.combo.addItem("On", 1)
        self.combo.setToolTip(
            "The firmware has no value readback. This shows the last setting "
            "accepted and saved by Honor Control."
        )
        self.combo.currentIndexChanged.connect(self._selection_changed)
        layout.addWidget(self.combo)

    def _selection_changed(self, _index: int) -> None:
        value = self.combo.currentData()
        if value is not None:
            self.changed.emit(self.setting.value, int(value))

    def set_value(self, value: int | None) -> None:
        self.combo.blockSignals(True)
        index = self.combo.findData(value)
        self.combo.setCurrentIndex(index if index >= 0 else 0)
        self.combo.blockSignals(False)


class _GestureRow(QWidget):
    """Stable, editable desktop shortcut row keyed by gesture ID."""

    intent = Signal(str, object)

    def __init__(self, entry: GestureEntry) -> None:
        super().__init__()
        self._gesture_id = entry.id
        self._enabled = entry.enabled
        layout = QHBoxLayout(self)
        layout.setContentsMargins(2, 5, 2, 5)
        layout.setSpacing(8)
        self.name = QLabel()
        self.name.setMinimumWidth(250)
        layout.addWidget(self.name)
        self.mapping = QLineEdit()
        self.mapping.setPlaceholderText("e.g. leftmeta,v")
        self.mapping.setToolTip("Comma-separated Linux key names")
        self.mapping.returnPressed.connect(self._apply)
        layout.addWidget(self.mapping, 1)
        self.apply_button = QPushButton("Save")
        self.apply_button.clicked.connect(self._apply)
        layout.addWidget(self.apply_button)
        self.toggle_button = QPushButton()
        self.toggle_button.clicked.connect(self._toggle)
        layout.addWidget(self.toggle_button)
        self.update_entry(entry)

    def update_entry(self, entry: GestureEntry) -> None:
        self._enabled = entry.enabled
        self.name.setText(entry.label)
        self.name.setToolTip(f"Report ID: {entry.id}")
        if not self.mapping.hasFocus():
            self.mapping.setText(entry.mapping)
        self.toggle_button.setText("Disable" if entry.enabled else "Enable")
        self.setToolTip(entry.error)

    def _apply(self) -> None:
        self.intent.emit("set_mapping", (self._gesture_id, self.mapping.text()))

    def _toggle(self) -> None:
        self.intent.emit("set_enabled", (self._gesture_id, not self._enabled))


class TouchpadPage(PageBase):
    """Firmware configuration and Linux gesture dispatch in one touchpad page."""

    title = "Touchpad"
    icon = "input-touchpad"

    def _build(self) -> None:
        self._rows: dict[str, _GestureRow] = {}
        self._firmware_rows: dict[str, _FirmwareSettingRow] = {}
        self._firmware_draft: dict[str, int] = {}
        self._firmware_dirty = False
        self._firmware_pending = False
        self._support_pending = False

        self._build_status()
        self._build_firmware_group(
            "Touch response",
            (
                TouchpadSetting.SENSITIVITY,
                TouchpadSetting.VIBRATION_INTENSITY,
                TouchpadSetting.PRESS_TEXT,
                TouchpadSetting.PRESS_PICTURE,
                TouchpadSetting.MOUSE_LIKE_MODE,
            ),
        )
        self._build_firmware_group(
            "Multitouch",
            (TouchpadSetting.THREE_FINGER_DRAG,),
        )
        self._build_firmware_group(
            "Edge gestures",
            (
                TouchpadSetting.EDGE_BRIGHTNESS,
                TouchpadSetting.EDGE_VOLUME,
                TouchpadSetting.EDGE_CONTROL_CENTER,
                TouchpadSetting.EDGE_CLOSE_OR_MINIMIZE,
            ),
        )
        self._build_firmware_group(
            "Knuckle gestures",
            (
                TouchpadSetting.KNUCKLE_SCREENSHOT,
                TouchpadSetting.KNUCKLE_SCREEN_RECORD,
            ),
        )
        self._build_runtime()
        self._build_mappings()
        self.add_stretch()

        self.state.snapshot_changed.connect(self._on_snapshot)
        self.state.operation_pending.connect(self._on_operation_pending)
        self.state.operation_completed.connect(self._on_operation_completed)

    def _build_status(self) -> None:
        status = Card("Touchpad firmware")
        headline = QHBoxLayout()
        self.firmware_dot = StatusDot(StatusDot.GREY)
        headline.addWidget(self.firmware_dot)
        self.firmware_summary = QLabel("Checking firmware endpoint…")
        self.firmware_summary.setStyleSheet("font-weight: 600;")
        headline.addWidget(self.firmware_summary)
        headline.addStretch(1)
        self.support_button = QPushButton("Check supported gestures")
        self.support_button.clicked.connect(
            lambda: self.intent.emit("query_touchpad_support", ())
        )
        self.register_control(self.support_button)
        headline.addWidget(self.support_button)
        status.layout.addLayout(headline)

        explanation = QLabel(
            "Changes are applied together in one firmware transaction and saved as "
            "desired state. The firmware does not "
            "provide setting readback, so “Not configured” means Honor Control has "
            "not written that value yet."
        )
        explanation.setWordWrap(True)
        explanation.setStyleSheet("color: rgba(128, 128, 128, 210);")
        status.layout.addWidget(explanation)

        self.device_row = InfoRow("Validated HID endpoint", "—")
        self.master_row = InfoRow("WMI master switch", "—")
        self.support_row = InfoRow("Capability query", "Not run")
        status.layout.addWidget(self.device_row)
        status.layout.addWidget(self.master_row)
        status.layout.addWidget(self.support_row)
        actions = QHBoxLayout()
        actions.addStretch(1)
        self.discard_button = QPushButton("Discard changes")
        self.discard_button.clicked.connect(self._discard_firmware_changes)
        self.register_control(self.discard_button)
        actions.addWidget(self.discard_button)
        self.apply_button = QPushButton("Apply touchpad settings")
        self.apply_button.setDefault(True)
        self.apply_button.clicked.connect(self._apply_firmware_changes)
        self.register_control(self.apply_button)
        actions.addWidget(self.apply_button)
        status.layout.addLayout(actions)
        self.add_widget(status)

    def _build_firmware_group(
        self,
        title: str,
        settings: tuple[TouchpadSetting, ...],
    ) -> None:
        card = Card(title)
        for setting in settings:
            row = _FirmwareSettingRow(setting)
            row.changed.connect(self._stage_firmware_setting)
            self._firmware_rows[setting.value] = row
            self.register_control(row.combo)
            card.layout.addWidget(row)
        self.add_widget(card)

    def _build_runtime(self) -> None:
        card = Card("Desktop gesture dispatch")
        description = QLabel(
            "The Linux gesture reader converts touchpad reports into configured "
            "keyboard shortcuts. It is independent of the firmware settings above."
        )
        description.setWordWrap(True)
        description.setStyleSheet("color: rgba(128, 128, 128, 210);")
        card.layout.addWidget(description)
        self.daemon_row = InfoRow("Dispatch service", "—")
        self.events_row = InfoRow("Reports / shortcuts emitted", "0 / 0")
        card.layout.addWidget(self.daemon_row)
        card.layout.addWidget(self.events_row)
        controls = QHBoxLayout()
        self.daemon_button = QPushButton("Enable gesture dispatch")
        self.daemon_button.clicked.connect(self._toggle_daemon)
        self.register_control(self.daemon_button)
        controls.addWidget(self.daemon_button)
        controls.addStretch(1)
        card.layout.addLayout(controls)
        self.add_widget(card)

    def _build_mappings(self) -> None:
        card = Card("Desktop shortcut mappings")
        intro = QLabel(
            "Map each confirmed input report to a Linux key combination. Changes "
            "are consumed live by the gesture dispatch service."
        )
        intro.setWordWrap(True)
        intro.setStyleSheet("color: rgba(128, 128, 128, 210);")
        card.layout.addWidget(intro)
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
        card.layout.addLayout(controls)

        self.list_host = QWidget()
        self.list_layout = QVBoxLayout(self.list_host)
        self.list_layout.setContentsMargins(0, 0, 0, 0)
        self.list_layout.setSpacing(2)
        self.list_layout.addStretch(1)
        card.layout.addWidget(self.list_host)
        self.add_widget(card)

    def _stage_firmware_setting(self, setting: str, value: int) -> None:
        self._firmware_draft[setting] = value
        self._firmware_dirty = True
        self._update_firmware_actions()

    def _apply_firmware_changes(self) -> None:
        if self._firmware_draft and self._firmware_dirty:
            if self.isVisible():
                answer = QMessageBox.warning(
                    self,
                    "Apply touchpad firmware settings",
                    "This writes settings to the descriptor-verified touchpad firmware "
                    "endpoint. Firmware setting readback and rollback are unavailable. "
                    "Continue?",
                    QMessageBox.StandardButton.Apply
                    | QMessageBox.StandardButton.Cancel,
                    QMessageBox.StandardButton.Cancel,
                )
                if answer != QMessageBox.StandardButton.Apply:
                    return
            self.intent.emit("apply_touchpad_settings", (dict(self._firmware_draft),))

    def _discard_firmware_changes(self) -> None:
        snap = self.state.snapshot
        self._firmware_draft = (
            dict(snap.gestures.firmware_settings) if snap is not None else {}
        )
        self._firmware_dirty = False
        for setting, row in self._firmware_rows.items():
            row.set_value(self._firmware_draft.get(setting))
        self._update_firmware_actions()

    def _update_firmware_actions(self) -> None:
        snap = self.state.snapshot
        supported = bool(snap and snap.gestures.firmware_settings_supported)
        can_edit = self.state.connected and supported and not self._firmware_pending
        for row in self._firmware_rows.values():
            row.combo.setEnabled(can_edit)
        self.apply_button.setEnabled(can_edit and self._firmware_dirty)
        self.discard_button.setEnabled(can_edit and self._firmware_dirty)
        self.support_button.setEnabled(can_edit and not self._support_pending)

    def _toggle_daemon(self) -> None:
        snap = self.state.snapshot
        enabled = snap.gestures.daemon_enabled if snap else False
        self.intent.emit("set_daemon_enabled", (not enabled,))

    def _on_operation_pending(self, operation_id: str) -> None:
        if operation_id.startswith("apply_touchpad_settings:"):
            self._firmware_pending = True
            self._update_firmware_actions()
        elif operation_id.startswith("query_touchpad_support"):
            self._support_pending = True
            self._update_firmware_actions()
            self.support_row.set_value("Querying…")

    def _on_operation_completed(self, operation_id: str, result: object) -> None:
        if operation_id.startswith("apply_touchpad_settings:"):
            self._firmware_pending = False
            if result is not None and bool(getattr(result, "applied", False)):
                self._firmware_dirty = False
            self._update_firmware_actions()
        elif operation_id.startswith("query_touchpad_support"):
            self._support_pending = False
            self._update_firmware_actions()
            if isinstance(result, dict):
                bits = result.get("supported_bits", [])
                known = result.get("known", {})
                self.support_row.set_value(
                    f"{len(bits)} bits · {len(known)} recognized"
                )
            elif result is None:
                self.support_row.set_value("Query failed")

    def _on_snapshot(self, snap: SystemSnapshot) -> None:
        ges = snap.gestures
        firmware_ok = ges.firmware_settings_supported
        self.firmware_dot.set_color(StatusDot.GREEN if firmware_ok else StatusDot.RED)
        self.firmware_summary.setText(
            "Firmware controls available"
            if firmware_ok
            else "Firmware controls unavailable"
        )
        if ges.device_found:
            device = ges.device_path or "Found"
            if ges.permission_denied:
                device += " (permission denied)"
        else:
            device = "Not found"
        self.device_row.set_value(device)
        self.master_row.set_value(
            "Driver present" if ges.wmi_transport_present else "Optional driver absent"
        )

        if not self._firmware_dirty and not self._firmware_pending:
            self._firmware_draft = dict(ges.firmware_settings)
            for setting, row in self._firmware_rows.items():
                row.set_value(self._firmware_draft.get(setting))

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
        self._update_firmware_actions()

    def refresh(self) -> None:
        snap = self.state.snapshot
        if snap is not None:
            self._on_snapshot(snap)
