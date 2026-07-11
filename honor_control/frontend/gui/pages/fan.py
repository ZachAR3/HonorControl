"""Fan page: live thermal readout, per-profile fan curves, and manual control.

Modes: stock (auto), curve, manual_override.  The curve editor uses a
``FanCurveGraph`` value model as the sole source of points.  Manual override
has a required TTL and is never persisted across restart.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QButtonGroup,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QRadioButton,
    QSlider,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from honor_control.core.models import POWER_PROFILES, FanMode, SystemSnapshot
from honor_control.core.validation import format_curve, parse_curve
from honor_control.frontend.gui.fan_curve_editor import FanCurveGraph
from honor_control.frontend.gui.pages import PageBase
from honor_control.frontend.gui.widgets import Card, InfoRow


class FanPage(PageBase):
    """Fan status + mode selector (stock / custom curve / manual)."""

    title = "Fan"
    icon = "preferences-system-performance"

    def _build(self) -> None:
        self._curve_dirty = False
        self._mode_dirty = False
        self._loading_curve = False
        status = Card("Fan status")
        self.temp_row = InfoRow("Temperature", "—")
        self.speed_row = InfoRow("Target speed", "—")
        self.rpm_row = InfoRow("Measured speed", "—")
        self.mode_row = InfoRow("Mode", "—")
        for row in (self.temp_row, self.speed_row, self.rpm_row, self.mode_row):
            status.layout.addWidget(row)
        self.add_widget(status)

        mode_card = Card("Fan mode")
        self.mode_group = QButtonGroup(self)
        self.mode_group.setExclusive(True)
        self.rb_auto = QRadioButton("Stock (Auto — EC firmware curve)")
        self.rb_curve = QRadioButton("Custom curve")
        self.rb_curve.setToolTip("Configure and activate a validated temperature curve")
        self.rb_manual = QRadioButton("Manual speed")
        self.mode_group.addButton(self.rb_auto, 0)
        self.mode_group.addButton(self.rb_curve, 1)
        self.mode_group.addButton(self.rb_manual, 2)
        mode_card.layout.addWidget(self.rb_auto)
        mode_card.layout.addWidget(self.rb_curve)
        mode_card.layout.addWidget(self.rb_manual)

        self.manual_widget = QWidget()
        mh = QHBoxLayout(self.manual_widget)
        mh.setContentsMargins(20, 4, 4, 4)
        self.manual_slider = QSlider(Qt.Orientation.Horizontal)
        self.manual_slider.setRange(0, 100)
        self.manual_slider.setSingleStep(5)
        self.manual_slider.setValue(50)
        self.manual_label = QLabel("50 %")
        self.manual_label.setMinimumWidth(50)
        self.manual_slider.valueChanged.connect(
            lambda v: self.manual_label.setText(f"{v} %")
        )
        self.manual_apply = QPushButton("Apply")
        self.manual_apply.setToolTip(
            "Temporarily overrides firmware fan control for 5 minutes"
        )
        self.manual_apply.clicked.connect(
            lambda: self.intent.emit("set_manual", (self.manual_slider.value(), 300))
        )
        mh.addWidget(self.manual_slider, 1)
        mh.addWidget(self.manual_label)
        mh.addWidget(self.manual_apply)
        mode_card.layout.addWidget(self.manual_widget)
        self.manual_widget.setVisible(False)

        self.register_control(self.rb_auto)
        self.register_control(self.rb_curve)
        self.register_control(self.rb_manual)
        self.register_control(self.manual_slider)
        self.register_control(self.manual_apply)
        self.add_widget(mode_card)

        # Curve editor (visible only in custom mode)
        self.curve_widget = QWidget()
        cl = QVBoxLayout(self.curve_widget)
        cl.setContentsMargins(0, 0, 0, 0)
        profile_row = QHBoxLayout()
        profile_row.addWidget(QLabel("Profile:"))
        self.profile_combo = QComboBox()
        self.profile_combo.addItems(
            ["default", *(entry.name for entry in POWER_PROFILES)]
        )
        profile_row.addWidget(self.profile_combo, 1)
        cl.addLayout(profile_row)
        self.curve_graph = FanCurveGraph()
        self.curve_graph.points_changed.connect(self._curve_changed)
        self.curve_graph.selection_changed.connect(self._selection_changed)
        cl.addWidget(self.curve_graph)
        point_row = QHBoxLayout()
        self.point_temp = QSpinBox()
        self.point_temp.setRange(40, 100)
        self.point_temp.setSuffix(" °C")
        self.point_speed = QSpinBox()
        self.point_speed.setRange(0, 100)
        self.point_speed.setSuffix(" %")
        self.point_temp.valueChanged.connect(self._point_values_changed)
        self.point_speed.valueChanged.connect(self._point_values_changed)
        self.point_add = QPushButton("Add point")
        self.point_remove = QPushButton("Remove selected")
        self.point_add.clicked.connect(self.curve_graph.add_point)
        self.point_remove.clicked.connect(self.curve_graph.remove_selected)
        point_row.addWidget(QLabel("Selected:"))
        point_row.addWidget(self.point_temp)
        point_row.addWidget(self.point_speed)
        point_row.addStretch(1)
        point_row.addWidget(self.point_add)
        point_row.addWidget(self.point_remove)
        cl.addLayout(point_row)
        self.curve_hint = QLabel(
            "Click to add, drag to edit, right-click to remove. "
            "2–12 points; 95 °C and above must be 100%."
        )
        self.curve_hint.setStyleSheet(
            "color: rgba(128, 128, 128, 200); font-size: 11px;"
        )
        self.curve_hint.setWordWrap(True)
        cl.addWidget(self.curve_hint)
        self.curve_apply = QPushButton("Validate and apply curve")
        self.curve_apply.clicked.connect(self._apply_curve)
        cl.addWidget(self.curve_apply)
        self.register_control(self.profile_combo)
        self.register_control(self.curve_graph)
        self.register_control(self.point_temp)
        self.register_control(self.point_speed)
        self.register_control(self.point_add)
        self.register_control(self.point_remove)
        self.register_control(self.curve_apply)
        self.add_widget(self.curve_widget)
        self.curve_widget.setVisible(False)

        self.rb_auto.toggled.connect(self._on_mode_changed)
        self.rb_auto.clicked.connect(lambda: self._set_mode_dirty())
        self.rb_auto.clicked.connect(lambda: self.intent.emit("set_stock_auto", ()))
        self.rb_curve.toggled.connect(self._on_mode_changed)
        self.rb_curve.clicked.connect(lambda: self._set_mode_dirty())
        self.rb_manual.toggled.connect(self._on_mode_changed)
        self.rb_manual.clicked.connect(lambda: self._set_mode_dirty())
        self.profile_combo.currentTextChanged.connect(self._profile_changed)

        self.add_stretch()
        self.state.snapshot_changed.connect(self._on_snapshot)

    def _on_snapshot(self, snap: SystemSnapshot) -> None:
        fan = snap.fan
        profile_names = ["default", *(profile.name for profile in snap.power.profiles)]
        if profile_names == ["default"]:
            profile_names.extend(entry.name for entry in POWER_PROFILES)
        existing_names = [
            self.profile_combo.itemText(index)
            for index in range(self.profile_combo.count())
        ]
        if existing_names != profile_names:
            selected = self.profile_combo.currentText()
            self.profile_combo.blockSignals(True)
            self.profile_combo.clear()
            self.profile_combo.addItems(profile_names)
            index = self.profile_combo.findText(selected)
            self.profile_combo.setCurrentIndex(max(0, index))
            self.profile_combo.blockSignals(False)
        temp = fan.temp_mc
        try:
            self.temp_row.set_value(f"{int(temp) // 1000} °C" if temp else "—")
        except (TypeError, ValueError):
            self.temp_row.set_value("—")
        speed = fan.target_speed
        self.speed_row.set_value(
            f"{speed} %"
            if speed is not None
            else "Firmware controlled"
            if fan.available
            else "Unavailable"
        )
        self.rpm_row.set_value(
            f"{fan.measured_rpm} RPM"
            if fan.measured_rpm is not None
            else "Not exposed by hardware"
            if fan.available
            else "Unavailable"
        )
        self.mode_row.set_value(str(fan.mode))
        saved = fan.curves.get(self.profile_combo.currentText(), "")
        current = format_curve(list(self.curve_graph.points))
        if fan.mode == FanMode.CURVE and saved == current:
            self._curve_dirty = False
            self._mode_dirty = False
        elif (
            (fan.mode == FanMode.STOCK and self.rb_auto.isChecked())
            or (
                fan.mode == FanMode.MANUAL_OVERRIDE and self.rb_manual.isChecked()
            )
        ):
            self._mode_dirty = False
        if not self._mode_dirty:
            if fan.mode == FanMode.CURVE:
                self.rb_curve.setChecked(True)
            elif fan.mode == FanMode.MANUAL_OVERRIDE:
                self.rb_manual.setChecked(True)
            else:
                self.rb_auto.setChecked(True)
        self._load_selected_curve()

    def _on_mode_changed(self) -> None:
        is_manual = self.rb_manual.isChecked()
        is_curve = self.rb_curve.isChecked()
        self.manual_widget.setVisible(is_manual)
        self.curve_widget.setVisible(is_curve)

    def _set_mode_dirty(self) -> None:
        self._mode_dirty = True

    def _profile_changed(self) -> None:
        self._curve_dirty = False
        self._load_selected_curve(force=True)

    def _load_selected_curve(self, force: bool = False) -> None:
        snap = self.state.snapshot
        if snap is None or (self._curve_dirty and not force):
            return
        curve = snap.fan.curves.get(self.profile_combo.currentText(), "")
        if not curve:
            curve = "40000:0,60000:30,80000:65,95000:100"
        try:
            self._loading_curve = True
            self.curve_graph.set_points(parse_curve(curve))
            self._curve_dirty = False
        finally:
            self._loading_curve = False

    def _curve_changed(self, _points: object) -> None:
        if not self._loading_curve:
            self._curve_dirty = True
            self.curve_hint.setText("Unsaved curve changes")

    def _selection_changed(self, index: int) -> None:
        enabled = index >= 0
        self.point_temp.setEnabled(enabled)
        self.point_speed.setEnabled(enabled)
        self.point_remove.setEnabled(enabled and len(self.curve_graph.points) > 2)
        if not enabled:
            return
        point = self.curve_graph.points[index]
        self.point_temp.blockSignals(True)
        self.point_speed.blockSignals(True)
        self.point_temp.setValue(point.temp_mc // 1000)
        self.point_speed.setValue(point.speed)
        self.point_temp.blockSignals(False)
        self.point_speed.blockSignals(False)

    def _point_values_changed(self) -> None:
        self.curve_graph.update_selected(
            self.point_temp.value() * 1000,
            self.point_speed.value(),
        )

    def _apply_curve(self) -> None:
        try:
            points = parse_curve(format_curve(list(self.curve_graph.points)))
        except Exception as exc:  # validation feedback stays in the page
            self.curve_hint.setText(str(exc))
            return
        self.curve_hint.setText("Curve is valid; applying through the service…")
        self._mode_dirty = True
        self.intent.emit(
            "set_curve",
            (
                self.profile_combo.currentText(),
                [(point.temp_mc, point.speed) for point in points],
            ),
        )

    def refresh(self) -> None:
        snap = self.state.snapshot
        if snap is not None:
            self._on_snapshot(snap)
