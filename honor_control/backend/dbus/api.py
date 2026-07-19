"""D-Bus interface layer: exported interfaces only.

This is a thin, versioned transport.  All mutation authority is
attributable to the actual caller through the authorizer.  No feature
logic lives here — methods delegate directly to the async
:class:`ApplicationService`.

The service exports one root object with the ``org.honorlinux.Control1``
interface.  Feature mutations are methods on that interface.  Reads
return cached snapshots.  A ``StateChanged`` signal carries the new
sequence and changed domains.
"""

from __future__ import annotations

import asyncio
import logging
import pathlib
from typing import Any

from sdbus import (
    DbusFailedError,
    DbusInterfaceCommonAsync,
    DbusUnprivilegedFlag,
    dbus_method_async,
    dbus_signal_async,
)

from honor_control.backend.dbus.authorizer import (
    Authorizer,
    CallerSubject,
    PolkitAuthorizer,
    _parse_start_time,
)
from honor_control.backend.dbus.codec import (
    operation_result_to_vardict,
    snapshot_to_vardict,
    to_vardict,
)
from honor_control.contract import (
    IFACE_ROOT,
    OBJECT_PATH,
)
from honor_control.core.errors import DomainError, DomainException

log = logging.getLogger("honor_control.backend.dbus.api")


class NotAuthorizedError(DbusFailedError):
    dbus_error_name = "org.honorlinux.Control1.Error.NotAuthorized"


class InvalidArgumentError(DbusFailedError):
    dbus_error_name = "org.honorlinux.Control1.Error.InvalidArgument"


class UnsupportedError(DbusFailedError):
    dbus_error_name = "org.honorlinux.Control1.Error.Unsupported"


class UnavailableError(DbusFailedError):
    dbus_error_name = "org.honorlinux.Control1.Error.Unavailable"


class BusyError(DbusFailedError):
    dbus_error_name = "org.honorlinux.Control1.Error.Busy"


class ControlTimeoutError(DbusFailedError):
    dbus_error_name = "org.honorlinux.Control1.Error.Timeout"


class DependencyError(DbusFailedError):
    dbus_error_name = "org.honorlinux.Control1.Error.Dependency"


class InternalError(DbusFailedError):
    dbus_error_name = "org.honorlinux.Control1.Error.Internal"


_DBUS_ERRORS = {
    DomainError.NOT_AUTHORIZED: NotAuthorizedError,
    DomainError.INVALID_ARGUMENT: InvalidArgumentError,
    DomainError.UNSUPPORTED: UnsupportedError,
    DomainError.UNAVAILABLE: UnavailableError,
    DomainError.BUSY: BusyError,
    DomainError.TIMEOUT: ControlTimeoutError,
    DomainError.DEPENDENCY: DependencyError,
    DomainError.INTERNAL: InternalError,
}


class BusDaemonInterface(
    DbusInterfaceCommonAsync, interface_name="org.freedesktop.DBus"
):
    @dbus_method_async(input_signature="s", result_signature="u")
    async def GetConnectionUnixProcessID(self, sender: str) -> int:
        raise NotImplementedError

    @dbus_method_async(input_signature="s", result_signature="u")
    async def GetConnectionUnixUser(self, sender: str) -> int:
        raise NotImplementedError


async def _capture_caller() -> CallerSubject | None:
    """Capture the D-Bus sender unique name and credentials.

    Returns ``None`` when running outside an sdbus method context (e.g.
    in tests or direct in-process calls).  The authorizer fails closed
    in that case for privileged methods.
    """
    try:
        import sdbus

        msg = sdbus.get_current_message()
        sender = getattr(msg, "sender", None)
        if not sender:
            return None
        daemon = BusDaemonInterface.new_proxy(
            "org.freedesktop.DBus",
            "/org/freedesktop/DBus",
            bus=sdbus.get_default_bus(),
        )
        pid = int(
            await asyncio.wait_for(
                daemon.GetConnectionUnixProcessID(sender), timeout=2.0
            )
        )
        uid = int(
            await asyncio.wait_for(daemon.GetConnectionUnixUser(sender), timeout=2.0)
        )
        stat = await asyncio.wait_for(
            asyncio.to_thread(
                pathlib.Path(f"/proc/{pid}/stat").read_text,
                encoding="utf-8",
            ),
            timeout=2.0,
        )
        start_time = _parse_start_time(stat)
        if pid <= 0 or uid < 0 or start_time <= 0:
            return None
        return CallerSubject(sender=sender, pid=pid, uid=uid, start_time=start_time)
    except Exception:  # noqa: BLE001
        return None


