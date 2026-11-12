"""Touchpad firmware controls, runtime status, and desktop mappings."""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFrame,
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
from honor_control.frontend.gui.key_capture import RecordButton
from honor_control.frontend.gui.pages import PageBase, PendingChange
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
        layout.setContentsMargins(0, 5, 0, 5)
        layout.setSpacing(12)

        copy_host = QWidget()
        copy_layout = QVBoxLayout(copy_host)
        copy_layout.setContentsMargins(0, 0, 0, 0)
        copy_layout.setSpacing(2)
        self.title_label = QLabel(title)
        self.title_label.setStyleSheet("font-weight: 600;")
        copy_layout.addWidget(self.title_label)
        description_label = QLabel(description)
        description_label.setWordWrap(True)
        description_label.setProperty("muted", True)
        description_label.setStyleSheet("font-size: 11px;")
        copy_layout.addWidget(description_label)
        layout.addWidget(copy_host, 1)

        self.combo = QComboBox()
        self.combo.setMinimumWidth(112)
        self.combo.setMaximumWidth(150)
        self.combo.setProperty("rowControl", True)
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


@dataclass(frozen=True)
class _Feature:
    """One user-visible gesture: a firmware switch plus its desktop shortcuts."""

    key: str
    title: str
    description: str
    setting: TouchpadSetting | None
    #: ``(gesture id, short label)`` for each shortcut this gesture can send.
    shortcuts: tuple[tuple[str, str], ...]


_FEATURES: tuple[_Feature, ...] = (
    _Feature(
        "edge_brightness",
        "Left edge: brightness",
        "Slide along the left edge to change display brightness.",
        TouchpadSetting.EDGE_BRIGHTNESS,
        (("3:1", "Slide up"), ("3:2", "Slide down")),
    ),
    _Feature(
        "edge_volume",
        "Right edge: volume",
        "Slide along the right edge to change speaker volume.",
        TouchpadSetting.EDGE_VOLUME,
        (("4:1", "Slide up"), ("4:2", "Slide down")),
    ),
    _Feature(
        "edge_control_center",
        "Two-finger swipe: notifications",
        "Swipe left with two fingers to open the notification panel.",
        TouchpadSetting.EDGE_CONTROL_CENTER,
        (("10:3", "Swipe left"),),
    ),
    _Feature(
        "edge_close_or_minimize",
        "Top corners: minimize and close",
        "Click the top-left corner to minimize, the top-right to close.",
        TouchpadSetting.EDGE_CLOSE_OR_MINIMIZE,
        (("8", "Top-left corner (minimize)"), ("9", "Top-right corner (close)")),
    ),
    _Feature(
        "knuckle_screenshot",
        "Knuckle: screenshot",
        "Double-knock with one knuckle to take a screenshot.",
        TouchpadSetting.KNUCKLE_SCREENSHOT,
        (("6", "Double knock"),),
    ),
    _Feature(
        "knuckle_screen_record",
        "Knuckle: screen recording",
        "Double-knock with two knuckles to start a recording.",
        TouchpadSetting.KNUCKLE_SCREEN_RECORD,
        (("7", "Double knock"),),
    ),
)

_KNOWN_IDS = frozenset(gid for feature in _FEATURES for gid, _ in feature.shortcuts)


class _ShortcutLine(QWidget):
    """One editable key chord (typed or recorded) for a single gesture ID."""

    changed = Signal()

    def __init__(self, entry: GestureEntry, label: str) -> None:
        super().__init__()
        self._entry = entry
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 2, 0, 2)
        layout.setSpacing(8)
        self.label = QLabel(label)
        self.label.setMinimumWidth(170)
        self.label.setProperty("muted", True)
        layout.addWidget(self.label)
        self.mapping = QLineEdit()
        self.mapping.setPlaceholderText("e.g. leftmeta,v")
        self.mapping.setToolTip("Comma-separated Linux key names, or use Record")
        self.mapping.textChanged.connect(lambda _text: self.changed.emit())
        layout.addWidget(self.mapping, 1)
        self.record_button = RecordButton()
        self.record_button.recorded.connect(self.mapping.setText)
        layout.addWidget(self.record_button)
        self._show(entry)

    @property
    def gesture_id(self) -> str:
        return self._entry.id

    @property
    def entry(self) -> GestureEntry:
        return self._entry

    @property
    def mapping_changed(self) -> bool:
        return self.mapping.text() != self._entry.mapping

    def _show(self, entry: GestureEntry) -> None:
        self.mapping.blockSignals(True)
        self.mapping.setText(entry.mapping)
        self.mapping.blockSignals(False)

    def update_entry(self, entry: GestureEntry) -> None:
        """Take the service value without losing a staged edit."""
        was_dirty = self.mapping_changed
        self._entry = entry
        self.setToolTip(entry.error)
        if not was_dirty:
            self._show(entry)

    def discard(self) -> None:
        self._show(self._entry)


