"""GUI controller: worker loop, commands, refresh/reconnect.

Owns an asyncio loop/client in a dedicated thread.  Creates and uses the
sdbus connection on that worker context only.  Exposes Qt signals for
connection state, snapshot received, operation started/completed, and
fatal protocol mismatch.  Pages never receive the client object.

Safety invariants:
  * Qt's main thread is free of D-Bus, filesystem, subprocess, and
    hardware I/O.
  * Transport offline preserves the last snapshot and marks it stale.
  * Authorization/validation failure leaves connection online.
  * Commands carry an operation ID/domain; duplicate submissions per
    control are prevented while pending.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import Any

from PySide6.QtCore import QObject, QThread, Signal

from honor_control.client.errors import ClientError
from honor_control.client.sdbus_client import SdbusClient
from honor_control.core.errors import TransportError
from honor_control.core.models import SystemSnapshot

log = logging.getLogger("honor_control.frontend.gui.controller")

# Profile saves/applies include PPD settling, sysfs readback, and serialized
# hardware work.  Keep the frontend deadline above the backend's normal
# operation time so a completed operation is not reported as a GUI timeout.
GUI_DBUS_TIMEOUT_SECONDS = 15.0


class GuiWorker(QThread):
    """Background worker thread running the asyncio loop and client.

    All D-Bus calls happen on this thread.  Results are emitted as Qt
    signals so the main thread never blocks on I/O.
    """

    snapshot_ready = Signal(object)  # SystemSnapshot
    connection_changed = Signal(bool)  # connected
    error = Signal(str)  # error message
    operation_result = Signal(str, object)  # operation_id, OperationResult

    def __init__(
        self,
        bus_kind: str = "system",
        timeout: float = GUI_DBUS_TIMEOUT_SECONDS,
    ) -> None:
        super().__init__()
        self._bus_kind = bus_kind
        self._timeout = timeout
        self._loop: asyncio.AbstractEventLoop | None = None
        self._client: SdbusClient | None = None
        self._stop_event: asyncio.Event | None = None
        self._tasks: set[asyncio.Task[Any]] = set()
        self._stop_requested = threading.Event()
        self._main_task: asyncio.Task[None] | None = None

    @property
    def client(self) -> SdbusClient | None:
        return self._client

    def run(self) -> None:
        """Run the asyncio loop and process client calls."""
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._main_task = self._loop.create_task(self._main())
            self._loop.run_until_complete(self._main_task)
        except asyncio.CancelledError:
            pass
        except Exception as exc:  # noqa: BLE001
            log.error("worker crashed: %s", exc)
            self.error.emit(str(exc))
        finally:
            self._loop.close()

    async def _main(self) -> None:
        """Connect, refresh, and retry with bounded backoff until stopped."""
        self._stop_event = asyncio.Event()
        if self._stop_requested.is_set():
            self._stop_event.set()
        backoff = 1.0
        while not self._stop_event.is_set():
            self._client = SdbusClient(bus_kind=self._bus_kind, timeout=self._timeout)
            try:
                await self._client.connect()
                client = self._client
                self._client.on_state_changed(
                    lambda snap, owner=client: self._emit_snapshot(owner, snap)
                )
                self.connection_changed.emit(True)
                self.snapshot_ready.emit(await self._client.get_snapshot())
                backoff = 1.0
                while not self._stop_event.is_set():
                    try:
                        await asyncio.wait_for(self._stop_event.wait(), timeout=15)
                    except TimeoutError:
                        if not self._client.connected:
                            raise ClientError(
                                TransportError.SERVICE_UNAVAILABLE,
                                "Service signal connection was lost",
                            )
            except ClientError as exc:
                self.connection_changed.emit(False)
                self.error.emit(exc.message)
            finally:
                await self._cancel_tasks()
                await self._client.close()
                self._client = None
            if not self._stop_event.is_set():
                try:
                    await asyncio.wait_for(self._stop_event.wait(), timeout=backoff)
                except TimeoutError:
                    backoff = min(backoff * 2, 15)

    def stop(self) -> None:
        """Signal the worker to stop and wait."""
        self._stop_requested.set()
        if self._stop_event is not None and self._loop is not None:

            def request_stop() -> None:
                assert self._stop_event is not None
                self._stop_event.set()
                if self._main_task is not None:
                    self._main_task.cancel()

            self._loop.call_soon_threadsafe(request_stop)
        if not self.wait(2_000):
            log.warning("GUI worker did not stop within 2 seconds")

    def _emit_snapshot(self, owner: SdbusClient, snap: SystemSnapshot) -> None:
        if owner is self._client and owner.connected:
            self.snapshot_ready.emit(snap)

    def _spawn(self, awaitable: Any) -> None:
        """Track one worker-owned task through completion."""
        task = asyncio.create_task(awaitable)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _cancel_tasks(self) -> None:
        tasks = tuple(self._tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def submit_call(self, operation_id: str, method: str, *args) -> bool:
        """Submit an async call to the worker loop; return False if unavailable."""
        if self._loop is None or self._loop.is_closed() or not self._loop.is_running():
            return False
        self._loop.call_soon_threadsafe(self._run_call, operation_id, method, args)
        return True

    def _run_call(
        self,
        operation_id: str,
        method: str,
        args: tuple,
    ) -> None:
        """Run an async call and emit the result (called on the worker loop)."""
        if self._loop is None:
            return

        async def _execute() -> None:
            try:
                client = self._client
                if client is None or not client.connected:
                    raise ClientError(
                        code=TransportError.SERVICE_UNAVAILABLE,
                        message="Service is offline",
                    )
                result = await getattr(client, method)(*args)
                self.operation_result.emit(operation_id, result)
            except asyncio.CancelledError:
                self.operation_result.emit(operation_id, None)
                raise
            except ClientError as exc:
                self.error.emit(exc.message)
                self.operation_result.emit(operation_id, None)
            except Exception as exc:  # noqa: BLE001
                log.error("operation %s failed: %s", operation_id, exc)
                self.error.emit(str(exc))
                self.operation_result.emit(operation_id, None)

        self._spawn(_execute())

    def request_snapshot(self) -> None:
        """Request a fresh snapshot from the service."""
        if self._loop is None or self._loop.is_closed() or not self._loop.is_running():
            return

        self._loop.call_soon_threadsafe(self._fetch_and_emit)

    def _fetch_and_emit(self) -> Any:
        """Fetch a snapshot and emit it (runs on worker loop)."""
        client = self._client
        if client is None:
            return None

        async def _do() -> None:
            try:
                snap = await client.get_snapshot()
                if client is self._client and client.connected:
                    self.snapshot_ready.emit(snap)
            except ClientError as exc:
                if client is self._client:
                    self.connection_changed.emit(False)
                    self.error.emit(exc.message)

        self._spawn(_do())
        return None


class GuiController(QObject):
    """Qt-facing controller that owns the worker thread.

    Pages connect to its signals and call its methods.  No page ever
    receives the client object.
    """

    snapshot_received = Signal(object)  # SystemSnapshot
    connection_changed = Signal(bool)  # connected
    operation_started = Signal(str)  # operation_id
    operation_completed = Signal(str, object)  # operation_id, OperationResult
    error = Signal(str)  # error message

    def __init__(
        self,
        bus_kind: str = "system",
        timeout: float = GUI_DBUS_TIMEOUT_SECONDS,
    ) -> None:
        super().__init__()
        self._worker = GuiWorker(bus_kind=bus_kind, timeout=timeout)
        self._worker.snapshot_ready.connect(self._on_snapshot)
        self._worker.connection_changed.connect(self._on_connection)
        self._worker.error.connect(self._on_error)
        self._worker.operation_result.connect(self._on_operation_result)
        self._pending_ops: set[str] = set()
        self._last_snapshot: SystemSnapshot | None = None
        self._connected = False

    @property
    def connected(self) -> bool:
        """Return whether the worker is connected to the service."""
        return self._connected

    @property
    def last_snapshot(self) -> SystemSnapshot | None:
        """Return the last received snapshot (or None)."""
        return self._last_snapshot

    def start(self) -> None:
        """Start the worker thread."""
        self._worker.start()

    def stop(self) -> None:
        """Stop the worker thread (bounded wait)."""
        self._worker.stop()

    def refresh(self) -> None:
        """Request a fresh snapshot from the service."""
        self._worker.request_snapshot()

    def call(self, operation_id: str, method: str, *args) -> bool:
        """Submit an operation.  Returns False if a duplicate is pending."""
        if operation_id in self._pending_ops:
            return False
        if not self._worker.submit_call(operation_id, method, *args):
            self.error.emit("Service worker is not ready")
            return False
        self._pending_ops.add(operation_id)
        self.operation_started.emit(operation_id)
        return True

    # -- Signal handlers (run on Qt main thread) --

    def _on_snapshot(self, snap: SystemSnapshot) -> None:
        self._last_snapshot = snap
        self.snapshot_received.emit(snap)

    def _on_connection(self, connected: bool) -> None:
        self._connected = connected
        self.connection_changed.emit(connected)

    def _on_error(self, message: str) -> None:
        self.error.emit(message)

    def _on_operation_result(self, operation_id: str, result: object) -> None:
        self._pending_ops.discard(operation_id)
        self.operation_completed.emit(operation_id, result)
