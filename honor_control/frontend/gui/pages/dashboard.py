"""Compact, at-a-glance hardware overview."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QButtonGroup,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSlider,
)

from honor_control.core.models import POWER_PROFILES, SystemSnapshot
from honor_control.frontend.gui.pages import PageBase
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
        self._build_performance()
        self._build_battery()
        self._build_cooling()
        self._build_gestures()
        self._build_utilities()
        self.add_stretch()
        self.state.snapshot_changed.connect(self._on_snapshot)

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

        modes = QHBoxLayout()
        modes.setSpacing(0)
        self.mode_group = QButtonGroup(self)
        self.mode_group.setExclusive(True)
        self.mode_buttons: dict[str, QPushButton] = {}
        for entry in POWER_PROFILES:
            button = QPushButton(entry.label)
            button.setCheckable(True)
            button.setMinimumWidth(112)
            button.clicked.connect(
                lambda _checked=False, name=entry.name: self.intent.emit(
                    "set_profile", (name,)
                )
            )
            self.register_pending_control("set_profile", button)
            self.mode_group.addButton(button)
            self.mode_buttons[entry.name] = button
            modes.addWidget(button)
        modes.addStretch(1)
        self.profile_card.layout.addLayout(modes)
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
        self.register_control(self.charge_slider)
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
        self.register_control(self.gesture_toggle)
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
        for name, button in self.mode_buttons.items():
            button.setChecked(name == applied)
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
        self.cooling_sub.setText(
            "Firmware curve" if snap.fan.available else "Fan controls unavailable"
        )

        gestures = snap.gestures
        running = gestures.daemon_running
        self.gesture_value.setText("Running" if running else "Stopped")
        self.gesture_sub.setText(
            f"{gestures.reports_seen:,} reports  ·  "
            f"{gestures.gestures_emitted:,} shortcuts emitted"
        )
        self.gesture_toggle.setChecked(gestures.daemon_enabled)

    def refresh(self) -> None:
        if self.state.snapshot is not None:
            self._on_snapshot(self.state.snapshot)
