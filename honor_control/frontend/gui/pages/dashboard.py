"""Compact, at-a-glance hardware overview."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSlider,
)

from honor_control.core.models import POWER_PROFILES, FanMode, SystemSnapshot
from honor_control.frontend.gui.pages import PageBase, PendingChange
from honor_control.frontend.gui.widgets import Card, ToggleSwitch


def _milli_to_celsius(value: int | None) -> str:
    try:
        return f"{int(value) // 1000} °C" if value is not None else "—"
    except (TypeError, ValueError):
        return "—"


def _quiet_button(text: str) -> QPushButton:
    button = QPushButton(text)
    button.setProperty("quiet", True)
    return button


class DashboardPage(PageBase):
    """Overview optimized for checking state and making one quick change."""

    title = "Overview"
    icon = "go-home"
    show_page_title = False

    def _build(self) -> None:
        self._profile_selection_dirty = False
        self._build_performance()
        self._build_battery()
        self._build_cooling()
        self._build_gestures()
        self._build_utilities()
        self.add_stretch()
        self.state.snapshot_changed.connect(self._on_snapshot)
        self.state.operation_completed.connect(self._on_operation_completed)

    def _build_performance(self) -> None:
        self.profile_card = Card("Performance mode")
        summary = QHBoxLayout()
        summary.setSpacing(16)
        self.profile_value = QLabel("—")
        self.profile_value.setObjectName("HonorPrimaryValue")
        summary.addWidget(self.profile_value)
        summary.addStretch(1)
        self.cpu_value = QLabel("CPU  —")
        self.cpu_value.setProperty("muted", True)
        summary.addWidget(self.cpu_value)
        self.fan_value = QLabel("Fan  —")
        self.fan_value.setProperty("muted", True)
        summary.addWidget(self.fan_value)
        self.profile_card.layout.addLayout(summary)

        picker = QHBoxLayout()
        picker.addWidget(QLabel("Profile"))
        self.profile_combo = QComboBox()
        self.profile_combo.currentIndexChanged.connect(
            self._on_profile_selection_changed
        )
        picker.addWidget(self.profile_combo, 1)
        self.register_pending_control("set_profile", self.profile_combo)
        self.profile_card.layout.addLayout(picker)
        self.add_widget(self.profile_card)

    def _build_battery(self) -> None:
        self.battery_card = Card("Battery")
        summary = QHBoxLayout()
        self.battery_value = QLabel("— %")
        self.battery_value.setObjectName("HonorPrimaryValue")
        summary.addWidget(self.battery_value)
        summary.addSpacing(20)
        self.battery_sub = QLabel("—")
        self.battery_sub.setProperty("muted", True)
        summary.addWidget(self.battery_sub)
        summary.addStretch(1)
        self.battery_card.layout.addLayout(summary)

        limit_line = QHBoxLayout()
        label = QLabel("Charge limit")
        label.setProperty("muted", True)
        limit_line.addWidget(label)
        self.limit_value = QLabel("—")
        self.limit_value.setProperty("muted", True)
        limit_line.addWidget(self.limit_value)
        limit_line.addStretch(1)
        self.battery_card.layout.addLayout(limit_line)
        self.charge_slider = QSlider(Qt.Orientation.Horizontal)
        self.charge_slider.setRange(45, 100)
        self.charge_slider.setSingleStep(5)
        self.charge_slider.sliderReleased.connect(
            lambda: self.intent.emit(
                "set_thresholds",
                (self.charge_slider.value(), self.charge_slider.value() - 5),
            )
        )
        self.register_pending_control("set_thresholds", self.charge_slider)
        self.battery_card.layout.addWidget(self.charge_slider)
        self.add_widget(self.battery_card)

    def _build_cooling(self) -> None:
        card = Card("Cooling")
        row = QHBoxLayout()
        self.cooling_value = QLabel("—")
        self.cooling_value.setStyleSheet("font-size: 18px; font-weight: 600;")
        row.addWidget(self.cooling_value)
        row.addSpacing(28)
        self.cooling_sub = QLabel("Firmware curve")
        self.cooling_sub.setProperty("muted", True)
        row.addWidget(self.cooling_sub)
        row.addStretch(1)
        adjust = _quiet_button("Adjust")
        adjust.clicked.connect(lambda: self.intent.emit("navigate", ("Fan",)))
        row.addWidget(adjust)
        card.layout.addLayout(row)
        self.add_widget(card)

    def _build_gestures(self) -> None:
        card = Card("Touchpad gestures")
        row = QHBoxLayout()
        self.gesture_value = QLabel("—")
        self.gesture_value.setStyleSheet("font-size: 18px; font-weight: 600;")
        row.addWidget(self.gesture_value)
        row.addSpacing(28)
        self.gesture_sub = QLabel("0 reports · 0 shortcuts emitted")
        self.gesture_sub.setProperty("muted", True)
        row.addWidget(self.gesture_sub)
        row.addStretch(1)
        self.gesture_toggle = ToggleSwitch(False)
        self.gesture_toggle.setAccessibleName("Desktop gesture dispatch")
        self.gesture_toggle.toggled.connect(
            lambda enabled: self.intent.emit("set_daemon_enabled", (enabled,))
        )
        self.register_pending_control("set_daemon_enabled", self.gesture_toggle)
        row.addWidget(self.gesture_toggle)
        configure = _quiet_button("Configure")
        configure.clicked.connect(lambda: self.intent.emit("navigate", ("Touchpad",)))
        row.addWidget(configure)
        card.layout.addLayout(row)
        self.add_widget(card)

    def _build_utilities(self) -> None:
        row = QHBoxLayout()
        row.setContentsMargins(12, 12, 12, 0)
        for label in ("Diagnostics", "Settings"):
            button = _quiet_button(label)
            button.clicked.connect(
                lambda _checked=False, target=label: self.intent.emit(
                    "navigate", (target,)
                )
            )
            row.addWidget(button)
            row.addStretch(1)
        quit_button = _quiet_button("Quit")
        quit_button.clicked.connect(lambda: self.intent.emit("quit", ()))
        row.addWidget(quit_button)
        self._root.addLayout(row)

    def _on_snapshot(self, snap: SystemSnapshot) -> None:
        applied = snap.power.applied_profile or snap.power.desired_profile or "—"
        self.profile_value.setText(applied)
        entries = snap.power.profiles or POWER_PROFILES
        combo_state = [
            (self.profile_combo.itemData(i), self.profile_combo.itemText(i))
            for i in range(self.profile_combo.count())
        ]
        desired_state = [(entry.name, entry.label) for entry in entries]
        if combo_state != desired_state:
            selected = self.profile_combo.currentData()
            self.profile_combo.blockSignals(True)
            self.profile_combo.clear()
            for entry in entries:
                self.profile_combo.addItem(entry.label, entry.name)
            index = self.profile_combo.findData(selected)
            if index < 0:
                index = self.profile_combo.findData(applied)
            if index >= 0:
                self.profile_combo.setCurrentIndex(index)
            self.profile_combo.blockSignals(False)
        if self.profile_combo.currentData() == applied:
            self._profile_selection_dirty = False
        if not self._profile_selection_dirty:
            index = self.profile_combo.findData(applied)
            if index >= 0 and index != self.profile_combo.currentIndex():
                self.profile_combo.blockSignals(True)
                self.profile_combo.setCurrentIndex(index)
                self.profile_combo.blockSignals(False)
        self.cpu_value.setText(f"CPU  {_milli_to_celsius(snap.fan.temp_mc)}")
        if snap.fan.measured_rpm is not None:
            fan = f"{snap.fan.measured_rpm:,} RPM"
        elif snap.fan.target_speed is not None:
            fan = f"{snap.fan.target_speed} %"
        else:
            fan = "Unavailable"
        self.fan_value.setText(f"Fan  {fan}")

        capacity = snap.battery.capacity_percent
        self.battery_value.setText(f"{capacity} %" if capacity is not None else "— %")
        status = str(snap.battery.status) if snap.battery.status else "Unknown"
        source = (
            "Connected to AC"
            if snap.battery.ac_online is True
            else "On battery"
            if snap.battery.ac_online is False
            else "Power source unknown"
        )
        self.battery_sub.setText(f"{status}  ·  {source}")
        limit = snap.battery.observed_end
        self.limit_value.setText(f"{limit} %" if limit is not None else "Unavailable")
        if limit is not None and not self.charge_slider.isSliderDown():
            self.charge_slider.blockSignals(True)
            self.charge_slider.setValue(limit)
            self.charge_slider.blockSignals(False)

        mode = str(snap.fan.mode).replace("_", " ").title()
        self.cooling_value.setText(mode or "Unknown")
        self.cooling_sub.setText(self._cooling_detail(snap))

        gestures = snap.gestures
        running = gestures.daemon_running
        self.gesture_value.setText("Running" if running else "Stopped")
        self.gesture_sub.setText(
            f"{gestures.reports_seen:,} reports  ·  "
            f"{gestures.gestures_emitted:,} shortcuts emitted"
        )
        self.gesture_toggle.setChecked(gestures.daemon_enabled)
        self.notify_changes()

    @staticmethod
    def _cooling_detail(snap: SystemSnapshot) -> str:
        fan = snap.fan
        if not fan.available:
            return "Fan controls unavailable"
        if fan.mode == FanMode.CURVE:
            return "Custom curve"
        if fan.mode == FanMode.MANUAL_OVERRIDE:
            speed = f" at {fan.target_speed} %" if fan.target_speed is not None else ""
            return f"Manual speed{speed}"
        if fan.mode == FanMode.FAILED_SAFE:
            return "Fail-safe: fans forced on"
        return "Firmware curve"

    def _on_profile_selection_changed(self, *_args) -> None:
        self._profile_selection_dirty = True
        self.notify_changes()

    def _on_operation_completed(self, operation_id: str, result: object) -> None:
        if not operation_id.startswith("set_profile:"):
            return
        self._profile_selection_dirty = False
        if not (result is not None and bool(getattr(result, "applied", False))):
            self.refresh()
        self.notify_changes()

    # -- Unsaved edits (see the window's Apply / Discard bar) --

    def _applied_profile(self) -> str | None:
        snap = self.state.snapshot
        if snap is None:
            return None
        return snap.power.applied_profile or snap.power.desired_profile

    def pending_changes(self) -> list[PendingChange]:
        if (
            self._profile_selection_dirty
            and self.profile_combo.currentData() != self._applied_profile()
        ):
            return [
                PendingChange(f"Apply power profile: {self.profile_combo.currentText()}")
            ]
        return []

    def apply_pending(self) -> None:
        if self.pending_changes():
            self.intent.emit("set_profile", (self.profile_combo.currentData(),))

    def discard_pending(self) -> None:
        self._profile_selection_dirty = False
        self.refresh()
        self.notify_changes()

    def refresh(self) -> None:
        if self.state.snapshot is not None:
            self._on_snapshot(self.state.snapshot)
