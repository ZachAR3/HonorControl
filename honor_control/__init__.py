"""honor_control — D-Bus service and Qt6 GUI for Honor MagicBook laptops.

This package provides:
  * a privileged root **backend** D-Bus service (``honor_control.backend``),
  * an unprivileged **frontend** with a PySide6 GUI, tray icon and CLI
    (``honor_control.frontend`` / ``honor_control.cli``).

All hardware mutations go through authorized system D-Bus and one
serialized command queue.  See ``docs/architecture.md`` for details.
"""

from __future__ import annotations

try:
    from importlib.metadata import PackageNotFoundError
    from importlib.metadata import version as _pkg_version

    try:
        __version__ = _pkg_version("honor-control")
    except PackageNotFoundError:
        __version__ = "0.2.0.dev0"
except ImportError:
    __version__ = "0.2.0.dev0"