class ControlInterface(DbusInterfaceCommonAsync, interface_name=IFACE_ROOT):
    """Root D-Bus interface: versioned API, snapshot, mutations, signals.

    Every method delegates to :class:`ApplicationService`.  Mutations
    are authorized through the injected :class:`Authorizer` before any
    executor hop.
    """

    def __init__(
        self,
        app: Any = None,
        authorizer: Authorizer | None = None,
    ) -> None:
        super().__init__()
        self._app: Any = app
        # A transport object constructed without explicit wiring must still
        # fail closed.  Tests that need permissive behavior inject FakeAuthorizer.
        self._authorizer: Authorizer = authorizer or PolkitAuthorizer()

    def wire(self, app: Any, authorizer: Authorizer) -> None:
        """Wire the application service and authorizer (called by service.py)."""
        self._app = app
        self._authorizer = authorizer

    async def _authorize(self, method: str) -> None:
        """Authorize a method call before any executor hop."""
        caller = await _capture_caller()
        try:
            await self._authorizer.check(method, caller)
        except DomainException as exc:
            log.warning(
                "authorization denied for %s: %s (caller=%s)",
                method,
                exc.message,
                caller.sender if caller else "none",
            )
            error_type = _DBUS_ERRORS.get(exc.code, InternalError)
            raise error_type(exc.message) from exc

    async def _call(self, awaitable):
        """Map application-domain exceptions to stable D-Bus errors."""
        try:
            return await awaitable
        except DomainException as exc:
            error_type = _DBUS_ERRORS.get(exc.code, InternalError)
            raise error_type(exc.message) from exc
        except Exception as exc:  # noqa: BLE001
            log.exception("application call failed")
            raise InternalError("Internal service error") from exc

    # -- Read-only methods (no authorization) --

    @dbus_method_async(flags=DbusUnprivilegedFlag, result_signature="u")
    async def GetApiVersion(self) -> int:
        """Return the D-Bus API version."""
        return await self._app.get_api_version()

    @dbus_method_async(flags=DbusUnprivilegedFlag, result_signature="u")
    async def GetSchemaVersion(self) -> int:
        """Return the snapshot schema version."""
        return await self._app.get_schema_version()

    @dbus_method_async(flags=DbusUnprivilegedFlag, result_signature="a{sv}")
    async def GetSnapshot(self) -> dict:
        """Return the current system snapshot as an ``a{sv}`` dict."""
        snap = await self._app.get_snapshot()
        return snapshot_to_vardict(snap)

    @dbus_method_async(flags=DbusUnprivilegedFlag, result_signature="a{sv}")
    async def RunChecks(self) -> dict:
        """Run diagnostic checks and return results."""
        result = await self._app.run_checks()
        return to_vardict(result)

    # -- Battery mutations --

    @dbus_method_async(
        flags=DbusUnprivilegedFlag, input_signature="ii", result_signature="a{sv}"
    )
    async def SetThresholds(self, end: int, start: int) -> dict:
        """Set battery charge thresholds (end, start)."""
        await self._authorize("SetThresholds")
        result = await self._call(
            self._app.set_battery_thresholds(int(end), int(start))
        )
        return operation_result_to_vardict(result)

    @dbus_method_async(
        flags=DbusUnprivilegedFlag, input_signature="s", result_signature="a{sv}"
    )
    async def SetMode(self, mode: str) -> dict:
        """Apply a charge-mode preset."""
        await self._authorize("SetMode")
        result = await self._call(self._app.set_battery_mode(str(mode)))
        return operation_result_to_vardict(result)

    # -- Power mutations --

    @dbus_method_async(
        flags=DbusUnprivilegedFlag, input_signature="s", result_signature="a{sv}"
    )
    async def SetProfile(self, profile: str) -> dict:
        """Apply a power profile."""
        await self._authorize("SetProfile")
        result = await self._call(self._app.set_power_profile(str(profile)))
        return operation_result_to_vardict(result)

    @dbus_method_async(
        flags=DbusUnprivilegedFlag, input_signature="b", result_signature="a{sv}"
    )
    async def SetAutoSwitch(self, enabled: bool) -> dict:
        """Enable or disable AC/battery auto-profile switching."""
        await self._authorize("SetAutoSwitch")
        result = await self._call(self._app.set_auto_switch(bool(enabled)))
        return operation_result_to_vardict(result)

    @dbus_method_async(
        flags=DbusUnprivilegedFlag,
        input_signature="sssiisssbi",
        result_signature="a{sv}",
    )
    async def SavePowerProfile(
        self,
        name: str,
        label: str,
        description: str,
        pl1_uw: int,
        pl2_uw: int,
        governor: str,
        epp: str,
        ppd_profile: str,
        turbo_enabled: bool,
        max_perf_pct: int,
    ) -> dict:
        """Create or update a typed power profile."""
        await self._authorize("SavePowerProfile")
        result = await self._call(
            self._app.save_power_profile(
                str(name),
                str(label),
                str(description),
                int(pl1_uw),
                int(pl2_uw),
                str(governor),
                str(epp),
                str(ppd_profile),
                bool(turbo_enabled),
                int(max_perf_pct),
            )
        )
        return operation_result_to_vardict(result)

    @dbus_method_async(
        flags=DbusUnprivilegedFlag, input_signature="s", result_signature="a{sv}"
    )
    async def DeletePowerProfile(self, name: str) -> dict:
        """Delete an unreferenced custom power profile."""
        await self._authorize("DeletePowerProfile")
        result = await self._call(self._app.delete_power_profile(str(name)))
        return operation_result_to_vardict(result)

    @dbus_method_async(
        flags=DbusUnprivilegedFlag,
        input_signature="bssss",
        result_signature="a{sv}",
    )
    async def ConfigureAutoSwitch(
        self,
        enabled: bool,
        on_ac: str,
        on_battery: str,
        on_ac_script: str,
        on_battery_script: str,
    ) -> dict:
        """Configure automatic profiles and optional direct script hooks."""
        await self._authorize("ConfigureAutoSwitch")
        result = await self._call(
            self._app.configure_auto_switch(
                bool(enabled),
                str(on_ac),
                str(on_battery),
                str(on_ac_script),
                str(on_battery_script),
            )
        )
        return operation_result_to_vardict(result)

    # -- Fan mutations --

    @dbus_method_async(flags=DbusUnprivilegedFlag, result_signature="a{sv}")
    async def SetStockAuto(self) -> dict:
        """Restore the EC's stock auto fan mode."""
        await self._authorize("SetStockAuto")
        result = await self._call(self._app.set_fan_stock_auto())
        return operation_result_to_vardict(result)

    @dbus_method_async(
        flags=DbusUnprivilegedFlag, input_signature="sa(ii)", result_signature="a{sv}"
    )
    async def SetCurve(self, profile: str, points: list) -> dict:
        """Set a fan curve for a profile."""
        await self._authorize("SetCurve")
        # Convert a(ii) back to curve string.
        curve_str = ",".join(f"{p[0]}:{p[1]}" for p in points) if points else ""
        result = await self._call(self._app.set_fan_curve(str(profile), curve_str))
        return operation_result_to_vardict(result)

    @dbus_method_async(
        flags=DbusUnprivilegedFlag, input_signature="iu", result_signature="a{sv}"
    )
    async def SetManual(self, speed: int, ttl_seconds: int) -> dict:
        """Set a fixed fan speed with a TTL."""
        await self._authorize("SetManual")
        result = await self._call(
            self._app.set_fan_manual(int(speed), int(ttl_seconds))
        )
        return operation_result_to_vardict(result)

    # -- Gesture mutations --

    @dbus_method_async(
        flags=DbusUnprivilegedFlag, input_signature="ss", result_signature="a{sv}"
    )
    async def SetMapping(self, gesture_id: str, combo: str) -> dict:
        """Set a gesture's key combo."""
        await self._authorize("SetMapping")
        result = await self._call(
            self._app.set_gesture_mapping(str(gesture_id), str(combo))
        )
        return operation_result_to_vardict(result)

    @dbus_method_async(
        flags=DbusUnprivilegedFlag, input_signature="sb", result_signature="a{sv}"
    )
    async def SetEnabled(self, gesture_id: str, enabled: bool) -> dict:
        """Enable or disable a gesture (preserves custom mapping)."""
        await self._authorize("SetEnabled")
        result = await self._call(
            self._app.set_gesture_enabled(str(gesture_id), bool(enabled))
        )
        return operation_result_to_vardict(result)

    @dbus_method_async(
        flags=DbusUnprivilegedFlag, input_signature="b", result_signature="a{sv}"
    )
    async def SetAllEnabled(self, enabled: bool) -> dict:
        """Atomically enable or disable all gestures."""
        await self._authorize("SetAllEnabled")
        result = await self._call(self._app.set_all_gestures_enabled(bool(enabled)))
        return operation_result_to_vardict(result)

    @dbus_method_async(
        flags=DbusUnprivilegedFlag, input_signature="b", result_signature="a{sv}"
    )
    async def SetDaemonEnabled(self, enabled: bool) -> dict:
        """Enable or disable the gesture daemon."""
        await self._authorize("SetDaemonEnabled")
        result = await self._call(self._app.set_gesture_daemon_enabled(bool(enabled)))
        return operation_result_to_vardict(result)

    @dbus_method_async(
        flags=DbusUnprivilegedFlag, input_signature="a{si}", result_signature="a{sv}"
    )
    async def ApplyTouchpadSettings(self, settings: dict) -> dict:
        """Apply a validated touchpad firmware settings profile."""
        await self._authorize("ApplyTouchpadSettings")
        result = await self._call(
            self._app.apply_touchpad_settings(
                {str(name): int(value) for name, value in settings.items()}
            )
        )
        return operation_result_to_vardict(result)

    @dbus_method_async(
        flags=DbusUnprivilegedFlag, input_signature="si", result_signature="a{sv}"
    )
    async def SetTouchpadSetting(self, setting: str, value: int) -> dict:
        """Apply one validated touchpad firmware setting."""
        await self._authorize("SetTouchpadSetting")
        result = await self._call(
            self._app.set_touchpad_setting(str(setting), int(value))
        )
        return operation_result_to_vardict(result)

    @dbus_method_async(flags=DbusUnprivilegedFlag, result_signature="a{sv}")
    async def QueryTouchpadSupport(self) -> dict:
        """Query the firmware support bitmap with exclusive reader access."""
        await self._authorize("QueryTouchpadSupport")
        return to_vardict(await self._call(self._app.query_touchpad_support()))

    @dbus_method_async(flags=DbusUnprivilegedFlag, result_signature="a{sv}")
    async def ProbeTouchpadFirmware(self) -> dict:
        """Probe DMI, hidraw identity, descriptor, and access."""
        await self._authorize("ProbeTouchpadFirmware")
        return to_vardict(await self._call(self._app.probe_touchpad_firmware()))

    # -- GPU mutations --

    @dbus_method_async(
        flags=DbusUnprivilegedFlag, input_signature="b", result_signature="a{sv}"
    )
    async def SetMitigationEnabled(self, enabled: bool) -> dict:
        """Apply or restore GPU IRQ mitigation."""
        await self._authorize("SetMitigationEnabled")
        result = await self._call(self._app.set_gpu_mitigation_enabled(bool(enabled)))
        return operation_result_to_vardict(result)

    # -- Config / diagnostics --

    @dbus_method_async(flags=DbusUnprivilegedFlag, result_signature="a{sv}")
    async def Reload(self) -> dict:
        """Reload config from disk."""
        await self._authorize("Reload")
        result = await self._call(self._app.reload())
        return operation_result_to_vardict(result)

    @dbus_method_async(flags=DbusUnprivilegedFlag, result_signature="a{sv}")
    async def GetDebugBundle(self) -> dict:
        """Return a bounded, redacted debug bundle as ``a{sv}``."""
        await self._authorize("GetDebugBundle")
        bundle = await self._call(self._app.get_debug_bundle())
        return to_vardict(bundle)

    @dbus_method_async(
        flags=DbusUnprivilegedFlag, input_signature="u", result_signature="as"
    )
    async def GetRecentLogs(self, lines: int) -> list:
        """Return recent backend log lines."""
        await self._authorize("GetRecentLogs")
        result = await self._call(self._app.get_recent_logs(int(lines)))
        return result

    # -- Signal --

    @dbus_signal_async(signal_signature="tas")
    def StateChanged(self):
        """Emitted with the new sequence and changed domains.

        Carries ``(t sequence, as domains)``.
        """


def build_objects(
    app: Any, authorizer: Authorizer
) -> dict[str, DbusInterfaceCommonAsync]:
    """Instantiate every interface object keyed by its object path."""
    root = ControlInterface(app, authorizer)
    return {OBJECT_PATH: root}
