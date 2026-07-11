"""Power profile selection, editing, automatic switching, and telemetry."""

from __future__ import annotations

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QWidget,
)

from honor_control.core.models import POWER_PROFILES, PowerProfileEntry, SystemSnapshot
from honor_control.frontend.gui.pages import PageBase
from honor_control.frontend.gui.widgets import Card, InfoRow


class PowerPage(PageBase):
    """Apply, create, and fully edit CPU power profiles."""

    title = "Power"
    icon = "preferences-system-power"

    def _build(self) -> None:
        self._profiles: dict[str, PowerProfileEntry] = {}
        self._profile_dirty = False
        self._auto_dirty = False
        self._loading = False

        active = Card("Active power profile")
        active_row = QHBoxLayout()
        self.active_combo = QComboBox()
        self.active_apply = QPushButton("Apply profile")
        self.active_apply.clicked.connect(
            lambda: self.intent.emit("set_profile", (self.active_combo.currentData(),))
        )
        active_row.addWidget(self.active_combo, 1)
        active_row.addWidget(self.active_apply)
        active.layout.addLayout(active_row)
        self.register_control(self.active_combo)
        self.register_control(self.active_apply)
        self.add_widget(active)

        editor = Card("Profile editor")
        pick_row = QHBoxLayout()
        self.edit_combo = QComboBox()
        self.profile_new = QPushButton("New profile")
        self.profile_new.clicked.connect(self._new_profile)
        self.edit_combo.currentIndexChanged.connect(self._load_profile_editor)
        pick_row.addWidget(QLabel("Edit:"))
        pick_row.addWidget(self.edit_combo, 1)
        pick_row.addWidget(self.profile_new)
        editor.layout.addLayout(pick_row)

        form_widget = QWidget()
        form = QFormLayout(form_widget)
        self.profile_name = QLineEdit()
        self.profile_name.setPlaceholderText("lowercase-name")
        self.profile_label = QLineEdit()
        self.profile_description = QLineEdit()
        self.pl1 = QDoubleSpinBox()
        self.pl1.setRange(3.0, 100.0)
        self.pl1.setDecimals(1)
        self.pl1.setSuffix(" W")
        self.pl2 = QDoubleSpinBox()
        self.pl2.setRange(3.0, 150.0)
        self.pl2.setDecimals(1)
        self.pl2.setSuffix(" W")
        self.governor = QComboBox()
        self.governor.addItems(["powersave", "performance"])
        self.epp = QComboBox()
        self.epp.addItems(
            [
                "power",
                "balance_power",
                "balance_performance",
                "performance",
                "default",
            ]
        )
        self.ppd = QComboBox()
        self.ppd.addItems(["power-saver", "balanced", "performance"])
        self.turbo = QCheckBox("Allow CPU turbo boost")
        self.max_perf = QSpinBox()
        self.max_perf.setRange(1, 100)
        self.max_perf.setSuffix(" %")
        form.addRow("Identifier", self.profile_name)
        form.addRow("Display name", self.profile_label)
        form.addRow("Description", self.profile_description)
        form.addRow("Sustained limit (PL1)", self.pl1)
        form.addRow("Burst limit (PL2)", self.pl2)
        form.addRow("CPU governor", self.governor)
        form.addRow("Energy preference (EPP)", self.epp)
        form.addRow("Power-profiles-daemon", self.ppd)
        form.addRow("Turbo", self.turbo)
        form.addRow("Maximum CPU performance", self.max_perf)
        editor.layout.addWidget(form_widget)
        for widget in (
            self.profile_name,
            self.profile_label,
            self.profile_description,
            self.pl1,
            self.pl2,
            self.governor,
            self.epp,
            self.ppd,
        ):
            if hasattr(widget, "textChanged"):
                widget.textChanged.connect(self._mark_profile_dirty)
            else:
                widget.currentIndexChanged.connect(self._mark_profile_dirty)
            self.register_control(widget)
        self.pl1.valueChanged.connect(self._mark_profile_dirty)
        self.pl2.valueChanged.connect(self._mark_profile_dirty)
        self.turbo.toggled.connect(self._mark_profile_dirty)
        self.max_perf.valueChanged.connect(self._mark_profile_dirty)
        self.register_control(self.turbo)
        self.register_control(self.max_perf)
        profile_buttons = QHBoxLayout()
        self.profile_save = QPushButton("Save profile")
        self.profile_delete = QPushButton("Delete custom profile")
        self.profile_reset = QPushButton("Reset built-in values")
        self.profile_save.clicked.connect(self._save_profile)
        self.profile_delete.clicked.connect(self._delete_profile)
        self.profile_reset.clicked.connect(self._reset_profile)
        profile_buttons.addStretch(1)
        profile_buttons.addWidget(self.profile_reset)
        profile_buttons.addWidget(self.profile_delete)
        profile_buttons.addWidget(self.profile_save)
        editor.layout.addLayout(profile_buttons)
        for widget in (
            self.edit_combo,
            self.profile_new,
            self.profile_save,
            self.profile_delete,
            self.profile_reset,
        ):
            self.register_control(widget)
        self.add_widget(editor)

        automatic = Card("Automatic AC / battery switching")
        auto_form_widget = QWidget()
        auto_form = QFormLayout(auto_form_widget)
        self.auto_enabled = QCheckBox("Enable automatic switching")
        self.auto_ac = QComboBox()
        self.auto_battery = QComboBox()
        self.auto_ac_script = QLineEdit()
        self.auto_battery_script = QLineEdit()
        self.auto_ac_script.setPlaceholderText("Optional: /absolute/path/script --arg")
        self.auto_battery_script.setPlaceholderText(
            "Optional: /absolute/path/script --arg"
        )
        auto_form.addRow("", self.auto_enabled)
        auto_form.addRow("When connected to AC", self.auto_ac)
        auto_form.addRow("When running on battery", self.auto_battery)
        auto_form.addRow("After switching to AC", self.auto_ac_script)
        auto_form.addRow("After switching to battery", self.auto_battery_script)
        automatic.layout.addWidget(auto_form_widget)
        warning = QLabel(
            "Scripts run as root after a successful transition, without a shell, "
            "with a 15-second timeout. The executable and parent directories must "
            "be root-owned and not group/world-writable."
        )
        warning.setWordWrap(True)
        warning.setStyleSheet("color: rgba(180, 110, 0, 220); font-size: 11px;")
        automatic.layout.addWidget(warning)
        auto_bottom = QHBoxLayout()
        self.script_status = QLabel("Not run")
        self.auto_save = QPushButton("Save automatic settings")
        self.auto_save.clicked.connect(self._save_auto)
        auto_bottom.addWidget(self.script_status, 1)
        auto_bottom.addWidget(self.auto_save)
        automatic.layout.addLayout(auto_bottom)
        for widget in (
            self.auto_enabled,
            self.auto_ac,
            self.auto_battery,
            self.auto_ac_script,
            self.auto_battery_script,
        ):
            if isinstance(widget, QLineEdit):
                widget.textChanged.connect(self._mark_auto_dirty)
            elif isinstance(widget, QCheckBox):
                widget.toggled.connect(self._mark_auto_dirty)
            else:
                widget.currentIndexChanged.connect(self._mark_auto_dirty)
            self.register_control(widget)
        self.register_control(self.auto_save)
        self.add_widget(automatic)

        telemetry = Card("Telemetry")
        self.temp_row = InfoRow("CPU temperature", "—")
        self.fan_row = InfoRow("Fan speed", "—")
        self.ac_row = InfoRow("Power source", "—")
        self.profile_row = InfoRow("Applied profile", "—")
        for row in (self.temp_row, self.fan_row, self.ac_row, self.profile_row):
            telemetry.layout.addWidget(row)
        self.add_widget(telemetry)
        self.add_stretch()
        self.state.snapshot_changed.connect(self._on_snapshot)

    def _profile_entries(self, snap: SystemSnapshot) -> tuple[PowerProfileEntry, ...]:
        return snap.power.profiles or POWER_PROFILES

    def _on_snapshot(self, snap: SystemSnapshot) -> None:
        entries = self._profile_entries(snap)
        names = [entry.name for entry in entries]
        self._profiles = {entry.name: entry for entry in entries}
        editor_name = self.profile_name.text().strip()
        if self._profile_dirty and self._editor_matches(
            self._profiles.get(editor_name)
        ):
            self._profile_dirty = False
        combo_state = [
            (self.active_combo.itemData(i), self.active_combo.itemText(i))
            for i in range(self.active_combo.count())
        ]
        desired_combo_state = [(entry.name, entry.label) for entry in entries]
        if combo_state != desired_combo_state:
            active_name = self.active_combo.currentData()
            edit_name = (
                editor_name if editor_name in names else self.edit_combo.currentData()
            )
            for combo in (
                self.active_combo,
                self.edit_combo,
                self.auto_ac,
                self.auto_battery,
            ):
                combo.blockSignals(True)
                combo.clear()
                for entry in entries:
                    combo.addItem(entry.label, entry.name)
                combo.blockSignals(False)
            self._select_data(
                self.active_combo, active_name or snap.power.desired_profile
            )
            self._select_data(self.edit_combo, edit_name or names[0])
            if not self._profile_dirty:
                self._load_profile_editor()
        self._select_data(
            self.active_combo, snap.power.applied_profile or snap.power.desired_profile
        )
        if self._auto_matches(snap):
            self._auto_dirty = False
        if not self._auto_dirty:
            self._loading = True
            self.auto_enabled.setChecked(snap.power.auto_switch_enabled)
            self._select_data(self.auto_ac, snap.power.auto_switch_on_ac)
            self._select_data(self.auto_battery, snap.power.auto_switch_on_battery)
            self.auto_ac_script.setText(snap.power.auto_switch_on_ac_script)
            self.auto_battery_script.setText(snap.power.auto_switch_on_battery_script)
            self._loading = False
        self.script_status.setText(
            snap.power.auto_switch_last_script_status or "Not run"
        )
        self.profile_row.set_value(snap.power.applied_profile or "Not reported")
        temp = snap.fan.temp_mc
        self.temp_row.set_value(
            f"{temp // 1000} °C" if temp is not None else "Unavailable"
        )
        if snap.fan.target_speed is not None:
            self.fan_row.set_value(f"Target {snap.fan.target_speed} %")
        elif snap.fan.measured_rpm is not None:
            self.fan_row.set_value(f"{snap.fan.measured_rpm} RPM")
        elif snap.fan.available:
            self.fan_row.set_value("Firmware controlled (RPM not exposed)")
        else:
            self.fan_row.set_value("Unavailable")
        self.ac_row.set_value(
            "AC"
            if snap.power.ac_online is True
            else "Battery"
            if snap.power.ac_online is False
            else "Unknown"
        )

    def _editor_matches(self, profile: PowerProfileEntry | None) -> bool:
        if profile is None:
            return False
        return (
            self.profile_label.text().strip() == profile.label
            and self.profile_description.text().strip() == profile.description
            and round(self.pl1.value() * 1_000_000) == profile.pl1_uw
            and round(self.pl2.value() * 1_000_000) == profile.pl2_uw
            and self.governor.currentText() == profile.governor
            and self.epp.currentText() == profile.epp
            and self.ppd.currentText() == profile.ppd_profile
            and self.turbo.isChecked() == profile.turbo_enabled
            and self.max_perf.value() == profile.max_perf_pct
        )

    def _auto_matches(self, snap: SystemSnapshot) -> bool:
        if not self._auto_dirty:
            return True
        return (
            self.auto_enabled.isChecked() == snap.power.auto_switch_enabled
            and self.auto_ac.currentData() == snap.power.auto_switch_on_ac
            and self.auto_battery.currentData() == snap.power.auto_switch_on_battery
            and self.auto_ac_script.text().strip()
            == snap.power.auto_switch_on_ac_script
            and self.auto_battery_script.text().strip()
            == snap.power.auto_switch_on_battery_script
        )

    @staticmethod
    def _select_data(combo: QComboBox, value: object) -> None:
        index = combo.findData(value)
        if index >= 0 and index != combo.currentIndex():
            combo.blockSignals(True)
            combo.setCurrentIndex(index)
            combo.blockSignals(False)

    def _load_profile_editor(self) -> None:
        name = self.edit_combo.currentData()
        profile = self._profiles.get(str(name))
        if profile is None:
            return
        self._loading = True
        self.profile_name.setText(profile.name)
        self.profile_name.setEnabled(False)
        self.profile_label.setText(profile.label)
        self.profile_description.setText(profile.description)
        self.pl1.setValue(profile.pl1_uw / 1_000_000)
        self.pl2.setValue(profile.pl2_uw / 1_000_000)
        self.governor.setCurrentText(profile.governor)
        self.epp.setCurrentText(profile.epp)
        self.ppd.setCurrentText(profile.ppd_profile)
        self.turbo.setChecked(profile.turbo_enabled)
        self.max_perf.setValue(profile.max_perf_pct)
        self.profile_delete.setEnabled(not profile.built_in)
        self.profile_reset.setEnabled(profile.built_in)
        self._loading = False
        self._profile_dirty = False

    def _new_profile(self) -> None:
        self._loading = True
        self.profile_name.setEnabled(True)
        self.profile_name.setText("custom")
        self.profile_label.setText("Custom")
        self.profile_description.setText("")
        self.pl1.setValue(25.0)
        self.pl2.setValue(35.0)
        self.governor.setCurrentText("powersave")
        self.epp.setCurrentText("balance_power")
        self.ppd.setCurrentText("balanced")
        self.turbo.setChecked(True)
        self.max_perf.setValue(100)
        self.profile_delete.setEnabled(False)
        self.profile_reset.setEnabled(False)
        self._loading = False
        self._profile_dirty = True

    def _mark_profile_dirty(self, *_args) -> None:
        if not self._loading:
            self._profile_dirty = True

    def _save_profile(self) -> None:
        self.intent.emit(
            "save_power_profile",
            (
                self.profile_name.text().strip(),
                self.profile_label.text().strip(),
                self.profile_description.text().strip(),
                round(self.pl1.value() * 1_000_000),
                round(self.pl2.value() * 1_000_000),
                self.governor.currentText(),
                self.epp.currentText(),
                self.ppd.currentText(),
                self.turbo.isChecked(),
                self.max_perf.value(),
            ),
        )

    def _reset_profile(self) -> None:
        name = str(self.edit_combo.currentData())
        factory = next((profile for profile in POWER_PROFILES if profile.name == name), None)
        if factory is None:
            return
        self._loading = True
        self.profile_label.setText(factory.label)
        self.profile_description.setText(factory.description)
        self.pl1.setValue(factory.pl1_uw / 1_000_000)
        self.pl2.setValue(factory.pl2_uw / 1_000_000)
        self.governor.setCurrentText(factory.governor)
        self.epp.setCurrentText(factory.epp)
        self.ppd.setCurrentText(factory.ppd_profile)
        self.turbo.setChecked(factory.turbo_enabled)
        self.max_perf.setValue(factory.max_perf_pct)
        self._loading = False
        self._profile_dirty = True

    def _delete_profile(self) -> None:
        name = self.edit_combo.currentData()
        if name:
            self.intent.emit("delete_power_profile", (name,))

    def _mark_auto_dirty(self, *_args) -> None:
        if not self._loading:
            self._auto_dirty = True

    def _save_auto(self) -> None:
        self.intent.emit(
            "configure_auto_switch",
            (
                self.auto_enabled.isChecked(),
                self.auto_ac.currentData(),
                self.auto_battery.currentData(),
                self.auto_ac_script.text().strip(),
                self.auto_battery_script.text().strip(),
            ),
        )

    def refresh(self) -> None:
        snap = self.state.snapshot
        if snap is not None:
            self._on_snapshot(snap)
