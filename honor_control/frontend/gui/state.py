"""Typed Qt-facing state store for the GUI.

Holds the last received :class:`SystemSnapshot`, connection status, stale
metadata, and pending/error results by operation/domain.  Emits only
changed domains so pages don't repaint unnecessarily.
"""

from __future__ import annotations

from PySide6.QtCore import QObject, Signal

from honor_control.core.models import SystemSnapshot


class GuiState(QObject):
    """Observable typed state for the GUI.

    The controller updates this after each successful fetch or operation.
    Pages connect to the domain-specific signals they care about.
    """

    # Domain-specific change signals.
    snapshot_changed = Signal(object)  # SystemSnapshot
    connection_changed = Signal(bool)  # connected
    stale_changed = Signal(tuple)  # stale_domains
    operation_pending = Signal(str)  # operation_id
    operation_completed = Signal(str, object)  # operation_id, OperationResult
    error_occurred = Signal(str)  # message

    def __init__(self) -> None:
        super().__init__()
        self._snapshot: SystemSnapshot | None = None
        self._connected: bool = False
        self._pending_ops: set[str] = set()

    @property
    def snapshot(self) -> SystemSnapshot | None:
        """Return the last received snapshot (or None)."""
        return self._snapshot

    @property
    def connected(self) -> bool:
        """Return whether the service is connected."""
        return self._connected

    @property
    def stale_domains(self) -> tuple[str, ...]:
        """Return domains whose last live read failed."""
        if self._snapshot is None:
            return ()
        return self._snapshot.stale_domains

    def set_snapshot(self, snap: SystemSnapshot) -> None:
        """Update the snapshot and emit change signals."""
        old_seq = self._snapshot.sequence if self._snapshot else -1
        self._snapshot = snap
        if snap.sequence != old_seq:
            self.snapshot_changed.emit(snap)
            self.stale_changed.emit(snap.stale_domains)

    def set_connected(self, connected: bool) -> None:
        """Update connection status and emit if changed."""
        if connected != self._connected:
            self._connected = connected
            self.connection_changed.emit(connected)

    def mark_pending(self, operation_id: str) -> None:
        """Mark an operation as pending."""
        self._pending_ops.add(operation_id)
        self.operation_pending.emit(operation_id)

    def clear_pending(self, operation_id: str) -> None:
        """Mark an operation as completed."""
        self._pending_ops.discard(operation_id)

    def emit_completed(self, operation_id: str, result: object) -> None:
        """Emit the completion signal for an operation."""
        self.clear_pending(operation_id)
        self.operation_completed.emit(operation_id, result)

    def emit_error(self, message: str) -> None:
        """Emit an error message."""
        self.error_occurred.emit(message)
