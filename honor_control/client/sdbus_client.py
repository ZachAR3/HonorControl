"""Async sdbus client for the Honor Control service.

Production defaults to the system bus; tests inject a bus.  Performs
``GetApiVersion`` handshake.  Rejects unsupported major versions with a
clear upgrade mismatch.  Subscribes to ``StateChanged``; coalesces bursts
and fetches one new snapshot.

No ``dbus-python`` dependency.  No raw D-Bus values reach callers —
everything is decoded into domain DTOs.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable
from datetime import datetime
from typing import Any

from honor_control.client.errors import ClientError, classify_dbus_error
from honor_control.client.protocol import ControlClient
from honor_control.client.proxy import ControlProxy
from honor_control.contract import API_VERSION, BUS_NAME, OBJECT_PATH
from honor_control.core.errors import CapabilityStatus, TransportError
from honor_control.core.models import (
    BatterySnapshot,
    BatteryStatusKind,
    Capability,
    ChargeMode,
    FanMode,
    FanSnapshot,
    GestureEntry,
    GesturesSnapshot,
    GpuSnapshot,
    OperationResult,
    OperationStatus,
    PlatformInfo,
    PowerProfileEntry,
    PowerSnapshot,
    ServiceHealth,
    SystemSnapshot,
)

log = logging.getLogger("honor_control.client.sdbus_client")

#: Default per-call timeout (seconds).
DEFAULT_TIMEOUT = 10.0


class SdbusClient(ControlClient):
    """Async sdbus client for ``org.honorlinux.Control1``.

    Constructing it is cheap; the real D-Bus connection is opened lazily
    on :meth:`connect`.  ``connected`` reports whether the service is
    reachable and the API version matches.
    """

    def __init__(
        self,
        bus_kind: str = "system",
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self._bus_kind = bus_kind
        self._timeout = timeout
        self._bus: Any = None
        self._proxy: Any = None
        self._connected = False
        self._subscribers: list[Callable[[SystemSnapshot], Any]] = []
        self._recovery_task: asyncio.Task | None = None
        self._signal_task: asyncio.Task | None = None
        self._refresh_task: asyncio.Task | None = None

    @property
    def connected(self) -> bool:
        return self._connected

    async def connect(self) -> None:
        """Open the bus, perform version handshake, subscribe to signals."""
        try:
            import sdbus

            if self._bus_kind == "session":
                self._bus = sdbus.sd_bus_open_user()
            else:
                self._bus = sdbus.sd_bus_open_system()
            # Create a proxy for the root object.
            self._proxy = ControlProxy.new_proxy(BUS_NAME, OBJECT_PATH, bus=self._bus)

            # Version handshake.
            server_version = await self._call_method("GetApiVersion")
            if server_version != API_VERSION:
                raise ClientError(
                    TransportError.API_MISMATCH,
                    f"API version mismatch: client={API_VERSION},"
                    f" server={server_version}",
                )
            self._connected = True
            self._signal_task = asyncio.create_task(
                self._listen_for_changes(), name="honor-control-signals"
            )
            log.info("connected to %s (API v%d)", BUS_NAME, server_version)
        except ClientError:
            await self.close()
            raise
        except Exception as exc:  # noqa: BLE001
            log.warning("connect failed: %s", exc)
            await self.close()
            raise ClientError(
                TransportError.SERVICE_UNAVAILABLE,
                f"Cannot connect to service: {exc}",
            ) from exc

    async def close(self) -> None:
        """Close the bus and cancel background tasks."""
        self._connected = False
        if self._recovery_task is not None:
            self._recovery_task.cancel()
            self._recovery_task = None
        for task in (self._signal_task, self._refresh_task):
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        self._signal_task = None
        self._refresh_task = None
        if self._bus is not None:
            try:
                self._bus.close()
            except Exception:  # noqa: BLE001
                pass
            self._bus = None
        self._proxy = None

    async def _call_method(self, method: str, *args: Any) -> Any:
        """Invoke a D-Bus method and return the decoded result."""
        if self._proxy is None:
            raise ClientError(
                TransportError.SERVICE_UNAVAILABLE,
                "Not connected",
            )
        try:
            member = getattr(self._proxy, method, None)
            if member is None:
                raise ClientError(
                    TransportError.INTERNAL, f"Unknown client method: {method}"
                )
            result = await asyncio.wait_for(member(*args), timeout=self._timeout)
            return _from_variant(result)
        except TimeoutError:
            raise ClientError(TransportError.TIMEOUT, f"{method} timed out")
        except ClientError:
            raise
        except Exception as exc:  # noqa: BLE001
            error_name = getattr(exc, "dbus_error_name", "") or str(exc)
            classified = classify_dbus_error(error_name, str(exc))
            if classified.code == TransportError.SERVICE_UNAVAILABLE:
                self._connected = False
            raise classified from exc

    # -- Reads --

    async def get_api_version(self) -> int:
        return int(await self._call_method("GetApiVersion"))

    async def get_schema_version(self) -> int:
        return int(await self._call_method("GetSchemaVersion"))

    async def get_snapshot(self) -> SystemSnapshot:
        result = await self._call_method("GetSnapshot")
        return _decode_snapshot(result)

    async def run_checks(self) -> dict[str, Any]:
        return await self._call_method("RunChecks")

    # -- Battery --

    async def set_thresholds(self, end: int, start: int) -> OperationResult:
        return _decode_result(await self._call_method("SetThresholds", end, start))

    async def set_mode(self, mode: str) -> OperationResult:
        return _decode_result(await self._call_method("SetMode", mode))

    # -- Power --

    async def set_profile(self, profile: str) -> OperationResult:
        return _decode_result(await self._call_method("SetProfile", profile))

    async def set_auto_switch(self, enabled: bool) -> OperationResult:
        return _decode_result(await self._call_method("SetAutoSwitch", enabled))

    async def save_power_profile(
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
    ) -> OperationResult:
        return _decode_result(
            await self._call_method(
                "SavePowerProfile",
                name,
                label,
                description,
                pl1_uw,
                pl2_uw,
                governor,
                epp,
                ppd_profile,
                turbo_enabled,
                max_perf_pct,
            )
        )

    async def delete_power_profile(self, name: str) -> OperationResult:
        return _decode_result(await self._call_method("DeletePowerProfile", name))

    async def configure_auto_switch(
        self,
        enabled: bool,
        on_ac: str,
        on_battery: str,
        on_ac_script: str,
        on_battery_script: str,
    ) -> OperationResult:
        return _decode_result(
            await self._call_method(
                "ConfigureAutoSwitch",
                enabled,
                on_ac,
                on_battery,
                on_ac_script,
                on_battery_script,
            )
        )

    # -- Fan --

    async def set_stock_auto(self) -> OperationResult:
        return _decode_result(await self._call_method("SetStockAuto"))

    async def set_curve(
        self, profile: str, points: list[tuple[int, int]]
    ) -> OperationResult:
        return _decode_result(await self._call_method("SetCurve", profile, points))

    async def set_manual(self, speed: int, ttl_seconds: int) -> OperationResult:
        return _decode_result(await self._call_method("SetManual", speed, ttl_seconds))

    # -- Gestures --

    async def set_mapping(self, gesture_id: str, combo: str) -> OperationResult:
        return _decode_result(await self._call_method("SetMapping", gesture_id, combo))

    async def set_enabled(self, gesture_id: str, enabled: bool) -> OperationResult:
        return _decode_result(
            await self._call_method("SetEnabled", gesture_id, enabled)
        )

    async def set_all_enabled(self, enabled: bool) -> OperationResult:
        return _decode_result(await self._call_method("SetAllEnabled", enabled))

    async def set_daemon_enabled(self, enabled: bool) -> OperationResult:
        return _decode_result(await self._call_method("SetDaemonEnabled", enabled))

    # -- GPU --

    async def set_mitigation_enabled(self, enabled: bool) -> OperationResult:
        return _decode_result(await self._call_method("SetMitigationEnabled", enabled))

    # -- Config / diagnostics --

    async def reload(self) -> OperationResult:
        return _decode_result(await self._call_method("Reload"))

    async def get_debug_bundle(self) -> dict[str, Any]:
        return await self._call_method("GetDebugBundle")

    async def get_recent_logs(self, lines: int) -> list[str]:
        result = await self._call_method("GetRecentLogs", lines)
        return [str(x) for x in result] if result else []

    # -- Subscriptions --

    def on_state_changed(self, callback: Callable[[SystemSnapshot], Any]) -> None:
        """Register a callback for state-change notifications."""
        self._subscribers.append(callback)

    async def _listen_for_changes(self) -> None:
        """Coalesce service signals into snapshot refreshes."""
        assert self._proxy is not None
        try:
            async for _sequence, _domains in self._proxy.StateChanged.catch():
                if self._refresh_task is None or self._refresh_task.done():
                    self._refresh_task = asyncio.create_task(self._notify_snapshot())
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            self._connected = False
            log.warning("state signal listener stopped: %s", exc)

    async def _notify_snapshot(self) -> None:
        try:
            await asyncio.sleep(0)
            snapshot = await self.get_snapshot()
            for callback in tuple(self._subscribers):
                try:
                    result = callback(snapshot)
                    if asyncio.iscoroutine(result):
                        await result
                except Exception:  # noqa: BLE001
                    log.exception("state subscriber failed")
        except asyncio.CancelledError:
            raise
        except ClientError as exc:
            self._connected = False
            log.warning("snapshot notification failed: %s", exc.message)


class FakeClient:
    """In-memory client for tests and dev session-bus mode.

    Implements the same :class:`ControlClient` protocol but talks
    directly to an :class:`ApplicationService` without D-Bus.
    """

    def __init__(self, app: Any) -> None:
        self._app = app
        self._connected = True
        self._subscribers: list[Callable[[SystemSnapshot], Any]] = []

    @property
    def connected(self) -> bool:
        return self._connected

    async def connect(self) -> None:
        self._connected = True

    async def close(self) -> None:
        self._connected = False

    async def get_api_version(self) -> int:
        return await self._app.get_api_version()

    async def get_schema_version(self) -> int:
        return await self._app.get_schema_version()

    async def get_snapshot(self) -> SystemSnapshot:
        return await self._app.get_snapshot()

    async def run_checks(self) -> dict[str, Any]:
        return await self._app.run_checks()

    async def set_thresholds(self, end: int, start: int) -> OperationResult:
        return await self._app.set_battery_thresholds(end, start)

    async def set_mode(self, mode: str) -> OperationResult:
        return await self._app.set_battery_mode(mode)

    async def set_profile(self, profile: str) -> OperationResult:
        return await self._app.set_power_profile(profile)

    async def set_auto_switch(self, enabled: bool) -> OperationResult:
        return await self._app.set_auto_switch(enabled)

    async def save_power_profile(
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
    ) -> OperationResult:
        return await self._app.save_power_profile(
            name,
            label,
            description,
            pl1_uw,
            pl2_uw,
            governor,
            epp,
            ppd_profile,
            turbo_enabled,
            max_perf_pct,
        )

    async def delete_power_profile(self, name: str) -> OperationResult:
        return await self._app.delete_power_profile(name)

    async def configure_auto_switch(
        self,
        enabled: bool,
        on_ac: str,
        on_battery: str,
        on_ac_script: str,
        on_battery_script: str,
    ) -> OperationResult:
        return await self._app.configure_auto_switch(
            enabled, on_ac, on_battery, on_ac_script, on_battery_script
        )

    async def set_stock_auto(self) -> OperationResult:
        return await self._app.set_fan_stock_auto()

    async def set_curve(
        self, profile: str, points: list[tuple[int, int]]
    ) -> OperationResult:
        curve_str = ",".join(f"{t}:{s}" for t, s in points) if points else ""
        return await self._app.set_fan_curve(profile, curve_str)

    async def set_manual(self, speed: int, ttl_seconds: int) -> OperationResult:
        return await self._app.set_fan_manual(speed, ttl_seconds)

    async def set_mapping(self, gesture_id: str, combo: str) -> OperationResult:
        return await self._app.set_gesture_mapping(gesture_id, combo)

    async def set_enabled(self, gesture_id: str, enabled: bool) -> OperationResult:
        return await self._app.set_gesture_enabled(gesture_id, enabled)

    async def set_all_enabled(self, enabled: bool) -> OperationResult:
        return await self._app.set_all_gestures_enabled(enabled)

    async def set_daemon_enabled(self, enabled: bool) -> OperationResult:
        return await self._app.set_gesture_daemon_enabled(enabled)

    async def set_mitigation_enabled(self, enabled: bool) -> OperationResult:
        return await self._app.set_gpu_mitigation_enabled(enabled)

    async def reload(self) -> OperationResult:
        return await self._app.reload()

    async def get_debug_bundle(self) -> dict[str, Any]:
        return await self._app.get_debug_bundle()

    async def get_recent_logs(self, lines: int) -> list[str]:
        return await self._app.get_recent_logs(lines)

    def on_state_changed(self, callback: Callable[[SystemSnapshot], Any]) -> None:
        self._subscribers.append(callback)


def _decode_snapshot(data: dict[str, Any]) -> SystemSnapshot:
    """Decode a wire ``a{sv}`` dict into a :class:`SystemSnapshot`.

    Forward-compatible: unknown keys are ignored.  Invalid wire data is a
    protocol error, not empty state.
    """
    if not isinstance(data, dict):
        raise ClientError(TransportError.INTERNAL, "Snapshot is not a dict")
    # Unwrap variant tuples if present.
    decoded = {k: _from_variant(v) for k, v in data.items()} if data else {}
    service = _dict(decoded.get("service"))
    platform = _dict(decoded.get("platform"))
    battery = _dict(decoded.get("battery"))
    power = _dict(decoded.get("power"))
    fan = _dict(decoded.get("fan"))
    gestures = _dict(decoded.get("gestures"))
    gpu = _dict(decoded.get("gpu"))
    capabilities: dict[str, Capability] = {}
    for name, raw in _dict(decoded.get("capabilities")).items():
        item = _dict(raw)
        capabilities[str(name)] = Capability(
            status=_enum(
                CapabilityStatus, item.get("status"), CapabilityStatus.UNAVAILABLE
            ),
            reason_code=_str(item.get("reason_code")),
            message=_str(item.get("message")),
            resources=_str_tuple(item.get("resources")),
        )
    mappings = tuple(
        GestureEntry(
            id=_str(item.get("id")),
            label=_str(item.get("label")),
            enabled=_bool(item.get("enabled")),
            mapping=_str(item.get("mapping")),
            default_mapping=_str(item.get("default_mapping")),
            error=_str(item.get("error")),
        )
        for item in (_dict(x) for x in _list(gestures.get("mappings")))
    )
    return SystemSnapshot(
        api_version=int(decoded.get("api_version", 1)),
        schema_version=int(decoded.get("schema_version", 1)),
        sequence=int(decoded.get("sequence", 0)),
        observed_at=_datetime(decoded.get("observed_at")),
        service=ServiceHealth(
            version=_str(service.get("version")),
            uptime=_int(service.get("uptime"), 0) or 0,
            overall=_str(service.get("overall"), "degraded"),
            controller_health={
                str(k): _str(v)
                for k, v in _dict(service.get("controller_health")).items()
            },
            dependency_ok=_bool(service.get("dependency_ok")),
            config_valid=_bool(service.get("config_valid")),
            stale_domains=_str_tuple(service.get("stale_domains")),
            last_fault=_str(service.get("last_fault")),
        ),
        platform=PlatformInfo(
            vendor=_str(platform.get("vendor")),
            product=_str(platform.get("product")),
            model=_str(platform.get("model")),
            cpu_model=_str(platform.get("cpu_model")),
            matched=_bool(platform.get("matched")),
            confidence=_str(platform.get("confidence"), "none"),
        ),
        capabilities=capabilities,
        battery=BatterySnapshot(
            available=_bool(battery.get("available")),
            capacity_percent=_int(battery.get("capacity_percent")),
            status=_optional_enum(BatteryStatusKind, battery.get("status")),
            ac_online=_optional_bool(battery.get("ac_online")),
            observed_end=_int(battery.get("observed_end")),
            observed_start=_int(battery.get("observed_start")),
            desired_end=_int(battery.get("desired_end")),
            desired_start=_int(battery.get("desired_start")),
            mode=_enum(ChargeMode, battery.get("mode"), ChargeMode.CUSTOM),
            last_error=_str(battery.get("last_error")),
        ),
        power=PowerSnapshot(
            available=_bool(power.get("available")),
            desired_profile=_str(power.get("desired_profile")),
            applied_profile=_str(power.get("applied_profile")),
            observed_summary=_dict(power.get("observed_summary")),
            ac_online=_optional_bool(power.get("ac_online")),
            auto_switch_enabled=_bool(power.get("auto_switch_enabled")),
            auto_switch_on_ac=_str(power.get("auto_switch_on_ac")),
            auto_switch_on_battery=_str(power.get("auto_switch_on_battery")),
            auto_switch_on_ac_script=_str(power.get("auto_switch_on_ac_script")),
            auto_switch_on_battery_script=_str(
                power.get("auto_switch_on_battery_script")
            ),
            auto_switch_last_script_status=_str(
                power.get("auto_switch_last_script_status")
            ),
            profiles=tuple(
                PowerProfileEntry(
                    name=_str(item.get("name")),
                    label=_str(item.get("label")),
                    description=_str(item.get("description")),
                    pl1_uw=_int(item.get("pl1_uw"), 25_000_000) or 25_000_000,
                    pl2_uw=_int(item.get("pl2_uw"), 35_000_000) or 35_000_000,
                    governor=_str(item.get("governor")),
                    epp=_str(item.get("epp")),
                    ppd_profile=_str(item.get("ppd_profile")),
                    turbo_enabled=(
                        True
                        if "turbo_enabled" not in item
                        else _bool(item.get("turbo_enabled"))
                    ),
                    max_perf_pct=_int(item.get("max_perf_pct"), 100) or 100,
                    built_in=_bool(item.get("built_in")),
                )
                for raw in _list(power.get("profiles"))
                if (item := _dict(raw)).get("name")
            ),
            last_error=_str(power.get("last_error")),
        ),
        fan=FanSnapshot(
            available=_bool(fan.get("available")),
            mode=_enum(FanMode, fan.get("mode"), FanMode.STOCK),
            desired_mode=_enum(FanMode, fan.get("desired_mode"), FanMode.STOCK),
            temp_mc=_int(fan.get("temp_mc")),
            target_speed=_int(fan.get("target_speed")),
            measured_rpm=_int(fan.get("measured_rpm")),
            curves={str(k): _str(v) for k, v in _dict(fan.get("curves")).items()},
            manual_expires_at=_optional_datetime(fan.get("manual_expires_at")),
            last_error=_str(fan.get("last_error")),
        ),
        gestures=GesturesSnapshot(
            available=_bool(gestures.get("available")),
            daemon_enabled=_bool(gestures.get("daemon_enabled")),
            daemon_running=_bool(gestures.get("daemon_running")),
            device_found=_bool(gestures.get("device_found")),
            permission_denied=_bool(gestures.get("permission_denied")),
            device_path=_str(gestures.get("device_path")),
            reports_seen=_int(gestures.get("reports_seen"), 0) or 0,
            gestures_emitted=_int(gestures.get("gestures_emitted"), 0) or 0,
            mappings=mappings,
            wmi_transport_present=_bool(gestures.get("wmi_transport_present")),
            firmware_settings_supported=_bool(
                gestures.get("firmware_settings_supported")
            ),
            last_error=_str(gestures.get("last_error")),
        ),
        gpu=GpuSnapshot(
            available=_bool(gpu.get("available")),
            mitigation_enabled=_bool(gpu.get("mitigation_enabled")),
            target_cpu=_int(gpu.get("target_cpu")),
            irqs=_str_tuple(gpu.get("irqs")),
            last_error=_str(gpu.get("last_error")),
        ),
        stale_domains=_str_tuple(decoded.get("stale_domains")),
        errors=_str_tuple(decoded.get("errors")),
    )


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _list(value: Any) -> list[Any]:
    return list(value) if isinstance(value, (list, tuple)) else []


def _str(value: Any, default: str = "") -> str:
    return default if value is None else str(value)


def _str_tuple(value: Any) -> tuple[str, ...]:
    return tuple(str(x) for x in _list(value))


def _int(value: Any, default: int | None = None) -> int | None:
    if value in (None, ""):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _bool(value: Any) -> bool:
    return value is True or value == 1


def _optional_bool(value: Any) -> bool | None:
    return None if value in (None, "") else _bool(value)


def _enum(enum_type, value: Any, default):
    try:
        return enum_type(str(value))
    except (TypeError, ValueError):
        return default


def _optional_enum(enum_type, value: Any):
    return None if value in (None, "") else _enum(enum_type, value, None)


def _datetime(value: Any) -> datetime:
    parsed = _optional_datetime(value)
    return parsed or SystemSnapshot().observed_at


def _optional_datetime(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


def _decode_result(data: dict[str, Any]) -> OperationResult:
    """Decode a wire ``a{sv}`` dict into an :class:`OperationResult`."""
    if not isinstance(data, dict):
        raise ClientError(TransportError.INTERNAL, "Result is not a dict")
    # Unwrap variant tuples if present.
    decoded = {k: _from_variant(v) for k, v in data.items()} if data else {}
    status_str = str(decoded.get("status", "failed"))
    try:
        status = OperationStatus(status_str)
    except ValueError:
        status = OperationStatus.FAILED
    details = decoded.get("details", {})
    if not isinstance(details, dict):
        details = {}
    return OperationResult(
        status=status,
        code=str(decoded.get("code", "")),
        message=str(decoded.get("message", "")),
        changed=bool(decoded.get("changed", False)),
        persisted=bool(decoded.get("persisted", False)),
        applied=bool(decoded.get("applied", False)),
        sequence=int(decoded.get("sequence", 0)),
        details=details,
    )


def _from_variant(value: Any) -> Any:
    if isinstance(value, tuple) and len(value) == 2:
        return _from_variant(value[1])
    if isinstance(value, dict):
        return {str(key): _from_variant(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_from_variant(item) for item in value]
    return value
