"""Main window: sidebar navigation + content stack.

Owns the :class:`GuiController` (worker thread) and :class:`GuiState`.
Pages receive only the state and emit user intents — they never touch
the client or perform D-Bus calls.  A periodic refresh timer triggers
snapshot fetches on the worker thread.
"""

from __future__ import annotations

import logging

from PySide6.QtCore import QSettings, QSize, QTimer
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QStackedWidget,
    QSystemTrayIcon,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from honor_control import __version__
from honor_control.frontend.gui.controller import GuiController
from honor_control.frontend.gui.design import apply_design_system
from honor_control.frontend.gui.pages import PageBase
from honor_control.frontend.gui.pages.battery import BatteryPage
from honor_control.frontend.gui.pages.dashboard import DashboardPage
from honor_control.frontend.gui.pages.diagnostics import DiagnosticsPage
from honor_control.frontend.gui.pages.fan import FanPage
from honor_control.frontend.gui.pages.power import PowerPage
from honor_control.frontend.gui.pages.settings import SettingsPage
from honor_control.frontend.gui.pages.touchpad import TouchpadPage
from honor_control.frontend.gui.state import GuiState
from honor_control.frontend.gui.widgets import StatusDot
from honor_control.frontend.tray.tray import HonorTray

log = logging.getLogger("honor_control.frontend.gui.main_window")

#: Sidebar entries: (page class, legacy icon name, display label).
PAGES: list[tuple[type[PageBase], str, str]] = [
    (DashboardPage, "go-home", "Overview"),
    (BatteryPage, "battery-good", "Battery"),
    (PowerPage, "preferences-system-power", "Power"),
    (TouchpadPage, "input-touchpad", "Touchpad"),
    (FanPage, "preferences-system-performance", "Fan"),
    (DiagnosticsPage, "preferences-system-devices", "Diagnostics"),
    (SettingsPage, "preferences-system", "Settings"),
]

#: Refresh interval (ms) for the periodic snapshot poll.
REFRESH_INTERVAL_MS = 5000
OFFLINE_MESSAGE = (
    "Honor Control service is not running. "
    "Start it with: sudo systemctl start honor-control.service"
)


