"""Snapshot store: monotonic sequence, immutable snapshots, subscriptions.

Services publish typed snapshots here.  D-Bus reads return cached state
quickly.  Monitors update it; frontends subscribe to one state-change
signal and use low-frequency polling only as recovery.

``update(domain, replacement)`` compares values, increments a 64-bit
sequence only on change, stamps UTC time, and notifies subscribers once
per transaction.  On refresh failure, last-known-good data is retained
and the domain is marked stale.
"""

from __future__ import annotations

import asyncio
import contextvars
import inspect
import logging
from collections import deque
from collections.abc import Callable
from dataclasses import replace
from typing import Any

from honor_control.core.models import SystemSnapshot, utc_now

log = logging.getLogger("honor_control.backend.snapshot_store")

# The owner marker lets an async subscriber publish a nested update without
# waiting for its own notification to finish.  Nested events are appended to
# the same FIFO and delivered after the current callback returns.
_NOTIFICATION_OWNER: contextvars.ContextVar[object | None] = contextvars.ContextVar(
    "snapshot_notification_owner", default=None
)


class SnapshotStore:
    """Event-loop-safe immutable snapshot store with change-only notifications.

    A single :class:`SystemSnapshot` is the source of truth.  Each
    ``update`` call replaces one domain field, increments the sequence
    only when the value actually changed, and notifies subscribers.
    """

    def __init__(self) -> None:
        self._snapshot = SystemSnapshot()
        self._lock = asyncio.Lock()
        self._subscribers: list[Callable[[SystemSnapshot, tuple[str, ...]], Any]] = []
        self._notification_queue: deque[
            tuple[SystemSnapshot, str, asyncio.Future[None]]
        ] = deque()
        self._notifying = False

    @property
    def snapshot(self) -> SystemSnapshot:
        """Return the current immutable snapshot (no lock needed for reads)."""
        return self._snapshot

    @property
    def sequence(self) -> int:
        """Return the current sequence number."""
        return self._snapshot.sequence

    def subscribe(
        self, callback: Callable[[SystemSnapshot, tuple[str, ...]], Any]
    ) -> Callable[[], None]:
        """Register a subscriber.  Returns an unsubscribe function."""
        self._subscribers.append(callback)

        def _unsubscribe() -> None:
            try:
                self._subscribers.remove(callback)
            except ValueError:
                pass

        return _unsubscribe

    async def update(self, domain: str, value: Any) -> SystemSnapshot:
        """Replace one domain field and notify subscribers if it changed.

        ``domain`` is one of: ``service``, ``platform``, ``capabilities``,
        ``battery``, ``power``, ``fan``, ``gestures``, ``gpu``.
        """
        async with self._lock:
            current = getattr(self._snapshot, domain)
            was_stale = domain in self._snapshot.stale_domains
            if current == value and not was_stale:
                return self._snapshot
            new_seq = self._snapshot.sequence + 1
            new_stale = tuple(
                item for item in self._snapshot.stale_domains if item != domain
            )
            self._snapshot = replace(
                self._snapshot,
                **{domain: value},
                sequence=new_seq,
                observed_at=utc_now(),
                stale_domains=new_stale,
            )
            snapshot = self._snapshot
        # Subscriber callbacks run outside the state lock.  A callback may
        # legitimately request another update (for example, to acknowledge a
        # signal); awaiting it while holding ``_lock`` would deadlock that
        # update forever.
        await self._notify(snapshot, domain)
        return snapshot

    async def mark_stale(self, domain: str, error: str) -> SystemSnapshot:
        """Mark a domain stale (last-known-good retained) with an error."""
        async with self._lock:
            if domain in self._snapshot.stale_domains:
                return self._snapshot
            new_stale = self._snapshot.stale_domains + (domain,)
            new_errors = self._snapshot.errors
            if error and error not in new_errors:
                # Keep a bounded error list (last 10).
                new_errors = (new_errors + (error,))[-10:]
            self._snapshot = replace(
                self._snapshot,
                stale_domains=new_stale,
                errors=new_errors,
                sequence=self._snapshot.sequence + 1,
                observed_at=utc_now(),
            )
            snapshot = self._snapshot
        await self._notify(snapshot, domain)
        return snapshot

    async def _notify(self, snapshot: SystemSnapshot, domain: str) -> None:
        """Notify subscribers in sequence without holding the state lock.

        Updates made by a subscriber are queued behind the current event and
        return immediately from the nested call, avoiding a reentrant wait on
        the notification currently being delivered.  Updates from unrelated
        tasks wait for their own event, keeping callback order deterministic.
        """
        loop = asyncio.get_running_loop()
        delivered = loop.create_future()
        self._notification_queue.append((snapshot, domain, delivered))
        if self._notifying:
            if _NOTIFICATION_OWNER.get() is self:
                # The outer drain will deliver this event after the callback
                # that enqueued it completes.
                return
            await delivered
            return

        self._notifying = True
        owner_token = _NOTIFICATION_OWNER.set(self)
        try:
            while self._notification_queue:
                current, current_domain, current_delivered = (
                    self._notification_queue.popleft()
                )
                try:
                    # Take a snapshot so an unsubscribe performed by a
                    # callback does not skip the next subscriber.
                    for callback in tuple(self._subscribers):
                        try:
                            result = callback(current, (current_domain,))
                            if inspect.isawaitable(result):
                                await result
                        except Exception:  # noqa: BLE001
                            log.exception("subscriber notification failed")
                except asyncio.CancelledError:
                    if not current_delivered.done():
                        current_delivered.cancel()
                    raise
                else:
                    if not current_delivered.done():
                        current_delivered.set_result(None)
        finally:
            _NOTIFICATION_OWNER.reset(owner_token)
            self._notifying = False
            # If the draining task was cancelled while a callback was
            # running, do not strand unrelated callers waiting on events that
            # can no longer be delivered.  The next state change starts a
            # fresh notification drain.
            while self._notification_queue:
                _queued_snapshot, _queued_domain, queued = (
                    self._notification_queue.popleft()
                )
                if not queued.done():
                    queued.cancel()
