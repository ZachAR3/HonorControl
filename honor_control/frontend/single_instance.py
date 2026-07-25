"""Small process locks for the standalone Qt frontends."""

from __future__ import annotations

from PySide6.QtCore import QLockFile, QStandardPaths


def acquire_single_instance(name: str) -> QLockFile | None:
    """Return a held per-user lock, or ``None`` when another instance owns it."""
    runtime_dir = QStandardPaths.writableLocation(
        QStandardPaths.StandardLocation.RuntimeLocation
    )
    if not runtime_dir:
        runtime_dir = QStandardPaths.writableLocation(
            QStandardPaths.StandardLocation.TempLocation
        )
    lock = QLockFile(f"{runtime_dir}/honor-control-{name}.lock")
    lock.setStaleLockTime(0)
    return lock if lock.tryLock(0) else None
