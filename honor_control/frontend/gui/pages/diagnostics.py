"""Diagnostics page: system info, capabilities, self-test, debug bundle.

Renders severity-aware results and remediation.  Export through a save
dialog in the user process.  Loads logs on demand with bounded lines.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from honor_control.core.errors import CapabilityStatus
from honor_control.core.models import SystemSnapshot
from honor_control.frontend.gui.pages import PageBase
from honor_control.frontend.gui.system_info import LocalSystemInfo, probe_local_system
from honor_control.frontend.gui.widgets import Card, InfoRow, StatusDot


class DiagnosticsPage(PageBase):
    """System info, capabilities, self-test, logs and debug export."""

    _bundle_saved = Signal(str)
    _logs_saved = Signal(str)
    _system_info_ready = Signal(object)

    title = "Diagnostics"
    icon = "preferences-system-devices"

    def _build(self) -> None:
        self._save_logs_requested = False
        info = Card("System information")
        self.info_rows: dict[str, InfoRow] = {}
        for key in (
            "model",
            "cpu_model",
            "hostname",
            "os",
            "kernel",
            "desktop",
            "service",
        ):
            row = InfoRow(self._label(key))
            info.layout.addWidget(row)
            self.info_rows[key] = row
        self.add_widget(info)

        hardware = Card("Live hardware")
        self.hardware_rows: dict[str, InfoRow] = {}
        for key in (
            "memory",
            "graphics",
            "cpu_temperature",
            "gpu_temperature",
            "fan",
            "power_profile",
            "battery",
            "battery_health",
            "battery_cycles",
        ):
            row = InfoRow(self._label(key))
            hardware.layout.addWidget(row)
            self.hardware_rows[key] = row
        self.add_widget(hardware)

        caps = Card("Capabilities")
        self.cap_dots: dict[str, StatusDot] = {}
        self.cap_labels: dict[str, QLabel] = {}
        grid = QVBoxLayout()
        for key in ("battery", "power", "fan", "gestures", "gpu"):
            h = QHBoxLayout()
            h.setContentsMargins(0, 0, 0, 0)
            lbl = QLabel("GPU IRQ mitigation" if key == "gpu" else key.capitalize())
            dot = StatusDot(StatusDot.GREY)
            self.cap_dots[key] = dot
            h.addWidget(lbl)
            h.addStretch(1)
            h.addWidget(dot)
            state_label = QLabel("Checking…")
            state_label.setProperty("muted", True)
            state_label.setMinimumWidth(82)
            self.cap_labels[key] = state_label
            h.addWidget(state_label)
            row = QWidget()
            row.setLayout(h)
            row.setFixedHeight(25)
            grid.addWidget(row)
        wrap = QWidget()
        wrap.setLayout(grid)
        caps.layout.addWidget(wrap)
        self.add_widget(caps)

        actions = Card("Self-test & debug bundle")
        btn_row = QHBoxLayout()
        self.run_btn = QPushButton("Run checks")
        self.export_btn = QPushButton("Export debug bundle")
        self.logs_btn = QPushButton("Refresh logs")
        self.save_logs_btn = QPushButton("Save logs…")
        self.run_btn.clicked.connect(lambda: self.intent.emit("run_checks", ()))
        self.logs_btn.clicked.connect(
            lambda: self.intent.emit("get_recent_logs", (100,))
        )
        self.save_logs_btn.clicked.connect(self._request_log_save)
        self.export_btn.clicked.connect(
            lambda: self.intent.emit("get_debug_bundle", ())
        )
        btn_row.addWidget(self.run_btn)
        btn_row.addWidget(self.export_btn)
        btn_row.addWidget(self.logs_btn)
        btn_row.addWidget(self.save_logs_btn)
        btn_row.addStretch(1)
        actions.layout.addLayout(btn_row)
        self.results_label = QLabel("No checks run yet.")
        self.results_label.setWordWrap(True)
        actions.layout.addWidget(self.results_label)
        self.register_pending_control("run_checks", self.run_btn)
        self.register_pending_control("get_debug_bundle", self.export_btn)
        self.register_pending_control("get_recent_logs", self.logs_btn)
        self.register_pending_control("get_recent_logs", self.save_logs_btn)
        self.add_widget(actions)

        logs_card = Card("Recent backend logs")
        self.logs_view = QTextEdit()
        self.logs_view.setReadOnly(True)
        self.logs_view.setPlaceholderText("Click 'Refresh logs' to load…")
        self.logs_view.setMaximumHeight(200)
        logs_card.layout.addWidget(self.logs_view)
        self.add_widget(logs_card)

        self.add_stretch()
        self.state.snapshot_changed.connect(self._on_snapshot)
        self.state.operation_completed.connect(self._on_operation)
        self._bundle_saved.connect(self.results_label.setText)
        self._logs_saved.connect(self.results_label.setText)
        self._system_info_ready.connect(self._on_local_system_info)
        threading.Thread(
            target=self._probe_local_system,
            name="honor-local-system-probe",
            daemon=True,
        ).start()

    @staticmethod
    def _label(key: str) -> str:
        return {
            "model": "Model",
            "cpu_model": "CPU",
            "hostname": "Host",
            "os": "Operating system",
            "kernel": "Kernel",
            "desktop": "Desktop session",
            "service": "Honor Control service",
            "memory": "Memory",
            "graphics": "Integrated graphics",
            "cpu_temperature": "CPU temperature",
            "gpu_temperature": "GPU temperature",
            "fan": "Cooling",
            "power_profile": "Power profile",
            "battery": "Battery state",
            "battery_health": "Battery health",
            "battery_cycles": "Battery cycles",
        }.get(key, key.replace("_", " ").capitalize())

    def _on_snapshot(self, snap: SystemSnapshot) -> None:
        self.info_rows["model"].set_value(snap.platform.model or "—")
        self.info_rows["cpu_model"].set_value(snap.platform.cpu_model or "—")
        self.info_rows["service"].set_value(
            f"{snap.service.overall} · uptime {snap.service.uptime}s"
        )
        temp = snap.fan.temp_mc
        self.hardware_rows["cpu_temperature"].set_value(
            f"{temp / 1000:.0f} °C" if temp is not None else "Unavailable"
        )
        fan_parts = [str(snap.fan.mode).replace("_", " ").title()]
        if snap.fan.target_speed is not None:
            fan_parts.append(f"target {snap.fan.target_speed}%")
        if snap.fan.measured_rpm is not None:
            fan_parts.append(f"{snap.fan.measured_rpm:,} RPM")
        if not snap.fan.available:
            fan_parts = ["Unavailable"]
        self.hardware_rows["fan"].set_value(" · ".join(fan_parts))
        self.hardware_rows["power_profile"].set_value(
            snap.power.applied_profile or snap.power.desired_profile or "Not reported"
        )
        capacity = snap.battery.capacity_percent
        battery_state = (
            f"{capacity}%" if capacity is not None else "Capacity unavailable"
        )
        if snap.battery.status:
            battery_state += f" · {snap.battery.status}"
        self.hardware_rows["battery"].set_value(battery_state)
        for key, dot in self.cap_dots.items():
            cap = snap.capabilities.get(key)
            if cap is None:
                dot.set_color(StatusDot.GREY)
                dot.setToolTip("Capability has not been probed")
                self.cap_labels[key].setText("Unknown")
            elif cap.status == CapabilityStatus.SUPPORTED:
                dot.set_color(StatusDot.GREEN)
                self.cap_labels[key].setText("Ready")
            elif cap.status == CapabilityStatus.EXPERIMENTAL:
                dot.set_color(StatusDot.AMBER)
                self.cap_labels[key].setText("Experimental")
            elif cap.status == CapabilityStatus.UNAVAILABLE:
                dot.set_color(StatusDot.AMBER)
                self.cap_labels[key].setText("Unavailable")
            elif cap.status == CapabilityStatus.UNSUPPORTED:
                dot.set_color(StatusDot.AMBER)
                self.cap_labels[key].setText("Unsupported")
            elif cap.status == CapabilityStatus.DISABLED:
                dot.set_color(StatusDot.GREY)
                self.cap_labels[key].setText("Disabled")
            else:
                dot.set_color(StatusDot.RED)
                self.cap_labels[key].setText("Error")
            if cap is not None:
                detail = cap.message or cap.reason_code
                dot.setToolTip(detail)
                self.cap_labels[key].setToolTip(detail)

    def refresh(self) -> None:
        snap = self.state.snapshot
        if snap is not None:
            self._on_snapshot(snap)

    def _on_operation(self, operation_id: str, result: object) -> None:
        if operation_id.startswith("run_checks:") and isinstance(result, dict):
            checks = result.get("checks", [])
            self.results_label.setText(
                "\n".join(
                    f"{item.get('severity', '?')}: {item.get('message', '')}"
                    for item in checks
                    if isinstance(item, dict)
                )
                or str(result.get("overall", "No results"))
            )
        elif operation_id.startswith("get_recent_logs:") and isinstance(result, list):
            self.logs_view.setPlainText("\n".join(str(line) for line in result))
            if self._save_logs_requested:
                self._save_logs_requested = False
                self._choose_log_destination(result)
        elif operation_id.startswith("get_recent_logs:"):
            if self._save_logs_requested:
                self._save_logs_requested = False
                self.results_label.setText("Could not load logs for export.")
        elif operation_id.startswith("get_debug_bundle:") and isinstance(result, dict):
            filename, _filter = QFileDialog.getSaveFileName(
                self, "Save debug bundle", "honor-control-debug.json", "JSON (*.json)"
            )
            if filename:
                self.results_label.setText("Saving debug bundle…")
                threading.Thread(
                    target=self._save_debug_bundle,
                    args=(filename, result),
                    name="honor-debug-bundle-save",
                    daemon=True,
                ).start()

    def _probe_local_system(self) -> None:
        self._system_info_ready.emit(probe_local_system())

    def _on_local_system_info(self, info: LocalSystemInfo) -> None:
        self.info_rows["hostname"].set_value(info.hostname)
        self.info_rows["os"].set_value(info.os_name)
        self.info_rows["kernel"].set_value(info.kernel)
        self.info_rows["desktop"].set_value(info.desktop)
        self.hardware_rows["memory"].set_value(info.memory)
        self.hardware_rows["graphics"].set_value(info.graphics)
        self.hardware_rows["gpu_temperature"].set_value(info.gpu_temperature)
        self.hardware_rows["battery_health"].set_value(info.battery_health)
        self.hardware_rows["battery_cycles"].set_value(info.battery_cycles)

    def _request_log_save(self) -> None:
        self._save_logs_requested = True
        self.results_label.setText("Loading logs for export…")
        self.intent.emit("get_recent_logs", (500,))

    def _choose_log_destination(self, logs: list[object]) -> None:
        filename, _filter = QFileDialog.getSaveFileName(
            self,
            "Save Honor Control logs",
            "honor-control.log",
            "Log files (*.log *.txt);;All files (*)",
        )
        if not filename:
            return
        self.results_label.setText("Saving logs…")
        threading.Thread(
            target=self._save_logs,
            args=(filename, logs),
            name="honor-log-save",
            daemon=True,
        ).start()

    def _save_logs(self, filename: str, logs: list[object]) -> None:
        try:
            text = "\n".join(str(line) for line in logs)
            Path(filename).write_text(text + ("\n" if text else ""), encoding="utf-8")
        except OSError as exc:
            message = f"Could not save logs: {exc}"
        else:
            message = f"Logs saved to {filename}"
        self._logs_saved.emit(message)

    def _save_debug_bundle(self, filename: str, result: dict) -> None:
        try:
            Path(filename).write_text(
                json.dumps(result, indent=2, default=str), encoding="utf-8"
            )
        except OSError as exc:
            message = f"Could not save debug bundle: {exc}"
        else:
            message = f"Debug bundle saved to {filename}"
        self._bundle_saved.emit(message)