class _GestureItem(QFrame):
    """One gesture: a single Enabled switch plus its shortcut line(s).

    *Enabled* means both that the touchpad firmware reports the gesture and
    that the desktop turns it into its shortcut, so there is one switch to
    reason about.  Edits are staged and sent from the window's Apply bar.
    """

    changed = Signal()

    def __init__(self, feature: _Feature) -> None:
        super().__init__()
        self.feature = feature
        self.setObjectName("HonorGestureItem")
        self.lines: dict[str, _ShortcutLine] = {}
        self._firmware_value: int | None = None
        self._firmware_supported = False
        self._baseline_enabled = False
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(0, 8, 0, 8)
        self._layout.setSpacing(2)
        head = QHBoxLayout()
        copy = QVBoxLayout()
        copy.setSpacing(1)
        self.title_label = QLabel(feature.title)
        self.title_label.setStyleSheet("font-weight: 600;")
        copy.addWidget(self.title_label)
        description = QLabel(feature.description)
        description.setWordWrap(True)
        description.setProperty("muted", True)
        description.setStyleSheet("font-size: 11px;")
        copy.addWidget(description)
        head.addLayout(copy, 1)
        self.enabled_box = QCheckBox("Enabled")
        self.enabled_box.toggled.connect(lambda _checked: self.changed.emit())
        head.addWidget(self.enabled_box)
        self._layout.addLayout(head)

    # -- State --

    @property
    def firmware_setting(self) -> str | None:
        setting = self.feature.setting
        return setting.value if setting is not None else None

    def _effective_enabled(self) -> bool:
        desktop = bool(self.lines) and all(
            line.entry.enabled for line in self.lines.values()
        )
        firmware_on = not self._firmware_supported or self._firmware_value != 0
        return desktop and firmware_on

    @property
    def enabled_changed(self) -> bool:
        return self.enabled_box.isChecked() != self._baseline_enabled

    @property
    def firmware_changed(self) -> bool:
        """Whether the firmware switch must be written on Apply."""
        return (
            self._firmware_supported
            and self.firmware_setting is not None
            and self.enabled_changed
        )

    @property
    def firmware_value(self) -> int:
        return 1 if self.enabled_box.isChecked() else 0

    @property
    def mappings_changed(self) -> bool:
        return any(line.mapping_changed for line in self.lines.values())

    @property
    def is_dirty(self) -> bool:
        return self.enabled_changed or self.mappings_changed

    def _show_enabled(self) -> None:
        self.enabled_box.blockSignals(True)
        self.enabled_box.setChecked(self._baseline_enabled)
        self.enabled_box.blockSignals(False)

    def update_from(
        self,
        entries: dict[str, GestureEntry],
        firmware_value: int | None,
        firmware_supported: bool,
    ) -> None:
        """Take new service values; a staged edit is never overwritten."""
        was_dirty = self.is_dirty
        for gid in tuple(self.lines):
            if gid not in entries:
                stale = self.lines.pop(gid)
                self._layout.removeWidget(stale)
                stale.deleteLater()
        for gid, label in self.feature.shortcuts:
            entry = entries.get(gid)
            if entry is None:
                continue
            line = self.lines.get(gid)
            if line is None:
                line = _ShortcutLine(entry, label)
                line.changed.connect(self.changed.emit)
                self.lines[gid] = line
                self._layout.addWidget(line)
            else:
                line.update_entry(entry)
        self._firmware_value = firmware_value
        self._firmware_supported = firmware_supported
        self._baseline_enabled = self._effective_enabled()
        if not was_dirty:
            self._show_enabled()
        self.changed.emit()

    def set_enabled(self, enabled: bool) -> None:
        self.enabled_box.setChecked(enabled)

    def discard(self) -> None:
        self._show_enabled()
        for line in self.lines.values():
            line.discard()
        self.changed.emit()


