"""Battery page: charge limit slider + charge-mode presets.

Shows observed thresholds as primary; shows desired mismatch warning.
Treats Custom as editor state, not an RPC preset.  Edits are staged here and
sent from the window's single Apply bar; success waits for the returned
verified snapshot sequence.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QButtonGroup,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QSlider,
    QSpinBox,
    QWidget,
)

from honor_control.core.errors import OperationStatus
from honor_control.core.models import CHARGE_PRESETS, SystemSnapshot
from honor_control.frontend.gui.pages import PageBase, PendingChange
from honor_control.frontend.gui.widgets import Card, InfoRow

MIN_LIMIT = 45
MAX_LIMIT = 100


class BatteryPage(PageBase):
    """Battery status, charge-limit slider and mode presets."""

    title = "Battery"
    icon = "battery-good"

    def _build(self) -> None:
        #: Preset the user picked but has not applied yet.
        self._staged_mode: str | None = None
        #: The slider/spin was moved by the user and not applied yet.
        self._limit_touched = False
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
        self.register_control(self.slider)
        self.register_control(self.spin)
        self.add_widget(limit)

        modes = Card("Charge modes")
        self.mode_group = QButtonGroup(self)
        self.mode_group.setExclusive(True)
        self.mode_buttons: dict[str, QPushButton] = {}
        for mode in ("off", "home", "travel", "storage"):
            btn = QPushButton(mode.capitalize())
            btn.setCheckable(True)
            self.mode_group.addButton(btn)
            btn.setToolTip(
                f"{mode}: {CHARGE_PRESETS[mode][0]}% end / {CHARGE_PRESETS[mode][1]}% start"
            )
            self.register_control(btn)
            self.register_pending_control("set_mode", btn)
            self.mode_buttons[mode] = btn
            btn.clicked.connect(
                lambda _checked=False, name=mode: self._stage_mode(name)
            )
        row = QHBoxLayout()
        for mode in ("off", "home", "travel", "storage"):
            row.addWidget(self.mode_buttons[mode])
        wrap = QWidget()
        wrap.setLayout(row)
        wrap.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        modes.layout.addWidget(wrap)
        self.mode_help = QLabel(
            "Select a preset to set its start and stop thresholds, then use "
            "Apply changes."
        )
        self.mode_help.setProperty("muted", True)
        modes.layout.addWidget(self.mode_help)
        self.add_widget(modes)

        self.add_stretch()
        self.state.snapshot_changed.connect(self._on_snapshot)
        self.state.operation_completed.connect(self._on_operation_completed)

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
            if self._pending_kind() is None and not self.slider.isSliderDown():
                self._staged_mode = None
                self._limit_touched = False
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
        if self._pending_kind() is None:
            for name, btn in self.mode_buttons.items():
                btn.setChecked(name == mode)
            preset = CHARGE_PRESETS.get(mode)
            if preset is not None:
                self.mode_help.setText(
                    f"Starts at {preset[1]}%  ·  Stops at {preset[0]}%"
                )
        self.notify_changes()

    def _on_slider_changed(self, value: int) -> None:
        self.spin.blockSignals(True)
        self.spin.setValue(value)
        self.spin.blockSignals(False)
        self._limit_edited()

    def _on_spin_changed(self, value: int) -> None:
        self.slider.blockSignals(True)
        self.slider.setValue(value)
        self.slider.blockSignals(False)
        self._limit_edited()

    # -- Staged edits (applied from the window's Apply bar) --

    def _limit_edited(self) -> None:
        """A custom limit replaces any staged preset."""
        self._staged_mode = None
        self._limit_touched = True
        self.mode_group.setExclusive(False)
        for button in self.mode_buttons.values():
            button.setChecked(False)
        self.mode_group.setExclusive(True)
        self.notify_changes()

    def _stage_mode(self, name: str) -> None:
        self._staged_mode = name
        self._limit_touched = False
        self._block_value(CHARGE_PRESETS[name][0])
        self.notify_changes()

    def _pending_kind(self) -> str | None:
        """Return ``"mode"``, ``"limit"`` or ``None`` for the staged edit."""
        snap = self.state.snapshot
        bat = snap.battery if snap is not None else None
        if self._staged_mode is not None:
            current = str(bat.mode) if bat is not None and bat.mode else ""
            return "mode" if self._staged_mode != current else None
        if self._limit_touched:
            end = bat.observed_end if bat is not None else None
            return "limit" if end is None or self.spin.value() != end else None
        return None

    def pending_changes(self) -> list[PendingChange]:
        kind = self._pending_kind()
        if kind == "mode":
            return [PendingChange(f"Charge mode: {str(self._staged_mode).capitalize()}")]
        if kind == "limit":
            return [PendingChange(f"Charge limit: {self.spin.value()}%")]
        return []

    def apply_pending(self) -> None:
        kind = self._pending_kind()
        if kind == "mode":
            self.intent.emit("set_mode", (self._staged_mode,))
        elif kind == "limit":
            self.intent.emit(
                "set_thresholds", (self.spin.value(), self.spin.value() - 5)
            )

    def discard_pending(self) -> None:
        self._staged_mode = None
        self._limit_touched = False
        self.refresh()
        self.notify_changes()

    def _on_operation_completed(self, operation_id: str, result: object) -> None:
        method = operation_id.split(":", 1)[0]
        if method not in {"set_mode", "set_thresholds"}:
            return
        if (
            result is not None
            and getattr(result, "status", None) == OperationStatus.SUCCESS
        ):
            # Keep what the user chose on screen; the verified snapshot that
            # follows confirms it.
            self._staged_mode = None
            self._limit_touched = False
        self.notify_changes()

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
