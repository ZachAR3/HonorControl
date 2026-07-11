"""Dashboard page: at-a-glance overview of the whole system.

Renders only :class:`SystemSnapshot`; emits no commands.  Shows platform
match confidence and overall health.  Missing data is Unknown, never No.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QSizePolicy,
    QWidget,
)

from honor_control.core.models import SystemSnapshot
from honor_control.frontend.gui.pages import PageBase
from honor_control.frontend.gui.widgets import Card, InfoRow


def _milli_to_celsius(v: int | None) -> str:
    if v is None:
        return "—"
    try:
        return f"{int(v) // 1000} °C"
    except (TypeError, ValueError):
        return "—"


class DashboardPage(PageBase):
    """Overview page summarising service, power, battery and capabilities."""

    title = "Dashboard"
    icon = "go-home"

    def _build(self) -> None:
        headline = QHBoxLayout()
        headline.setSpacing(14)
        self.profile_card = Card("Power profile")
        self.profile_value = QLabel("—")
        self.profile_value.setStyleSheet("font-size: 22px; font-weight: 600;")
        self.profile_value.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.profile_card.layout.addWidget(self.profile_value)
        headline.addWidget(self.profile_card)

        self.battery_card = Card("Battery")
        self.battery_value = QLabel("— %")
        self.battery_value.setStyleSheet("font-size: 22px; font-weight: 600;")
        self.battery_value.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.battery_card.layout.addWidget(self.battery_value)
        self.battery_sub = QLabel("—")
        self.battery_sub.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.battery_card.layout.addWidget(self.battery_sub)
        headline.addWidget(self.battery_card)

        self.fan_card = Card("Thermal")
        self.fan_value = QLabel("— °C")
        self.fan_value.setStyleSheet("font-size: 22px; font-weight: 600;")
        self.fan_value.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.fan_card.layout.addWidget(self.fan_value)
        self.fan_sub = QLabel("—")
        self.fan_sub.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.fan_card.layout.addWidget(self.fan_sub)
        headline.addWidget(self.fan_card)
        self.add_widget(self._wrap(headline))

        grid = QGridLayout()
        grid.setSpacing(10)
        self.rows: dict[str, InfoRow] = {}
        for r, key in enumerate(("model", "cpu_model", "service")):
            row = InfoRow(self._label_for(key))
            grid.addWidget(row, r, 0)
            self.rows[key] = row
        self.cap_rows: list[tuple[str, InfoRow]] = []
        for r, key in enumerate(("battery", "power", "fan", "gestures", "gpu")):
            row = InfoRow(key, "—")
            grid.addWidget(row, r, 1)
            self.cap_rows.append((key, row))
        wrap = QWidget()
        wrap.setLayout(grid)
        health_card = Card("System & service")
        health_card.layout.addWidget(wrap)
        self.add_widget(health_card)
        self.add_stretch()

        self.state.snapshot_changed.connect(self._on_snapshot)

    def _wrap(self, layout) -> QWidget:
        w = QWidget()
        w.setLayout(layout)
        w.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        return w

    def _label_for(self, key: str) -> str:
        return {"model": "Model", "cpu_model": "CPU", "service": "Service"}.get(
            key, key
        )

    def _on_snapshot(self, snap: SystemSnapshot) -> None:
        """Update all widgets from a fresh system snapshot."""
        # Headline cards.
        self.profile_value.setText(snap.power.applied_profile or "—")
        if snap.battery.capacity_percent is not None:
            self.battery_value.setText(f"{snap.battery.capacity_percent} %")
        else:
            self.battery_value.setText("— %")
        status = str(snap.battery.status) if snap.battery.status else "—"
        ac = (
            "on AC"
            if snap.battery.ac_online is True
            else "on battery"
            if snap.battery.ac_online is False
            else "power source unknown"
        )
        self.battery_sub.setText(f"{status} · {ac}")
        self.fan_value.setText(_milli_to_celsius(snap.fan.temp_mc))
        speed = snap.fan.target_speed
        if speed is not None:
            detail = f"target {speed}%"
        elif snap.fan.measured_rpm is not None:
            detail = f"{snap.fan.measured_rpm} RPM"
        elif snap.fan.available:
            detail = "firmware controlled"
        else:
            detail = "fan data unavailable"
        self.fan_sub.setText(detail)

        # System identity.
        self.rows["model"].set_value(snap.platform.model or "—")
        self.rows["cpu_model"].set_value(snap.platform.cpu_model or "—")
        uptime = snap.service.uptime
        self.rows["service"].set_value(f"{snap.service.overall} · uptime {uptime}s")

        # Capability grid.
        for key, row in self.cap_rows:
            cap = snap.capabilities.get(key)
            if cap is None:
                row.set_value("Unknown")
            else:
                row.set_value(str(cap.status))

    def refresh(self) -> None:
        snap = self.state.snapshot
        if snap is not None:
            self._on_snapshot(snap)
