"""Diagnostics page: system info, capabilities, self-test, debug bundle.

Renders severity-aware results and remediation.  Export through a save
dialog in the user process.  Loads logs on demand with bounded lines.
"""

from __future__ import annotations

import json
from pathlib import Path

from PySide6.QtWidgets import (
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from honor_control.core.models import SystemSnapshot
from honor_control.frontend.gui.pages import PageBase
from honor_control.frontend.gui.widgets import Card, InfoRow, StatusDot


class DiagnosticsPage(PageBase):
    """System info, capabilities, self-test, logs and debug export."""

    title = "Diagnostics"
    icon = "preferences-system-devices"

    def _build(self) -> None:
        info = Card("System information")
        self.info_rows: dict[str, InfoRow] = {}
        for key in ("model", "cpu_model", "service"):
            row = InfoRow(self._label(key))
            info.layout.addWidget(row)
            self.info_rows[key] = row
        self.add_widget(info)

        caps = Card("Capabilities")
        self.cap_dots: dict[str, StatusDot] = {}
        grid = QVBoxLayout()
        for key in ("battery", "power", "fan", "gestures", "gpu"):
            h = QHBoxLayout()
            lbl = QLabel(key)
            dot = StatusDot(StatusDot.GREY)
            self.cap_dots[key] = dot
            h.addWidget(lbl)
            h.addStretch(1)
            h.addWidget(dot)
            row = QWidget()
            row.setLayout(h)
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
        self.run_btn.clicked.connect(lambda: self.intent.emit("run_checks", ()))
        self.logs_btn.clicked.connect(
            lambda: self.intent.emit("get_recent_logs", (100,))
        )
        self.export_btn.clicked.connect(
            lambda: self.intent.emit("get_debug_bundle", ())
        )
        btn_row.addWidget(self.run_btn)
        btn_row.addWidget(self.export_btn)
        btn_row.addWidget(self.logs_btn)
        btn_row.addStretch(1)
        actions.layout.addLayout(btn_row)
        self.results_label = QLabel("No checks run yet.")
        self.results_label.setWordWrap(True)
        actions.layout.addWidget(self.results_label)
        self.register_control(self.run_btn)
        self.register_control(self.export_btn)
        self.register_control(self.logs_btn)
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

    @staticmethod
    def _label(key: str) -> str:
        return {"model": "Model", "cpu_model": "CPU", "service": "Service"}.get(
            key, key
        )

    def _on_snapshot(self, snap: SystemSnapshot) -> None:
        self.info_rows["model"].set_value(snap.platform.model or "—")
        self.info_rows["cpu_model"].set_value(snap.platform.cpu_model or "—")
        self.info_rows["service"].set_value(
            f"{snap.service.overall} · uptime {snap.service.uptime}s"
        )
        for key, dot in self.cap_dots.items():
            cap = snap.capabilities.get(key)
            if cap is None:
                dot.set_color(StatusDot.GREY)
                dot.setToolTip("Capability has not been probed")
            elif cap.status == "supported":
                dot.set_color(StatusDot.GREEN)
            elif cap.status == "unavailable":
                dot.set_color(StatusDot.AMBER)
            else:
                dot.set_color(StatusDot.RED)
            if cap is not None:
                dot.setToolTip(cap.message or cap.reason_code)

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
        elif operation_id.startswith("get_debug_bundle:") and isinstance(result, dict):
            filename, _filter = QFileDialog.getSaveFileName(
                self, "Save debug bundle", "honor-control-debug.json", "JSON (*.json)"
            )
            if filename:
                try:
                    Path(filename).write_text(
                        json.dumps(result, indent=2, default=str), encoding="utf-8"
                    )
                except OSError as exc:
                    self.results_label.setText(f"Could not save debug bundle: {exc}")
                else:
                    self.results_label.setText(f"Debug bundle saved to {filename}")
