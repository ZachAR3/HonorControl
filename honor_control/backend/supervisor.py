"""Runtime supervisor: controller lifecycle and health.

Owns all task creation/cancellation on the event-loop thread.
``start()``/``stop()`` are awaited, idempotent state transitions:
``stopped → starting → running → stopping/failed``.

Task exceptions are captured into controller health.  Safety cleanup
runs in ``finally`` with a bounded timeout.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass
from typing import Any

from honor_control.core.errors import ControllerState

log = logging.getLogger("honor_control.backend.supervisor")


@dataclass
class ControllerHealth:
    """Health record for a single controller."""

    name: str
    state: ControllerState = ControllerState.STOPPED
    last_fault: str = ""

    @property
    def running(self) -> bool:
        return self.state == ControllerState.RUNNING


class RuntimeSupervisor:
    """Manages background controller task lifecycle.

    Each controller is registered with a ``start_func`` (async, returns
    an awaitable that runs until cancelled) and a ``stop_func`` (async,
    performs safety cleanup).  The supervisor ensures:
      * ``start()``/``stop()`` are idempotent state transitions.
      * Task exceptions are captured into controller health.
      * Safety cleanup runs in ``finally`` with a bounded timeout.
    """

    def __init__(self) -> None:
        self._controllers: dict[str, _ControllerEntry] = {}
        self._health: dict[str, ControllerHealth] = {}

    def register(
        self,
        name: str,
        start_func: Callable[[], Coroutine[Any, Any, None]],
        stop_func: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        """Register a controller by name."""
        if name in self._controllers:
            raise ValueError(f"controller '{name}' is already registered")
        self._controllers[name] = _ControllerEntry(
            start_func=start_func, stop_func=stop_func
        )
        self._health[name] = ControllerHealth(name=name)

    @property
    def health(self) -> dict[str, ControllerHealth]:
        """Return a snapshot of all controller health records."""
        return dict(self._health)

    def get_health(self, name: str) -> ControllerHealth:
        """Return the health record for a controller."""
        return self._health.get(name, ControllerHealth(name=name))

    async def start(self, name: str) -> bool:
        """Start a controller.  Idempotent: returns True if now running."""
        entry = self._controllers.get(name)
        if entry is None:
            log.warning("supervisor: unknown controller '%s'", name)
            return False
        health = self._health[name]
        if health.running or health.state == ControllerState.STARTING:
            return True
        health.state = ControllerState.STARTING
        try:
            task = asyncio.create_task(entry.start_func(), name=f"ctrl-{name}")
            entry.task = task
            task.add_done_callback(lambda done, n=name: self._task_done(n, done))
            health.state = ControllerState.RUNNING
            health.last_fault = ""
            log.info("supervisor: started '%s'", name)
            return True
        except Exception as exc:  # noqa: BLE001
            health.state = ControllerState.FAILED
            health.last_fault = str(exc)
            log.error("supervisor: failed to start '%s': %s", name, exc)
            return False

    def _task_done(self, name: str, task: asyncio.Task) -> None:
        health = self._health.get(name)
        if (
            health is None
            or task.cancelled()
            or health.state == ControllerState.STOPPING
        ):
            return
        try:
            fault = task.exception()
        except asyncio.CancelledError:
            return
        if fault is not None:
            health.state = ControllerState.FAILED
            health.last_fault = str(fault)
            log.error("supervisor: '%s' stopped: %s", name, health.last_fault)
        elif health.state == ControllerState.RUNNING:
            health.state = ControllerState.STOPPED

    async def stop(self, name: str, timeout: float = 5.0) -> None:
        """Stop a controller and run safety cleanup.  Idempotent."""
        entry = self._controllers.get(name)
        if entry is None:
            return
        health = self._health[name]
        if health.state in (ControllerState.STOPPED, ControllerState.STOPPING):
            return
        health.state = ControllerState.STOPPING
        task = entry.task
        entry.task = None
        if task is not None and not task.done():
            task.cancel()
            try:
                await asyncio.wait_for(task, timeout=timeout)
            except asyncio.CancelledError:
                pass
            except TimeoutError:
                log.warning("supervisor: '%s' did not stop in %.1fs", name, timeout)
            except Exception as exc:  # noqa: BLE001
                log.error("supervisor: '%s' raised: %s", name, exc)
                health.last_fault = str(exc)
        # Run safety cleanup.
        if entry.stop_func is not None:
            try:
                await asyncio.wait_for(entry.stop_func(), timeout=timeout)
            except asyncio.CancelledError:
                pass
            except TimeoutError:
                log.warning("supervisor: '%s' cleanup timed out", name)
            except Exception as exc:  # noqa: BLE001
                log.error("supervisor: '%s' cleanup failed: %s", name, exc)
                health.last_fault = str(exc)
        health.state = ControllerState.STOPPED
        log.info("supervisor: stopped '%s'", name)

    async def stop_all(self, timeout: float = 5.0) -> None:
        """Stop all controllers in reverse registration order."""
        names = list(self._controllers.keys())
        for name in reversed(names):
            await self.stop(name, timeout=timeout)

    def check_health(self) -> dict[str, str]:
        """Return a dict of controller name -> state string."""
        return {name: str(h.state) for name, h in self._health.items()}


@dataclass
class _ControllerEntry:
    """Internal registration record for a controller."""

    start_func: Callable[[], Coroutine[Any, Any, None]]
    stop_func: Callable[[], Awaitable[None]] | None = None
    task: asyncio.Task | None = None
