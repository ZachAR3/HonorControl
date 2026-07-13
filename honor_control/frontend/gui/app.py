"""QApplication entry point for the Honor Control GUI.

Creates a :class:`QApplication`, applies KDE-friendly metadata, builds the
:class:`MainWindow`, and runs the Qt event loop.
"""

from __future__ import annotations

import argparse
import sys

from PySide6.QtCore import Qt
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication

from honor_control import __version__


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
    app.setQuitOnLastWindowClosed(False)

    from honor_control.frontend.gui.main_window import MainWindow

    window = MainWindow(bus_kind=args.bus)
    app.aboutToQuit.connect(window.shutdown)
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
