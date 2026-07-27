"""D-Bus backend service entry point (runs as root under systemd).

Process lifecycle and composition only.  Feature behavior lives in
:class:`ApplicationService`.  The D-Bus layer is a thin, versioned
transport.  All hardware mutations go through the serialized command
queue.

Safety invariants:
  * Unknown hardware cannot perform any write (positive platform match
    required).
  * Polkit failure/missing caller fails closed.
  * No frontend imports backend implementation or writes config/hardware
    directly.
  * Development session-bus mode uses FakeHardware and refuses to start
    with real hardware writes.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
from typing import Any

from honor_control import __version__
from honor_control.backend.application import ApplicationService
from honor_control.backend.command_queue import HardwareCommandQueue
from honor_control.backend.config_store import ConfigStore
from honor_control.backend.dbus.api import build_objects
from honor_control.backend.dbus.authorizer import (
    Authorizer,
    FakeAuthorizer,
    PolkitAuthorizer,
)
from honor_control.backend.gesture_runtime import GestureRuntime
from honor_control.backend.hardware import FakeHardware, HonorToolsAdapter
from honor_control.backend.snapshot_store import SnapshotStore
from honor_control.backend.supervisor import RuntimeSupervisor
from honor_control.contract import BUS_NAME

log = logging.getLogger("honor_control.backend.service")

# Bound the emergency fan-auto restore (run as ExecStopPost and on escalation)
# so a hung EC call cannot wedge shutdown; kept under the unit's TimeoutStopSec.
EMERGENCY_FAN_RESTORE_TIMEOUT_SECONDS = 8


async def _serve(use_session_bus: bool = False, state_path: str | None = None) -> int:
    """Export the D-Bus interface, publish the bus name, and run forever."""
    import sdbus
    from sdbus import request_default_bus_name_async

    # Open the bus.
    if use_session_bus:
        bus = sdbus.sd_bus_open_user()
        log.info("using session bus (dev mode — FakeHardware)")
        hardware = FakeHardware()
        authorizer: Authorizer = FakeAuthorizer()
    else:
        bus = sdbus.sd_bus_open_system()
        log.info("using system bus")
        hardware = HonorToolsAdapter()
        authorizer = PolkitAuthorizer()

    sdbus.set_default_bus(bus)
    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()
    escalated = False

    # Compose the application.
    config = ConfigStore(state_path=state_path) if state_path else ConfigStore()
    gesture_runtime = (
        None
        if use_session_bus
        else GestureRuntime(lambda: config.state.gestures.mappings)
    )
    snapshots = SnapshotStore()
    queue = HardwareCommandQueue()
    supervisor = RuntimeSupervisor()
    app = ApplicationService(
        hardware=hardware,
        config_store=config,
        snapshot_store=snapshots,
        command_queue=queue,
        supervisor=supervisor,
        gesture_runtime=gesture_runtime,
    )

    def _on_stuck_escalation() -> None:
        # HC-002: a permanently wedged hardware call cannot be cancelled.  Stop
        # accepting work and exit nonzero so systemd's Restart=on-failure brings
        # up a fresh process (ExecStopPost restores stock fan auto on the way
        # out) instead of leaving a healthy-looking, indefinitely read-only
        # service.
        nonlocal escalated
        escalated = True
        log.critical("hardware queue wedged; escalating for service restart")
        stop_event.set()

    app.set_stuck_escalation(_on_stuck_escalation)

    def unsubscribe() -> None:
        """No-op until the snapshot signal subscription is installed."""

    signals_installed = False
    try:
        await app.initialize()
        await app.start_background()

        # Export D-Bus objects before publishing the well-known name.
        objects = build_objects(app, authorizer)
        for path, obj in objects.items():
            obj.export_to_dbus(path)
            log.info("exported %s", path)
        root = objects[next(iter(objects))]

        def _emit_state_changed(snapshot, domains) -> None:
            root.StateChanged.emit((snapshot.sequence, list(domains)))

        unsubscribe = app.snapshots.subscribe(_emit_state_changed)
        await request_default_bus_name_async(BUS_NAME)
        log.info("acquired bus name %s", BUS_NAME)
        log.info("honor-control-service v%s ready", __version__)

        def _signal_handler() -> None:
            log.info("received stop signal")
            stop_event.set()

        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, _signal_handler)
        signals_installed = True
        await stop_event.wait()
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        log.info("shutting down")
        if signals_installed:
            for sig in (signal.SIGTERM, signal.SIGINT):
                loop.remove_signal_handler(sig)
        unsubscribe()
        # Stop accepting new D-Bus calls before application cleanup waits for
        # any in-flight serialized mutation to finish.
        bus.close()
        await app.shutdown()
    return 1 if escalated else 0


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="honor-control-service",
        description="Honor Control D-Bus backend service.",
    )
    p.add_argument(
        "--session-bus",
        action="store_true",
        help="use the session bus with FakeHardware (development only)",
    )
    p.add_argument(
        "--state-path",
        default=None,
        help="override the state file path (default: /var/lib/honor-control/state.toml)",
    )
    p.add_argument(
        "--restore-fan-auto",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    p.add_argument("--verbose", "-v", action="store_true", help="debug logging")
    p.add_argument(
        "--version", action="version", version=f"honor-control-service {__version__}"
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    if args.restore_fan_auto:
        # Bound the emergency restore so a hung EC call cannot wedge shutdown.
        def _restore_alarm(signum: int, frame: Any) -> None:
            raise TimeoutError("emergency fan-auto restore timed out")

        signal.signal(signal.SIGALRM, _restore_alarm)
        signal.alarm(EMERGENCY_FAN_RESTORE_TIMEOUT_SECONDS)
        try:
            hardware = HonorToolsAdapter()
            platform = hardware.detect_platform()
            if platform.matched and not hardware.set_fan_auto():
                log.error("emergency fan-auto restore failed")
                return 1
            return 0
        except TimeoutError:
            log.error("emergency fan-auto restore timed out")
            return 1
        finally:
            signal.alarm(0)
    try:
        return asyncio.run(_serve(args.session_bus, args.state_path))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