class TouchpadPage(PageBase):
    """Firmware configuration and Linux gesture dispatch in one touchpad page."""

    title = "Touchpad"
    icon = "input-touchpad"

    def _build(self) -> None:
        self._items: dict[str, _GestureItem] = {}
        self._rows: dict[str, _ShortcutLine] = {}
        self._firmware_rows: dict[str, _FirmwareSettingRow] = {}
        self._firmware_draft: dict[str, int] = {}
        self._firmware_dirty = False
        self._firmware_pending = False

        self._build_status()
        self._build_behavior()
        self._build_runtime()
        self._build_gestures()
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
        status.layout.addLayout(headline)

        self.device_row = InfoRow("Validated HID endpoint", "—")
        self.master_row = InfoRow("WMI master switch", "—")
        status.layout.addWidget(self.device_row)
        self.master_row.setVisible(False)
        self.add_widget(status)

    def _build_behavior(self) -> None:
        card = Card("Touch behavior")
        note = QLabel(
            "Written to the touchpad firmware with Apply changes. The firmware "
            "has no readback, so “Not configured” means Honor Control has not "
            "written that value yet."
        )
        note.setWordWrap(True)
        note.setProperty("muted", True)
        note.setStyleSheet("font-size: 11px;")
        card.layout.addWidget(note)
        for setting in (
            TouchpadSetting.SENSITIVITY,
            TouchpadSetting.VIBRATION_INTENSITY,
            TouchpadSetting.PRESS_TEXT,
            TouchpadSetting.PRESS_PICTURE,
            TouchpadSetting.THREE_FINGER_DRAG,
            TouchpadSetting.MOUSE_LIKE_MODE,
        ):
            setting_row = _FirmwareSettingRow(setting)
            setting_row.changed.connect(self._stage_firmware_setting)
            self._firmware_rows[setting.value] = setting_row
            self.register_control(setting_row.combo)
            card.layout.addWidget(setting_row)
        self.add_widget(card)

    def _build_runtime(self) -> None:
        card = Card("Desktop gesture dispatch")
        description = QLabel(
            "The Linux gesture reader turns touchpad reports into the keyboard "
            "shortcuts configured below."
        )
        description.setWordWrap(True)
        description.setProperty("muted", True)
        card.layout.addWidget(description)
        self.daemon_row = InfoRow("Dispatch service", "—")
        self.events_row = InfoRow("Reports / shortcuts emitted", "0 / 0")
        card.layout.addWidget(self.daemon_row)
        card.layout.addWidget(self.events_row)
        controls = QHBoxLayout()
        self.daemon_button = QPushButton("Enable gesture dispatch")
        self.daemon_button.clicked.connect(self._toggle_daemon)
        self.register_pending_control("set_daemon_enabled", self.daemon_button)
        controls.addWidget(self.daemon_button)
        controls.addStretch(1)
        card.layout.addLayout(controls)
        self.add_widget(card)

    def _build_gestures(self) -> None:
        card = Card("Gestures")
        intro = QLabel(
            "Each gesture has one Enabled switch (touchpad firmware and desktop "
            "shortcut together) and a key combination. Type the combination, "
            "for example leftmeta,pagedown, or press Record and use the keys."
        )
        intro.setWordWrap(True)
        intro.setProperty("muted", True)
        card.layout.addWidget(intro)
        controls = QHBoxLayout()
        enable_all = QPushButton("Enable all")
        disable_all = QPushButton("Disable all")
        enable_all.clicked.connect(lambda: self._stage_all_enabled(True))
        disable_all.clicked.connect(lambda: self._stage_all_enabled(False))
        self.register_control(enable_all)
        self.register_control(disable_all)
        controls.addWidget(enable_all)
        controls.addWidget(disable_all)
        controls.addStretch(1)
        card.layout.addLayout(controls)

        self.list_host = QWidget()
        self.list_layout = QVBoxLayout(self.list_host)
        self.list_layout.setContentsMargins(0, 0, 0, 0)
        self.list_layout.setSpacing(6)
        self.list_layout.addStretch(1)
        card.layout.addWidget(self.list_host)
        self.add_widget(card)

    def _stage_firmware_setting(self, setting: str, value: int) -> None:
        self._firmware_draft[setting] = value
        self._firmware_dirty = True
        self._update_firmware_actions()
        self.notify_changes()

    def _stage_all_enabled(self, enabled: bool) -> None:
        for item in self._items.values():
            item.set_enabled(enabled)

    def _firmware_payload(self) -> dict[str, int]:
        payload = dict(self._firmware_draft)
        for item in self._items.values():
            if item.firmware_changed and item.firmware_setting is not None:
                payload[item.firmware_setting] = item.firmware_value
        return payload

    def _firmware_edits_pending(self) -> bool:
        return self._firmware_dirty or any(
            item.firmware_changed for item in self._items.values()
        )

    def _apply_firmware_changes(self) -> None:
        payload = self._firmware_payload()
        if not payload or not self._firmware_edits_pending():
            return
        if self.isVisible():
            answer = QMessageBox.warning(
                self,
                "Apply touchpad firmware settings",
                "This writes settings to the descriptor-verified touchpad firmware "
                "endpoint. Firmware setting readback and rollback are unavailable. "
                "Continue?",
                QMessageBox.StandardButton.Apply | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel,
            )
            if answer != QMessageBox.StandardButton.Apply:
                return
        self.intent.emit("apply_touchpad_settings", (payload,))

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

    def _toggle_daemon(self) -> None:
        snap = self.state.snapshot
        enabled = snap.gestures.daemon_enabled if snap else False
        self.intent.emit("set_daemon_enabled", (not enabled,))

    def _on_operation_pending(self, operation_id: str) -> None:
        if operation_id.startswith("apply_touchpad_settings:"):
            self._firmware_pending = True
            self._update_firmware_actions()

    def _on_operation_completed(self, operation_id: str, result: object) -> None:
        if operation_id.startswith("apply_touchpad_settings:"):
            self._firmware_pending = False
            if result is not None and bool(getattr(result, "applied", False)):
                self._firmware_dirty = False
            self._update_firmware_actions()
            self.notify_changes()

    def _on_snapshot(self, snap: SystemSnapshot) -> None:
        ges = snap.gestures
        firmware_ok = ges.firmware_settings_supported
        self.firmware_dot.set_color(StatusDot.GREEN if firmware_ok else StatusDot.RED)
        if not firmware_ok:
            summary = "Firmware controls unavailable"
        else:
            summary = "Firmware controls available"
        self.firmware_summary.setText(summary)
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
        self._sync_items(incoming, ges.firmware_settings, firmware_ok)
        self.set_backend_available(self.state.connected)
        self._update_firmware_actions()

    def _features_for(self, incoming: dict[str, GestureEntry]) -> list[_Feature]:
        features = [
            feature
            for feature in _FEATURES
            if any(gid in incoming for gid, _ in feature.shortcuts)
        ]
        for gid, entry in incoming.items():
            if gid not in _KNOWN_IDS:
                features.append(
                    _Feature(
                        f"id:{gid}",
                        entry.label,
                        f"Report {gid}",
                        None,
                        ((gid, "Shortcut"),),
                    )
                )
        return features

    def _sync_items(
        self,
        incoming: dict[str, GestureEntry],
        firmware: dict[str, int],
        firmware_ok: bool,
    ) -> None:
        wanted = {feature.key: feature for feature in self._features_for(incoming)}
        for key in tuple(self._items):
            if key not in wanted:
                item = self._items.pop(key)
                self.unregister_control(item)
                self.list_layout.removeWidget(item)
                item.deleteLater()
        for key, feature in wanted.items():
            item = self._items.get(key)
            if item is None:
                item = _GestureItem(feature)
                item.changed.connect(self.notify_changes)
                self._items[key] = item
                self.register_pending_control("set_mapping", item)
                self.register_pending_control("set_enabled", item)
                self.list_layout.insertWidget(self.list_layout.count() - 1, item)
            setting = feature.setting.value if feature.setting is not None else None
            item.update_from(
                incoming, firmware.get(setting) if setting else None, firmware_ok
            )
        self._rows = {
            gid: line
            for item in self._items.values()
            for gid, line in item.lines.items()
        }

    # -- Unsaved edits (see the window's Apply / Discard bar) --

    def pending_changes(self) -> list[PendingChange]:
        changes: list[PendingChange] = []
        if self._firmware_dirty:
            changes.append(PendingChange("Touchpad behavior settings"))
        edited = sum(1 for item in self._items.values() if item.is_dirty)
        if edited:
            noun = "gesture" if edited == 1 else "gestures"
            changes.append(PendingChange(f"{edited} {noun} edited"))
        return changes

    def apply_pending(self) -> None:
        for item in self._items.values():
            for line in item.lines.values():
                if line.mapping_changed:
                    self.intent.emit(
                        "set_mapping", (line.gesture_id, line.mapping.text())
                    )
                if (
                    item.enabled_changed
                    and item.enabled_box.isChecked() != line.entry.enabled
                ):
                    self.intent.emit(
                        "set_enabled",
                        (line.gesture_id, item.enabled_box.isChecked()),
                    )
        self._apply_firmware_changes()

    def discard_pending(self) -> None:
        if self._firmware_dirty:
            self._discard_firmware_changes()
        for item in self._items.values():
            item.discard()
        self.notify_changes()

    def refresh(self) -> None:
        snap = self.state.snapshot
        if snap is not None:
            self._on_snapshot(snap)
