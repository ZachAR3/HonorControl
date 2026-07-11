"""Serialized, timeout-aware hardware command worker.

Hardware and firmware calls are not cancellable in Python. A dedicated daemon
thread ensures a wedged ACPI call cannot prevent process shutdown, while the
async lock and poison-after-timeout rule prevent later calls from overlapping
the still-running operation.
"""

from __future__ import annotations

import asyncio
import logging
import queue
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Generic, TypeVar

from honor_control.core.errors import DomainError, DomainException

log = logging.getLogger("honor_control.backend.command_queue")

T = TypeVar("T")
DEFAULT_TIMEOUT = 10.0


class CommandTimeoutError(DomainException):
    """Raised when a hardware command exceeds its deadline."""

    def __init__(self, name: str, timeout: float) -> None:
        super().__init__(
            DomainError.TIMEOUT,
            f"Hardware command '{name}' timed out after {timeout}s",
        )


@dataclass(frozen=True)
class _WorkItem(Generic[T]):
    loop: asyncio.AbstractEventLoop
    future: asyncio.Future[T]
    func: Callable[..., T]
    args: tuple[Any, ...]


_STOP = object()


class HardwareCommandQueue:
    """Run all synchronous hardware calls serially on one daemon thread."""

    def __init__(self, max_workers: int = 1) -> None:
        if max_workers != 1:
            raise ValueError("HardwareCommandQueue supports exactly one worker")
        self._lock = asyncio.Lock()
        self._requests: queue.Queue[_WorkItem[Any] | object] = queue.Queue()
        self._running: dict[str, float] = {}
        self._timed_out_future: asyncio.Future[Any] | None = None
        self._closed = False
        self._thread = threading.Thread(
            target=self._worker,
            name="honor-hw",
            daemon=True,
        )
        self._thread.start()

    async def run(
        self,
        name: str,
        func: Callable[..., T],
        *args: Any,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> T:
        """Execute one call or fail while a previously timed-out call runs."""
        if timeout <= 0:
            raise ValueError("Hardware command timeout must be positive")
        async with self._lock:
            if self._closed:
                raise DomainException(
                    DomainError.UNAVAILABLE,
                    "Hardware command queue is shut down",
                )
            if self._timed_out_future is not None:
                if not self._timed_out_future.done():
                    raise DomainException(
                        DomainError.BUSY,
                        "A timed-out hardware command is still running",
                    )
                self._timed_out_future = None

            correlation_id = str(uuid.uuid4())[:8]
            start = time.monotonic()
            self._running[name] = start
            loop = asyncio.get_running_loop()
            future: asyncio.Future[T] = loop.create_future()
            self._requests.put_nowait(_WorkItem(loop, future, func, args))
            log.debug(
                "hw-queue: start %s (corr=%s, timeout=%ss)",
                name,
                correlation_id,
                timeout,
            )
            try:
                result = await asyncio.wait_for(asyncio.shield(future), timeout)
                log.debug(
                    "hw-queue: done %s in %.3fs",
                    name,
                    time.monotonic() - start,
                )
                return result
            except TimeoutError:
                self._timed_out_future = future
                raise CommandTimeoutError(name, timeout) from None
            except asyncio.CancelledError:
                if not future.done():
                    self._timed_out_future = future
                raise
            except Exception:
                log.exception("hw-queue: %s failed", name)
                raise
            finally:
                self._running.pop(name, None)

    def _worker(self) -> None:
        while True:
            item = self._requests.get()
            if item is _STOP:
                return
            assert isinstance(item, _WorkItem)
            try:
                result = item.func(*item.args)
            except BaseException as exc:  # propagate hardware failures verbatim
                self._schedule_completion(item, exception=exc)
            else:
                self._schedule_completion(item, result=result)

    @staticmethod
    def _schedule_completion(
        item: _WorkItem[Any],
        *,
        result: Any = None,
        exception: BaseException | None = None,
    ) -> None:
        def complete() -> None:
            if item.future.done():
                return
            if exception is not None:
                item.future.set_exception(exception)
            else:
                item.future.set_result(result)

        try:
            item.loop.call_soon_threadsafe(complete)
        except RuntimeError:
            # The owning loop was already closed after cancellation/shutdown.
            pass

    def shutdown(self, wait: bool = True, timeout: float = 1.0) -> None:
        """Reject new work and stop the worker when its current call returns."""
        if self._closed:
            return
        self._closed = True
        self._requests.put_nowait(_STOP)
        if wait:
            self._thread.join(timeout=max(0.0, timeout))
            if self._thread.is_alive():
                log.warning("hardware worker did not stop within %.1fs", timeout)
