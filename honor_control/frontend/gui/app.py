"""QApplication entry point for the Honor Control GUI.

Creates a :class:`QApplication`, applies KDE-friendly metadata, builds the
:class:`MainWindow`, and runs the Qt event loop.
"""

from __future__ import annotations

import argparse
import sys
import time

from PySide6.QtCore import Qt
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication

from honor_control import __version__
from honor_control.frontend.gui.design import apply_design_system
from honor_control.frontend.single_instance import (
    FRONTEND_LOCK_NAME,
    acquire_single_instance,
    clear_frontend_handoff,
    request_frontend_handoff,
)


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="honor-control-gui",
        description="Honor Control — graphical control center for Honor laptops.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"honor-control-gui {__version__}",
    )
    parser.add_argument(
        "--bus",
        choices=["system", "session"],
        default="system",
        help="D-Bus to use (default: system)",
    )
    return parser.parse_args(argv)


def _apply_metadata(app: QApplication) -> None:
    app.setApplicationName("Honor Control")
    app.setApplicationDisplayName("Honor Control")
    app.setOrganizationName("HonorLinux")
    app.setApplicationVersion(__version__)
    app.setDesktopFileName("org.honorlinux.Control")


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )
    app = QApplication.instance() or QApplication([sys.argv[0]])
    assert isinstance(app, QApplication)
    if not QIcon.themeName():
        QIcon.setThemeName("breeze")
    _apply_metadata(app)
    apply_design_system(app)
    app.setQuitOnLastWindowClosed(False)
    # HC-017: one per-user frontend session shared with the tray.  If the tray
    # currently owns it, ask it to step aside and wait briefly for the lock so
    # the GUI takes over instead of running a second icon/client.
    request_frontend_handoff()
    instance_lock = None
    deadline = time.monotonic() + 3.0
    while instance_lock is None and time.monotonic() < deadline:
        instance_lock = acquire_single_instance(FRONTEND_LOCK_NAME)
        if instance_lock is None:
            time.sleep(0.1)
    clear_frontend_handoff()
    if instance_lock is None:
        print(
            "honor-control-gui: another frontend is running and did not step aside",
            file=sys.stderr,
        )
        return 0
    app._honor_instance_lock = instance_lock  # type: ignore[attr-defined]

    from honor_control.frontend.gui.main_window import MainWindow

    window = MainWindow(bus_kind=args.bus)
    app.aboutToQuit.connect(window.shutdown)
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
