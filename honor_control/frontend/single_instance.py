"""Small process locks for the standalone Qt frontends."""

from __future__ import annotations

import os

from PySide6.QtCore import QLockFile, QStandardPaths

#: Single per-user ownership lock shared by the GUI and the tray, so at most
#: one Honor Control frontend session runs at a time (HC-017).  Previously the
#: GUI and tray used separate lock names and could run concurrently, yielding
#: two tray icons and two D-Bus clients.
FRONTEND_LOCK_NAME = "frontend"


def _runtime_dir() -> str:
    runtime_dir = QStandardPaths.writableLocation(
        QStandardPaths.StandardLocation.RuntimeLocation
    )
    if not runtime_dir:
        runtime_dir = QStandardPaths.writableLocation(
            QStandardPaths.StandardLocation.TempLocation
        )
    return runtime_dir


def acquire_single_instance(name: str) -> QLockFile | None:
    """Return a held per-user lock, or ``None`` when another instance owns it."""
    lock = QLockFile(f"{_runtime_dir()}/honor-control-{name}.lock")
    lock.setStaleLockTime(0)
    return lock if lock.tryLock(0) else None


def _handoff_flag_path() -> str:
    return f"{_runtime_dir()}/honor-control-gui-requested"


def request_frontend_handoff() -> None:
    """Ask a running tray to step aside so the GUI can take ownership."""
    try:
        with open(_handoff_flag_path(), "w", encoding="utf-8") as handle:
            handle.write(str(os.getpid()))
    except OSError:
        pass


def clear_frontend_handoff() -> None:
    """Remove the handoff-request flag (idempotent)."""
    try:
        os.remove(_handoff_flag_path())
    except OSError:
        pass


def frontend_handoff_requested() -> bool:
    """Return whether the GUI has asked the current frontend to step aside."""
    return os.path.exists(_handoff_flag_path())
