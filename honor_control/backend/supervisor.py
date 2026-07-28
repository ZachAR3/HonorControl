"""Runtime supervisor: controller lifecycle and health.

Owns all task creation/cancellation on the event-loop thread.
``start()``/``stop()`` are awaited, idempotent state transitions:
``stopped → starting → running → stopping/failed``.

Task exceptions are captured into controller health. Explicit ``stop()``
requests run registered safety cleanup with a bounded timeout; controllers
whose abnormal exit requires immediate restoration own that exception path.

Fault states: a stop whose task ignores cancellation ends in
``stop_timeout`` (never a clean ``stopped``), and the still-alive task stays
tracked so ``start()`` refuses to spawn a duplicate until it completes. A
registered cleanup that raises ends in ``cleanup_failed``. Controllers may
opt into a bounded exponential-backoff restart policy for unexpected
crashes (see :meth:`RuntimeSupervisor.register`).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass
from typing import Any

from honor_control.core.errors import ControllerState

log = logging.getLogger("honor_control.backend.supervisor")

#: Upper bound for a single restart backoff delay.
MAX_RESTART_DELAY_SECONDS = 30.0


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
      * Explicit stop runs registered safety cleanup with a bounded timeout.
      * A stop timeout or cleanup failure is reported as a fault state,
        never as a clean ``stopped``.
      * Controllers with a restart policy are restarted with bounded
        exponential backoff after unexpected crashes.
    """

    def __init__(self) -> None:
        self._controllers: dict[str, _ControllerEntry] = {}
        self._health: dict[str, ControllerHealth] = {}
        self._restart_counts: dict[str, int] = {}
        self._pending_restarts: set[asyncio.Task[None]] = set()

    def register(
        self,
        name: str,
        start_func: Callable[[], Coroutine[Any, Any, None]],
        stop_func: Callable[[], Awaitable[None]] | None = None,
        *,
        restart: bool = False,
        max_restarts: int = 3,
        backoff_seconds: float = 1.0,
    ) -> None:
        """Register a controller by name.

        ``restart`` opts the controller into a bounded restart policy:
        unexpected task crashes (not cancellation, not an explicit stop)
        are restarted up to ``max_restarts`` times with exponential backoff
        starting at ``backoff_seconds`` (each delay capped at
        ``MAX_RESTART_DELAY_SECONDS``).  When the budget is exhausted the
        controller stays ``failed``.  Without the policy, a crash ends in
        ``failed`` immediately (the historical behavior).
        """
        if name in self._controllers:
            raise ValueError(f"controller '{name}' is already registered")
        self._controllers[name] = _ControllerEntry(
            start_func=start_func,
            stop_func=stop_func,
            restart=restart,
            max_restarts=max_restarts,
            backoff_seconds=backoff_seconds,
        )
        self._health[name] = ControllerHealth(name=name)

    @property
    def health(self) -> dict[str, ControllerHealth]:
        """Return a snapshot of all controller health records."""
        return dict(self._health)

    def get_health(self, name: str) -> ControllerHealth:
        """Return the health record for a controller."""
        return self._health.get(name, ControllerHealth(name=name))

    def restart_counts(self) -> dict[str, int]:
        """Return restarts attempted per registered controller."""
        return {name: self._restart_counts.get(name, 0) for name in self._controllers}

    async def start(self, name: str) -> bool:
        """Start a controller.  Idempotent: returns True if now running.

        Refuses (returns False) when a previous task for the controller is
        known to still be alive — e.g. it ignored cancellation during a stop
        timeout — rather than spawning a duplicate.  A fresh start is only
        allowed once the prior task has completed (or was never started).
        """
        entry = self._controllers.get(name)
        if entry is None:
            log.warning("supervisor: unknown controller '%s'", name)
            return False
        health = self._health[name]
        if health.running or health.state == ControllerState.STARTING:
            return True
        prior = entry.task
        if prior is not None and not prior.done():
            log.warning(
                "supervisor: '%s' previous task is still alive; not starting",
                name,
            )
            return False
        return await self._spawn(name)

    async def _spawn(self, name: str) -> bool:
        """Create the controller task and mark it running."""
        entry = self._controllers[name]
        health = self._health[name]
        health.state = ControllerState.STARTING
        try:
            task = asyncio.create_task(entry.start_func(), name=f"ctrl-{name}")
        except Exception as exc:  # noqa: BLE001
            health.state = ControllerState.FAILED
            health.last_fault = str(exc)
            log.error("supervisor: failed to start '%s': %s", name, exc)
            return False
        entry.task = task
        task.add_done_callback(lambda done, n=name: self._task_done(n, done))
        health.state = ControllerState.RUNNING
        health.last_fault = ""
        log.info("supervisor: started '%s'", name)
        return True

    def _task_done(self, name: str, task: asyncio.Task) -> None:
        entry = self._controllers.get(name)
        if entry is not None and entry.task is task:
            # The tracked task finished: a later start() may proceed again.
            entry.task = None
        health = self._health.get(name)
        if (
            health is None
            or task.cancelled()
            or health.state
            in (
                ControllerState.STOPPING,
                # Fault states stay visible; a late completion of a stuck or
                # failed-cleanup controller must not masquerade as healthy.
                ControllerState.STOP_TIMEOUT,
                ControllerState.CLEANUP_FAILED,
            )
        ):
            return
        try:
            fault = task.exception()
        except asyncio.CancelledError:
            return
        if fault is not None:
            health.state = ControllerState.FAILED
            health.last_fault = str(fault)
            if entry is not None and entry.restart:
                count = self._restart_counts.get(name, 0)
                if count < entry.max_restarts:
                    self._restart_counts[name] = count + 1
                    log.warning(
                        "supervisor: '%s' crashed (%s); restart %d/%d",
                        name,
                        fault,
                        count + 1,
                        entry.max_restarts,
                    )
                    self._schedule_restart(name, count + 1)
                    return
            log.error("supervisor: '%s' stopped: %s", name, health.last_fault)
        elif health.state == ControllerState.RUNNING:
            health.state = ControllerState.STOPPED

    def _schedule_restart(self, name: str, attempt: int) -> None:
        """Schedule a crash restart with bounded exponential backoff."""
        entry = self._controllers.get(name)
        if entry is None:
            return
        delay = min(
            entry.backoff_seconds * (2 ** (attempt - 1)),
            MAX_RESTART_DELAY_SECONDS,
        )
        loop = asyncio.get_running_loop()
        task = loop.create_task(
            self._restart_after(name, delay),
            name=f"ctrl-{name}-restart",
        )
        self._pending_restarts.add(task)
        task.add_done_callback(self._pending_restarts.discard)

    async def _restart_after(self, name: str, delay: float) -> None:
        await asyncio.sleep(delay)
        health = self._health.get(name)
        if health is None or name not in self._controllers:
            return
        if health.state != ControllerState.FAILED:
            # Stopped or restarted by other means while the backoff was pending.
            return
        log.info("supervisor: restarting '%s'", name)
        await self._spawn(name)

    async def stop(self, name: str, timeout: float = 5.0) -> None:
        """Stop a controller and run safety cleanup.  Idempotent.

        A task that does not finish within ``timeout`` leaves the controller
        in ``STOP_TIMEOUT`` (not ``STOPPED``) and stays tracked, so a later
        ``start()`` refuses to duplicate it until the task completes.  A
        cleanup/stop function that raises leaves the controller in
        ``CLEANUP_FAILED``.  Only a fully clean stop reports ``STOPPED``.
        """
        entry = self._controllers.get(name)
        if entry is None:
            return
        health = self._health[name]
        if health.state in (ControllerState.STOPPED, ControllerState.STOPPING):
            return
        health.state = ControllerState.STOPPING
        task = entry.task
        entry.task = None
        stop_timed_out = False
        if task is not None and not task.done():
            task.cancel()
            _, pending = await asyncio.wait({task}, timeout=timeout)
            if pending:
                # The task ignored cancellation and is still alive.  Keep
                # tracking it (its done-callback clears the reference) so
                # start() knows a duplicate must not be spawned.
                entry.task = task
                stop_timed_out = True
                health.last_fault = f"did not stop within {timeout:.1f}s"
                log.warning("supervisor: '%s' did not stop in %.1fs", name, timeout)
            elif not task.cancelled():
                fault = task.exception()
                if fault is not None:
                    log.error("supervisor: '%s' raised: %s", name, fault)
                    health.last_fault = str(fault)
        # Run safety cleanup.
        cleanup_failed = False
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
                cleanup_failed = True
        if stop_timed_out:
            health.state = ControllerState.STOP_TIMEOUT
        elif cleanup_failed:
            health.state = ControllerState.CLEANUP_FAILED
        else:
            health.state = ControllerState.STOPPED
            log.info("supervisor: stopped '%s'", name)

    async def stop_all(self, timeout: float = 5.0) -> None:
        """Stop all controllers in reverse registration order."""
        # Cancel pending crash-restart backoffs first so teardown is
        # deterministic and a backoff cannot respawn a controller mid-shutdown.
        for task in list(self._pending_restarts):
            task.cancel()
        self._pending_restarts.clear()
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
    restart: bool = False
    max_restarts: int = 3
    backoff_seconds: float = 1.0
