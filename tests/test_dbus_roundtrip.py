"""H-1: end-to-end D-Bus round-trip over a real (private) bus.

Exercises the full wire path that the unit tests only cover in pieces:
client encode -> sdbus marshalling -> ``ControlInterface`` -> application
-> codec -> sdbus marshalling -> client decode.  A variant/signature drift
between ``api.py`` and ``proxy.py`` (e.g. a nested ``a{sv}`` or a dropped
``None``) fails here even though every piece passes in isolation.

sdbus registers each interface name once per process, so the server
(``ControlInterface``) and the client (``ControlProxy``) cannot coexist in
one interpreter.  We therefore run the real service entry point in a
subprocess on a private ``dbus-daemon`` — the same topology as production —
and drive it from ``SdbusClient`` in the test process.  A locally built
``ApplicationService`` (which does not import the D-Bus layer) provides the
expected snapshot for the hardware-derived domains.

Skips cleanly when ``dbus-daemon`` is unavailable.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import sys

import pytest

from honor_control.backend.application import ApplicationService
from honor_control.backend.command_queue import HardwareCommandQueue
from honor_control.backend.config_store import ConfigStore
from honor_control.backend.hardware import FakeHardware
from honor_control.backend.snapshot_store import SnapshotStore
from honor_control.backend.supervisor import RuntimeSupervisor
from honor_control.client.errors import ClientError
from honor_control.client.sdbus_client import SdbusClient
from honor_control.contract import API_VERSION, SCHEMA_VERSION
from honor_control.core.errors import TransportError
from honor_control.core.models import OperationStatus, SystemSnapshot


@pytest.fixture()
def private_bus_address(monkeypatch: pytest.MonkeyPatch):
    """Spawn a private session bus and point the environment at it."""
    if shutil.which("dbus-daemon") is None:
        pytest.skip("dbus-daemon not available")
    proc = subprocess.Popen(
        ["dbus-daemon", "--session", "--print-address=1", "--nofork"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert proc.stdout is not None
    address = proc.stdout.readline().strip()
    if not address or proc.poll() is not None:
        proc.kill()
        pytest.skip("could not start a private D-Bus daemon")
    monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", address)
    yield address
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


async def _connect_with_retry(attempts: int = 100, delay: float = 0.1) -> SdbusClient:
    """Connect to the service, retrying while it acquires the bus name.

    A generous per-call timeout keeps the round trip stable when the service
    subprocess is contended for CPU (e.g. under the full test suite).
    """
    last: Exception | None = None
    for _ in range(attempts):
        client = SdbusClient(bus_kind="session", timeout=30.0)
        try:
            await client.connect()
            return client
        except ClientError as exc:
            last = exc
            await asyncio.sleep(delay)
    raise AssertionError(f"service did not become ready: {last}")


def _make_local_app(tmp_path) -> ApplicationService:
    """Build an in-process service (no D-Bus layer) for expected values."""
    return ApplicationService(
        hardware=FakeHardware(),
        config_store=ConfigStore(state_path=str(tmp_path / "local-state.toml")),
        snapshot_store=SnapshotStore(),
        command_queue=HardwareCommandQueue(),
        supervisor=RuntimeSupervisor(),
    )


class TestDbusRoundTrip:
    def test_snapshot_mutation_and_signal_round_trip(
        self, tmp_path, private_bus_address
    ) -> None:
        # The service runs in a subprocess (sdbus allows only one class per
        # interface name per process). Under heavy CI CPU load the subprocess
        # can be starved long enough for a method call to time out, so retry
        # the whole round trip on a transient transport timeout. A logic error
        # fails the assertions deterministically on every attempt and is never
        # masked by this retry.
        last_exc: Exception | None = None
        for attempt in range(3):
            # A clean state dir per attempt so a partially completed prior
            # attempt (e.g. a mutation that persisted before its reply timed
            # out) cannot pollute the next attempt's snapshot comparison.
            attempt_dir = tmp_path / f"attempt-{attempt}"
            attempt_dir.mkdir()
            try:
                self._run_once(attempt_dir, private_bus_address)
                return
            except ClientError as exc:
                if exc.code not in (
                    TransportError.TIMEOUT,
                    TransportError.SERVICE_UNAVAILABLE,
                ):
                    raise
                last_exc = exc
        raise AssertionError(f"D-Bus round trip stayed unavailable: {last_exc}")

    def _run_once(self, tmp_path, private_bus_address) -> None:
        state_path = tmp_path / "state.toml"
        service_log = tmp_path / "service.log"
        env = dict(os.environ, DBUS_SESSION_BUS_ADDRESS=private_bus_address)
        with open(service_log, "wb") as log_file:
            service = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "honor_control.backend.service",
                    "--session-bus",
                    "--state-path",
                    str(state_path),
                ],
                env=env,
                stdout=log_file,
                stderr=log_file,
            )
            try:
                asyncio.run(self._scenario(tmp_path))
            finally:
                service.terminate()
                try:
                    service.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    service.kill()
                    service.wait()

    async def _scenario(self, tmp_path) -> None:
        # Expected snapshot from an in-process service (FakeHardware is
        # deterministic, so the hardware domains match the subprocess).
        local_app = _make_local_app(tmp_path)
        await local_app.initialize()
        expected = await local_app.get_snapshot()

        client = await _connect_with_retry()
        try:
            assert client.connected

            # -- Snapshot round trip: every hardware domain must survive the
            # wire (nested a{sv}: capabilities map, power profiles, gesture
            # entries) byte-for-byte against the in-process expectation. --
            wire = await client.get_snapshot()
            assert isinstance(wire, SystemSnapshot)
            assert wire.api_version == API_VERSION
            assert wire.schema_version == SCHEMA_VERSION
            assert wire.sequence >= 1
            assert wire.platform == expected.platform
            assert wire.capabilities == expected.capabilities
            assert wire.battery == expected.battery
            assert wire.power == expected.power
            assert wire.fan == expected.fan
            assert wire.gestures == expected.gestures
            assert wire.gpu == expected.gpu

            # -- Mutation round trip: the OperationResult a{sv} decodes. --
            result = await client.set_thresholds(80, 75)
            assert result.status == OperationStatus.SUCCESS
            assert result.applied is True
            assert result.persisted is True

            # The mutation is observable on a subsequent read.
            after = await client.get_snapshot()
            assert after.battery.desired_end == 80
            assert after.battery.desired_start == 75

            # -- Signal round trip: a StateChanged reaches the subscriber and
            # decodes to a valid snapshot. --
            received = asyncio.Event()
            delivered: list[SystemSnapshot] = []

            def on_state(snapshot: SystemSnapshot) -> None:
                delivered.append(snapshot)
                received.set()

            client.on_state_changed(on_state)
            await client.set_thresholds(85, 80)
            await asyncio.wait_for(received.wait(), timeout=5)
            assert delivered, "no StateChanged delivery received"
            assert delivered[0].sequence >= wire.sequence
        finally:
            await client.close()
            await local_app.shutdown()