class MainWindow(QMainWindow):
    """Top-level window tying together the controller, state and pages."""

    def __init__(self, bus_kind: str = "system") -> None:
        super().__init__()
        self.controller = GuiController(bus_kind=bus_kind)
        self.state = GuiState()
        self._pages: list[PageBase] = []
        self._settings = QSettings()
        self._close_to_tray = self._settings.value(
            "window/close_to_tray", True, type=bool
        )
        self._quitting = False
        self._shutdown = False
        self._tray_notice_shown = False

        self.setWindowTitle("Honor Control")
        self.setObjectName("HonorWindow")
        app = QApplication.instance()
        if isinstance(app, QApplication) and not app.property("honorTheme"):
            apply_design_system(app)
        self.resize(980, 640)
        self.setMinimumSize(700, 500)
        geometry = self._settings.value("window/geometry")
        if geometry is not None:
            self.restoreGeometry(geometry)

        self._build_ui()
        self._build_menu()
        self._connect_signals()

        # Start the worker thread.
        self.controller.start()
        self.tray = HonorTray(
            bus_kind=bus_kind,
            controller=self.controller,
            open_window=self.restore_from_tray,
            quit_application=self.quit_application,
        )

        settings_page = next(
            (page for page in self._pages if isinstance(page, SettingsPage)), None
        )
        if settings_page is not None:
            settings_page.set_close_to_tray(self._close_to_tray)

        self._timer = QTimer(self)
        self._timer.setInterval(REFRESH_INTERVAL_MS)
        self._timer.timeout.connect(self.controller.refresh)
        self._timer.start()

        self.statusBar().setSizeGripEnabled(False)
        self.footer_identity = QLabel(f"Honor Control {__version__}")
        self.footer_identity.setProperty("muted", True)
        self.statusBar().addPermanentWidget(self.footer_identity)
        self.statusBar().showMessage("Connecting…", 2500)

    # UI construction

    def _build_ui(self) -> None:
        central = QWidget(self)
        central.setObjectName("HonorWindow")
        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        self.setCentralWidget(central)
        root.addWidget(self._build_header())
        self.banner = self._build_banner()
        root.addWidget(self.banner)

        body = QWidget()
        body_layout = QHBoxLayout(body)
        body_layout.setContentsMargins(0, 0, 0, 0)
        body_layout.setSpacing(0)
        self.sidebar_frame = self._build_sidebar()
        body_layout.addWidget(self.sidebar_frame)

        self.stack = QStackedWidget()
        self.stack.setObjectName("HonorContent")
        for page_cls, _icon, _label in PAGES:
            page = page_cls(self.state)
            page.intent.connect(self._on_intent)
            self._pages.append(page)
            self.stack.addWidget(page)
        body_layout.addWidget(self.stack, 1)
        root.addWidget(body, 1)
        self.sidebar.setCurrentRow(0)

    def _build_sidebar(self) -> QWidget:
        bar = QFrame()
        bar.setObjectName("HonorSidebar")
        bar.setFixedWidth(176)
        lay = QVBoxLayout(bar)
        lay.setContentsMargins(0, 14, 0, 12)
        lay.setSpacing(0)
        self.sidebar = QListWidget()
        self.sidebar.setObjectName("HonorSidebarList")
        self.sidebar.setUniformItemSizes(True)
        self.sidebar.setFrameShape(QFrame.Shape.NoFrame)
        self.sidebar.currentRowChanged.connect(self._on_sidebar_changed)
        lay.addWidget(self.sidebar, 1)
        for _page_cls, _icon_name, label in PAGES:
            item = QListWidgetItem(label)
            item.setSizeHint(QSize(0, 36))
            self.sidebar.addItem(item)
        return bar

    def _build_header(self) -> QWidget:
        header = QFrame()
        header.setObjectName("HonorHeader")
        header.setFixedHeight(52)
        h = QHBoxLayout(header)
        h.setContentsMargins(20, 0, 16, 0)
        h.setSpacing(12)
        brand = QLabel("Honor Control")
        brand.setObjectName("HonorBrand")
        h.addWidget(brand)
        separator = QFrame()
        separator.setFrameShape(QFrame.Shape.VLine)
        separator.setFixedHeight(20)
        h.addWidget(separator)
        self.device_label = QLabel("Detecting device…")
        self.device_label.setObjectName("HonorDevice")
        h.addWidget(self.device_label)
        h.addStretch(1)
        self.service_label = QLabel("Service: …")
        h.addWidget(self.service_label)
        self.health_dot = StatusDot(StatusDot.GREY)
        h.addWidget(self.health_dot)
        refresh = QToolButton()
        refresh.setText("Refresh")
        refresh.setProperty("quiet", True)
        refresh.setToolTip("Refresh now (Ctrl+R)")
        refresh.setAccessibleName("Refresh hardware state")
        refresh.clicked.connect(self.controller.refresh)
        h.addWidget(refresh)
        return header

    def _build_banner(self) -> QFrame:
        banner = QFrame()
        banner.setObjectName("HonorBanner")
        banner.setMinimumHeight(36)
        bh = QHBoxLayout(banner)
        bh.setContentsMargins(16, 4, 16, 4)
        self.banner_label = QLabel(OFFLINE_MESSAGE)
        self.banner_label.setWordWrap(True)
        bh.addWidget(self.banner_label)
        bh.addStretch(1)
        banner.setVisible(False)
        return banner

    def _build_menu(self) -> None:
        quit_action = QAction("&Quit", self)
        quit_action.setShortcut(QKeySequence.StandardKey.Quit)
        quit_action.triggered.connect(self.quit_application)
        self.addAction(quit_action)
        refresh_action = QAction("&Refresh", self)
        refresh_action.setShortcut(QKeySequence.StandardKey.Refresh)
        refresh_action.triggered.connect(self.controller.refresh)
        self.addAction(refresh_action)
        about_action = QAction("&About Honor Control", self)
        about_action.triggered.connect(self._about)
        self.addAction(about_action)

    def _connect_signals(self) -> None:
        self.controller.snapshot_received.connect(self.state.set_snapshot)
        self.controller.connection_changed.connect(self._on_connection)
        self.controller.protocol_mismatch.connect(self._on_protocol_mismatch)
        self.controller.error.connect(self.state.emit_error)
        self.controller.operation_started.connect(self.state.mark_pending)
        self.controller.operation_completed.connect(self.state.emit_completed)
        self.controller.operation_completed.connect(self._on_operation_completed)
        self.state.connection_changed.connect(self._update_availability)
        self.state.snapshot_changed.connect(self._update_identity)
        self.state.stale_changed.connect(self._on_stale_changed)
        self.state.error_occurred.connect(
            lambda msg: self.statusBar().showMessage(f"Error: {msg}", 4000)
        )

    # Navigation

    def _on_sidebar_changed(self, row: int) -> None:
        if 0 <= row < len(self._pages):
            self.stack.setCurrentIndex(row)
            self._pages[row].refresh()

    # Availability

    def _on_connection(self, connected: bool) -> None:
        self.state.set_connected(connected)
        if connected:
            self.statusBar().showMessage("Connected", 2000)

    def _on_operation_completed(self, _operation_id: str, result: object) -> None:
        self.controller.refresh()
        if result is None:
            return
        message = str(getattr(result, "message", ""))
        if message:
            self.statusBar().showMessage(message, 4000)

    def _on_intent(self, method: str, args: object) -> None:
        values = tuple(args) if isinstance(args, (tuple, list)) else ()
        if method == "navigate" and values:
            target = str(values[0])
            row = next(
                (index for index, page in enumerate(PAGES) if page[2] == target),
                -1,
            )
            if row >= 0:
                self.sidebar.setCurrentRow(row)
            return
        if method == "quit":
            self.quit_application()
            return
        if method == "set_close_to_tray":
            self._close_to_tray = bool(values[0]) if values else True
            self._settings.setValue("window/close_to_tray", self._close_to_tray)
            self.statusBar().showMessage(
                "Close button will hide to tray"
                if self._close_to_tray
                else "Close button will quit Honor Control",
                3000,
            )
            return
        operation_id = f"{method}:{':'.join(str(x) for x in values)}"
        self.controller.call(operation_id, method, *values)

    def _update_availability(self, available: bool) -> None:
        if available:
            self.banner_label.setText(OFFLINE_MESSAGE)
        self.banner.setVisible(not available)
        color = StatusDot.GREEN if available else StatusDot.RED
        self.health_dot.set_color(color)
        self.service_label.setText(
            "Service: online" if available else "Service: offline"
        )
        for page in self._pages:
            page.set_backend_available(available)

    def _on_protocol_mismatch(self, message: str) -> None:
        self.banner_label.setText(f"Incompatible service version: {message}")
        self.banner.setVisible(True)

    def _update_identity(self, snapshot: object) -> None:
        platform = getattr(snapshot, "platform", None)
        model = str(getattr(platform, "model", "") or "HONOR laptop")
        self.device_label.setText(model)

    def _on_stale_changed(self, domains: tuple[str, ...]) -> None:
        if domains:
            self.statusBar().showMessage(
                "Stale data: " + ", ".join(domains),
                5000,
            )

    # Misc

    def _about(self) -> None:
        QMessageBox.about(
            self,
            "About Honor Control",
            f"<h3>Honor Control</h3>"
            f"<p>Version {__version__}</p>"
            f"<p>D-Bus service and Qt6 GUI for managing "
            f"Honor MagicBook laptops on Linux.</p>"
            f"<p>Licensed under the GPL-3.0-or-later.</p>",
        )

    def restore_from_tray(self) -> None:
        """Show, raise, and focus the existing control-center window."""
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def resizeEvent(self, event) -> None:  # noqa: N802
        """Keep the compact shell useful at the supported narrow width."""
        compact = event.size().width() < 820
        self.sidebar_frame.setFixedWidth(136 if compact else 176)
        self.device_label.setVisible(not compact)
        super().resizeEvent(event)

    def quit_application(self) -> None:
        """Exit the GUI process explicitly from File or the tray menu."""
        self._quitting = True
        self._shutdown_application()
        app = QApplication.instance()
        if app is not None:
            app.quit()

    def shutdown(self) -> None:
        """Release the tray and worker during desktop-session shutdown."""
        self._shutdown_application()

    def _shutdown_application(self) -> None:
        if self._shutdown:
            return
        self._shutdown = True
        self._settings.setValue("window/geometry", self.saveGeometry())
        self._timer.stop()
        self.tray.shutdown()
        self.controller.stop()

    def closeEvent(self, event) -> None:  # noqa: N802
        self._settings.setValue("window/geometry", self.saveGeometry())
        if self._close_to_tray and not self._quitting and self.tray.available:
            event.ignore()
            self.hide()
            if not self._tray_notice_shown:
                self.tray.tray.showMessage(
                    "Honor Control is still running",
                    "Click the tray icon to reopen it; choose Quit from the tray "
                    "menu to exit.",
                    QSystemTrayIcon.MessageIcon.Information,
                    3000,
                )
                self._tray_notice_shown = True
            return
        self._shutdown_application()
        event.accept()
        app = QApplication.instance()
        if app is not None:
            app.quit()
