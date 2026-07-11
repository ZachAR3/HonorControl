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
import logging
from collections.abc import Callable
from dataclasses import replace
from typing import Any

from honor_control.core.models import SystemSnapshot, utc_now

log = logging.getLogger("honor_control.backend.snapshot_store")


class SnapshotStore:
    """Thread-safe immutable snapshot store with change-only notifications.

    A single :class:`SystemSnapshot` is the source of truth.  Each
    ``update`` call replaces one domain field, increments the sequence
    only when the value actually changed, and notifies subscribers.
    """

    def __init__(self) -> None:
        self._snapshot = SystemSnapshot()
        self._lock = asyncio.Lock()
        self._subscribers: list[Callable[[SystemSnapshot, tuple[str, ...]], Any]] = []

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
            await self._notify(domain)
            return self._snapshot

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
            await self._notify(domain)
            return self._snapshot

    async def _notify(self, domain: str) -> None:
        """Notify all subscribers of a change in ``domain``."""
        for callback in self._subscribers:
            try:
                result = callback(self._snapshot, (domain,))
                if asyncio.iscoroutine(result):
                    await result
            except Exception:  # noqa: BLE001
                log.exception("subscriber notification failed")
