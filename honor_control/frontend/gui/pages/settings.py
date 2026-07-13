"""Settings page: app-level prefs + backend config actions.

Keeps only real user preferences and service metadata already in state.
Machine config import/reload is in a clearly privileged maintenance
section.  No synchronous version/path calls.
"""

from __future__ import annotations

from PySide6.QtWidgets import QHBoxLayout, QLabel, QWidget

from honor_control import __version__
from honor_control.core.models import SystemSnapshot
from honor_control.frontend.gui.pages import PageBase
from honor_control.frontend.gui.widgets import Card, InfoRow, ToggleSwitch


class SettingsPage(PageBase):
    """Application settings and backend config actions."""

    title = "Settings"
    icon = "preferences-system"

    def _build(self) -> None:
        app_card = Card("Application")
        self.version_row = InfoRow("GUI version", __version__)
        app_card.layout.addWidget(self.version_row)
        self.api_version_row = InfoRow("API version", "—")
        app_card.layout.addWidget(self.api_version_row)
        close_host = QWidget()
        close_layout = QHBoxLayout(close_host)
        close_layout.setContentsMargins(0, 6, 0, 0)
        close_copy = QLabel(
            "<b>Keep running in the tray</b><br>"
            "<span style='color: rgba(128, 128, 128, 210);'>"
            "Closing the window hides it; Quit exits the application.</span>"
        )
        close_layout.addWidget(close_copy, 1)
        self.close_to_tray = ToggleSwitch(True)
        self.close_to_tray.setToolTip("Hide the window instead of exiting")
        self.close_to_tray.toggled.connect(
            lambda enabled: self.intent.emit("set_close_to_tray", (enabled,))
        )
        close_layout.addWidget(self.close_to_tray)
        app_card.layout.addWidget(close_host)
        self.add_widget(app_card)

        backend = Card("Backend")
        self.health_row = InfoRow("Service health", "—")
        self.uptime_row = InfoRow("Uptime", "—")
        backend.layout.addWidget(self.health_row)
        backend.layout.addWidget(self.uptime_row)
        self.add_widget(backend)

        about = Card("About")
        about_text = QLabel(
            f"<p><b>Honor Control</b> v{__version__}</p>"
            "<p>A D-Bus service and Qt6 GUI for managing Honor MagicBook "
            "laptops on Linux.</p>"
            "<p>Licensed under the LGPL-3.0-or-later.</p>"
        )
        about_text.setWordWrap(True)
        about.layout.addWidget(about_text)
        self.add_widget(about)

        self.add_stretch()
        self.state.snapshot_changed.connect(self._on_snapshot)

    def _on_snapshot(self, snap: SystemSnapshot) -> None:
        self.api_version_row.set_value(f"v{snap.api_version}")
        self.health_row.set_value(snap.service.overall)
        self.uptime_row.set_value(f"{snap.service.uptime}s")

    def refresh(self) -> None:
        snap = self.state.snapshot
        if snap is not None:
            self._on_snapshot(snap)

    def set_close_to_tray(self, enabled: bool) -> None:
        """Update the local close behavior without emitting an intent."""
        self.close_to_tray.setChecked(enabled)
