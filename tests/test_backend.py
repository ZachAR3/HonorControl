"""Tests for the snapshot store, command queue, and supervisor (WP-04)."""

from __future__ import annotations

import asyncio

import pytest

from honor_control.backend.command_queue import (
    CommandTimeoutError,
    HardwareCommandQueue,
)
from honor_control.backend.snapshot_store import SnapshotStore
from honor_control.backend.supervisor import RuntimeSupervisor
from honor_control.core.errors import ControllerState, DomainError, DomainException
from honor_control.core.models import BatterySnapshot, SystemSnapshot


class TestSnapshotStore:
    """Verify sequence, change-only notifications, and stale marking."""

    def test_initial_sequence_is_zero(self) -> None:
        store = SnapshotStore()
        assert store.sequence == 0

    def test_update_increments_sequence_on_change(self) -> None:
        store = SnapshotStore()
        asyncio.run(
            store.update(
                "battery", BatterySnapshot(available=True, capacity_percent=50)
            )
        )
        assert store.sequence == 1

    def test_update_does_not_increment_on_no_change(self) -> None:
        store = SnapshotStore()
        bat = BatterySnapshot(available=True, capacity_percent=50)
        asyncio.run(store.update("battery", bat))
        seq_before = store.sequence
        asyncio.run(store.update("battery", bat))
        assert store.sequence == seq_before

    def test_subscriber_notified_on_change(self) -> None:
        store = SnapshotStore()
        received: list[tuple[SystemSnapshot, tuple[str, ...]]] = []
        store.subscribe(lambda snap, domains: received.append((snap, domains)))
        asyncio.run(store.update("battery", BatterySnapshot(available=True)))
        assert len(received) == 1
        assert received[0][1] == ("battery",)

    def test_mark_stale_adds_domain(self) -> None:
        store = SnapshotStore()
        asyncio.run(store.mark_stale("battery", "read failed"))
        assert "battery" in store.snapshot.stale_domains
        assert "read failed" in store.snapshot.errors

    def test_mark_stale_clears_on_successful_update(self) -> None:
        store = SnapshotStore()
        value = BatterySnapshot(available=True)
        asyncio.run(store.update("battery", value))
        asyncio.run(store.mark_stale("battery", "read failed"))
        asyncio.run(store.update("battery", value))
        assert "battery" not in store.snapshot.stale_domains


class TestCommandQueue:
    """Verify serialization, timeout, and cancellation."""

    def test_run_executes_function(self) -> None:
        q = HardwareCommandQueue()
        result = asyncio.run(q.run("test", lambda: 42))
        assert result == 42
        q.shutdown()

    def test_run_passes_args(self) -> None:
        q = HardwareCommandQueue()
        result = asyncio.run(q.run("test", lambda a, b: a + b, 3, 4))
        assert result == 7
        q.shutdown()

    def test_run_timeout_raises(self) -> None:
        q = HardwareCommandQueue()

        def slow() -> int:
            import time

            time.sleep(2)
            return 42

        with pytest.raises(CommandTimeoutError):
            asyncio.run(q.run("slow", slow, timeout=0.1))
        q.shutdown()

    def test_run_propagates_exception(self) -> None:
        q = HardwareCommandQueue()

        def boom() -> int:
            raise ValueError("kaboom")

        with pytest.raises(ValueError):
            asyncio.run(q.run("boom", boom))
        q.shutdown()

    def test_timeout_blocks_new_work_until_call_returns(self) -> None:
        q = HardwareCommandQueue()

        async def scenario() -> None:
            import time

            with pytest.raises(CommandTimeoutError):
                await q.run("slow", lambda: time.sleep(0.15), timeout=0.01)
            with pytest.raises(DomainException) as exc_info:
                await q.run("must-not-overlap", lambda: 1)
            assert exc_info.value.code == DomainError.BUSY
            await asyncio.sleep(0.2)
            assert await q.run("recovered", lambda: 2) == 2

        asyncio.run(scenario())
        q.shutdown()


class TestSupervisor:
    """Verify controller lifecycle and health."""

    def test_duplicate_registration_is_rejected(self) -> None:
        sup = RuntimeSupervisor()

        async def start_func() -> None:
            pass

        sup.register("test", start_func)
        with pytest.raises(ValueError, match="already registered"):
            sup.register("test", start_func)

    def test_start_stop_idempotent(self) -> None:
        sup = RuntimeSupervisor()
        started = False
        stopped = False

        async def start_func() -> None:
            nonlocal started
            started = True
            try:
                await asyncio.sleep(100)
            except asyncio.CancelledError:
                pass

        async def stop_func() -> None:
            nonlocal stopped
            stopped = True

        async def scenario() -> None:
            sup.register("test", start_func, stop_func)
            await sup.start("test")
            await asyncio.sleep(0)
            assert sup.get_health("test").running is True
            await sup.start("test")
            await sup.stop("test")
            assert sup.get_health("test").state == ControllerState.STOPPED

        asyncio.run(scenario())
        assert started is True
        assert stopped is True

    def test_stop_when_not_started_is_noop(self) -> None:
        sup = RuntimeSupervisor()

        async def start_func() -> None:
            pass

        sup.register("test", start_func)
        asyncio.run(sup.stop("test"))
        assert sup.get_health("test").state == ControllerState.STOPPED

    def test_stop_all_stops_everything(self) -> None:
        sup = RuntimeSupervisor()
        started: list[str] = []

        async def make_start(name: str):
            async def start_func() -> None:
                started.append(name)
                try:
                    await asyncio.sleep(100)
                except asyncio.CancelledError:
                    pass

            return start_func

        async def setup():
            sup.register("a", await make_start("a"))
            sup.register("b", await make_start("b"))
            await sup.start("a")
            await sup.start("b")

        async def run():
            await setup()
            await sup.stop_all()

        asyncio.run(run())
        assert sup.get_health("a").state == ControllerState.STOPPED
        assert sup.get_health("b").state == ControllerState.STOPPED

    def test_check_health_returns_state_strings(self) -> None:
        sup = RuntimeSupervisor()

        async def start_func() -> None:
            pass

        sup.register("test", start_func)
        health = sup.check_health()
        assert health["test"] == "stopped"
