"""Tests for the snapshot store, command queue, and supervisor (WP-04)."""

from __future__ import annotations

import asyncio
import threading

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

    def test_timeout_recovery_is_the_next_command_and_extends_poison(self) -> None:
        q = HardwareCommandQueue()

        async def scenario() -> None:
            import time

            calls: list[str] = []

            def slow() -> None:
                time.sleep(0.08)
                calls.append("slow")

            def recover() -> bool:
                calls.append("recover")
                return True

            with pytest.raises(CommandTimeoutError) as exc_info:
                await q.run(
                    "slow",
                    slow,
                    timeout=0.01,
                    timeout_recovery=("recover", recover, ()),
                )
            recovery = exc_info.value.recovery_future
            assert recovery is not None
            with pytest.raises(DomainException) as busy:
                await q.run("must-wait-for-recovery", lambda: calls.append("unsafe"))
            assert busy.value.code == DomainError.BUSY

            assert await asyncio.wait_for(asyncio.shield(recovery), timeout=1) is True
            assert await q.run("after-recovery", lambda: calls.append("after")) is None
            assert calls == ["slow", "recover", "after"]

        asyncio.run(scenario())
        q.shutdown()

    def test_cancelled_call_blocks_overlap_until_worker_finishes(self) -> None:
        q = HardwareCommandQueue()

        async def scenario() -> None:
            import time

            task = asyncio.create_task(q.run("slow", lambda: time.sleep(0.08)))
            await asyncio.sleep(0.01)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

            with pytest.raises(DomainException) as busy:
                await q.run("must-not-overlap", lambda: None)
            assert busy.value.code == DomainError.BUSY

            await asyncio.sleep(0.1)
            assert await q.run("recovered", lambda: 2) == 2

        asyncio.run(scenario())
        q.shutdown()

    def test_shutdown_rejects_new_work(self) -> None:
        q = HardwareCommandQueue()
        q.shutdown()

        with pytest.raises(DomainException) as unavailable:
            asyncio.run(q.run("after-shutdown", lambda: None))
        assert unavailable.value.code == DomainError.UNAVAILABLE

    def test_stuck_command_is_observable_until_release(self) -> None:
        q = HardwareCommandQueue()
        release = threading.Event()

        def blocked() -> str:
            release.wait()
            return "late"

        async def scenario() -> None:
            with pytest.raises(CommandTimeoutError):
                await q.run("stuck-call", blocked, timeout=0.05)
            await asyncio.sleep(0.005)
            info = q.stuck_info()
            assert info is not None
            assert info.name == "stuck-call"
            assert info.timed_out_at >= info.started_at
            assert q.poisoned_duration() > 0
            with pytest.raises(DomainException) as busy:
                await q.run("while-stuck", lambda: 1)
            assert busy.value.code == DomainError.BUSY
            # Release the wedged call; the poison must clear on completion.
            release.set()
            loop = asyncio.get_running_loop()
            deadline = loop.time() + 2
            while q.stuck_info() is not None:
                assert loop.time() < deadline, "poison never cleared"
                await asyncio.sleep(0.01)
            assert q.poisoned_duration() == 0.0
            assert await q.run("after-stuck", lambda: 7) == 7

        asyncio.run(scenario())
        q.shutdown()

    def test_successful_command_leaves_no_stuck_record(self) -> None:
        q = HardwareCommandQueue()
        assert q.stuck_info() is None
        assert q.poisoned_duration() == 0.0
        assert asyncio.run(q.run("ok", lambda: 1)) == 1
        assert q.stuck_info() is None
        assert q.poisoned_duration() == 0.0
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

    def test_stop_timeout_is_a_fault_and_blocks_duplicate_start(self) -> None:
        sup = RuntimeSupervisor()
        give_up = False
        cancellations = 0

        async def stubborn() -> None:
            nonlocal cancellations
            while not give_up:
                try:
                    await asyncio.sleep(0.02)
                except asyncio.CancelledError:
                    cancellations += 1

        async def scenario() -> None:
            nonlocal give_up
            sup.register("stubborn", stubborn)
            assert await sup.start("stubborn") is True
            await asyncio.sleep(0)
            await sup.stop("stubborn", timeout=0.05)
            assert sup.get_health("stubborn").state == ControllerState.STOP_TIMEOUT
            assert cancellations >= 1
            # A duplicate start must be refused while the task is alive.
            assert await sup.start("stubborn") is False
            assert sup.get_health("stubborn").state == ControllerState.STOP_TIMEOUT
            # Let the wedged task exit; then a fresh start is allowed again
            # and a clean stop reports STOPPED.
            give_up = True
            loop = asyncio.get_running_loop()
            deadline = loop.time() + 2
            while not await sup.start("stubborn"):
                assert loop.time() < deadline, "stuck task never released"
                await asyncio.sleep(0.01)
            await sup.stop("stubborn", timeout=1)
            assert sup.get_health("stubborn").state == ControllerState.STOPPED

        asyncio.run(scenario())

    def test_cleanup_failure_is_not_a_clean_stop(self) -> None:
        sup = RuntimeSupervisor()

        async def start_func() -> None:
            try:
                await asyncio.sleep(100)
            except asyncio.CancelledError:
                pass

        async def bad_cleanup() -> None:
            raise RuntimeError("cleanup exploded")

        async def scenario() -> None:
            sup.register("bad", start_func, bad_cleanup)
            await sup.start("bad")
            await asyncio.sleep(0)
            await sup.stop("bad", timeout=1)
            assert sup.get_health("bad").state == ControllerState.CLEANUP_FAILED
            assert "cleanup exploded" in sup.get_health("bad").last_fault

        asyncio.run(scenario())

    def test_restart_policy_is_bounded(self) -> None:
        sup = RuntimeSupervisor()
        attempts = 0

        async def always_crashes() -> None:
            nonlocal attempts
            attempts += 1
            raise RuntimeError("boom")

        async def scenario() -> None:
            sup.register(
                "flaky",
                always_crashes,
                restart=True,
                max_restarts=2,
                backoff_seconds=0.01,
            )
            assert await sup.start("flaky") is True
            loop = asyncio.get_running_loop()
            deadline = loop.time() + 2
            while attempts < 3 or (
                sup.get_health("flaky").state != ControllerState.FAILED
            ):
                assert loop.time() < deadline, (
                    f"restart budget not exhausted (attempts={attempts}, "
                    f"state={sup.get_health('flaky').state})"
                )
                await asyncio.sleep(0.01)

        asyncio.run(scenario())
        assert attempts == 3  # initial run + 2 restarts
        assert sup.restart_counts()["flaky"] == 2
        assert sup.get_health("flaky").state == ControllerState.FAILED

    def test_no_restart_policy_stays_failed(self) -> None:
        sup = RuntimeSupervisor()
        attempts = 0

        async def crashes_once() -> None:
            nonlocal attempts
            attempts += 1
            raise RuntimeError("dead")

        async def scenario() -> None:
            sup.register("plain", crashes_once)
            assert await sup.start("plain") is True
            loop = asyncio.get_running_loop()
            deadline = loop.time() + 2
            while sup.get_health("plain").state != ControllerState.FAILED:
                assert loop.time() < deadline, "crash not recorded"
                await asyncio.sleep(0.01)
            await asyncio.sleep(0.05)  # grace period: no restart may happen

        asyncio.run(scenario())
        assert attempts == 1
        assert sup.restart_counts()["plain"] == 0
        assert sup.get_health("plain").state == ControllerState.FAILED
