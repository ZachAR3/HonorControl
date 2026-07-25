"""Battery page: charge limit slider + charge-mode presets.

Shows observed thresholds as primary; shows desired mismatch warning.
Treats Custom as editor state, not an RPC preset.  Pending state
disables Apply only; success waits for returned verified snapshot
sequence.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QSlider,
    QSpinBox,
    QWidget,
)

from honor_control.core.models import CHARGE_PRESETS, SystemSnapshot
from honor_control.frontend.gui.pages import PageBase
from honor_control.frontend.gui.widgets import Card, InfoRow

MIN_LIMIT = 45
MAX_LIMIT = 100


class BatteryPage(PageBase):
    """Battery status, charge-limit slider and mode presets."""

    title = "Battery"
    icon = "battery-good"

    def _build(self) -> None:
        cap = Card("")
        cap_row = QHBoxLayout()
        self.capacity_value = QLabel("— %")
        self.capacity_value.setObjectName("HonorPrimaryValue")
        cap_row.addWidget(self.capacity_value)
        cap_row.addSpacing(24)
        self.status_summary = QLabel("—")
        self.status_summary.setProperty("muted", True)
        cap_row.addWidget(self.status_summary)
        cap_row.addStretch(1)
        self.health_summary = QLabel("—")
        self.health_summary.setProperty("muted", True)
        cap_row.addWidget(self.health_summary)
        cap.layout.addLayout(cap_row)
        self.status_row = InfoRow("Status", "—")
        self.ac_row = InfoRow("Power source", "—")
        self.threshold_row = InfoRow("Thresholds", "—")
        self.status_row.setVisible(False)
        self.ac_row.setVisible(False)
        self.threshold_row.setVisible(False)
        self.add_widget(cap)

        limit = Card("Charge limit")
        ll = QHBoxLayout()
        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.setRange(MIN_LIMIT, MAX_LIMIT)
        self.slider.setSingleStep(5)
        self.slider.setPageStep(5)
        self.spin = QSpinBox()
        self.spin.setRange(MIN_LIMIT, MAX_LIMIT)
        self.spin.setSuffix(" %")
        self.slider.valueChanged.connect(self._on_slider_changed)
        self.spin.valueChanged.connect(self._on_spin_changed)
        ll.addWidget(self.slider, 1)
        ll.addWidget(self.spin)
        limit.layout.addLayout(ll)
        self.limit_help = QLabel("Recommended: 80% for daily use, 60% for storage.")
        self.limit_help.setProperty("muted", True)
        limit.layout.addWidget(self.limit_help)
        self.apply_btn = QPushButton("Apply charge limit")
        self.apply_btn.setProperty("quiet", True)
        self.apply_btn.clicked.connect(
            lambda: self.intent.emit(
                "set_thresholds", (self.spin.value(), self.spin.value() - 5)
            )
        )
        self.register_control(self.slider)
        self.register_control(self.spin)
        self.register_pending_control("set_thresholds", self.apply_btn)
        self.add_widget(limit)

        modes = Card("Charge modes")
        self.mode_buttons: dict[str, QPushButton] = {}
        for mode in ("off", "home", "travel", "storage"):
            btn = QPushButton(mode.capitalize())
            btn.setCheckable(True)
            btn.setToolTip(
                f"{mode}: {CHARGE_PRESETS[mode][0]}% end / {CHARGE_PRESETS[mode][1]}% start"
            )
            self.register_control(btn)
            self.register_pending_control("set_mode", btn)
            self.mode_buttons[mode] = btn
            btn.clicked.connect(
                lambda _checked=False, name=mode: self.intent.emit("set_mode", (name,))
            )
        row = QHBoxLayout()
        for mode in ("off", "home", "travel", "storage"):
            row.addWidget(self.mode_buttons[mode])
        wrap = QWidget()
        wrap.setLayout(row)
        wrap.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        modes.layout.addWidget(wrap)
        self.mode_help = QLabel("Select a preset to set its start and stop thresholds.")
        self.mode_help.setProperty("muted", True)
        modes.layout.addWidget(self.mode_help)
        self.add_widget(modes)

        actions = Card("")
        action_row = QHBoxLayout()
        action_row.addWidget(self.apply_btn)
        action_row.addStretch(1)
        self.applied_label = QLabel("Changes are verified after applying")
        self.applied_label.setProperty("muted", True)
        action_row.addWidget(self.applied_label)
        actions.layout.addLayout(action_row)
        self.add_widget(actions)

        self.add_stretch()
        self.state.snapshot_changed.connect(self._on_snapshot)

    def _on_snapshot(self, snap: SystemSnapshot) -> None:
        bat = snap.battery
        cap = bat.capacity_percent
        self.capacity_value.setText(f"{cap} %" if cap is not None else "— %")
        self.status_row.set_value(str(bat.status) if bat.status else "—")
        self.ac_row.set_value(
            "AC"
            if bat.ac_online is True
            else "Battery"
            if bat.ac_online is False
            else "—"
        )
        status = str(bat.status) if bat.status else "Unknown"
        source = (
            "Connected to AC"
            if bat.ac_online is True
            else "On battery"
            if bat.ac_online is False
            else "Power source unknown"
        )
        self.status_summary.setText(f"{status}  ·  {source}")
        self.health_summary.setText("Healthy" if bat.available else "Unavailable")
        end = bat.observed_end
        start = bat.observed_start
        if end is not None and start is not None:
            self.threshold_row.set_value(f"{end}% stop · {start}% start")
            if not self.slider.isSliderDown():
                self._block_value(end)
        else:
            self.threshold_row.set_value("—")
        if (
            bat.desired_end is not None
            and bat.desired_start is not None
            and (bat.desired_end, bat.desired_start) != (end, start)
        ):
            self.limit_help.setText(
                f"Desired {bat.desired_end}%/{bat.desired_start}% is not applied."
            )
        else:
            self.limit_help.setText("Recommended: 80% for daily use, 60% for storage.")
        mode = str(bat.mode) if bat.mode else ""
        for name, btn in self.mode_buttons.items():
            btn.setChecked(name == mode)
        preset = CHARGE_PRESETS.get(mode)
        if preset is not None:
            self.mode_help.setText(f"Starts at {preset[1]}%  ·  Stops at {preset[0]}%")

    def _on_slider_changed(self, value: int) -> None:
        self.spin.blockSignals(True)
        self.spin.setValue(value)
        self.spin.blockSignals(False)

    def _on_spin_changed(self, value: int) -> None:
        self.slider.blockSignals(True)
        self.slider.setValue(value)
        self.slider.blockSignals(False)

    def _block_value(self, value: int) -> None:
        self.slider.blockSignals(True)
        self.spin.blockSignals(True)
        self.slider.setValue(value)
        self.spin.setValue(value)
        self.slider.blockSignals(False)
        self.spin.blockSignals(False)

    def refresh(self) -> None:
        snap = self.state.snapshot
        if snap is not None:
            self._on_snapshot(snap)
