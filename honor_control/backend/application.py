"""Application service: async use cases that orchestrate hardware, config, and state.

This is the core of the backend.  D-Bus methods invoke async application
methods directly.  No D-Bus, Qt, or ``honor-tools`` imports here — only
domain models, the hardware port, config store, snapshot store, command
queue, and supervisor.

Safety invariants:
  * All hardware mutations go through the serialized command queue.
  * Desired/applied/observed state are separate.
  * Mutations return structured :class:`OperationResult`.
  * Unsupported hardware never performs writes.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import os
import pathlib
import shlex
import signal
import stat
import time
from collections.abc import Callable
from dataclasses import replace
from datetime import timedelta
from typing import Any

from honor_control import __version__
from honor_control.backend.command_queue import (
    CommandTimeoutError,
    HardwareCommandQueue,
)
from honor_control.backend.config_store import (
    BatteryState,
    ConfigStore,
    FanState,
    GestureMappingState,
    GesturesState,
    GpuState,
    PowerAutoSwitchState,
    PowerProfileState,
    PowerState,
    ServiceState,
    TouchpadState,
)
from honor_control.backend.gesture_runtime import (
    GestureRuntime,
    wmi_transport_present,
)
from honor_control.backend.hardware import HardwarePort, verify_power_definition
from honor_control.backend.snapshot_store import SnapshotStore
from honor_control.backend.supervisor import RuntimeSupervisor
from honor_control.backend.touchpad_firmware import TouchpadFirmwareError
from honor_control.core.errors import DomainError, DomainException
from honor_control.core.gestures import DEFAULT_GESTURE_MAPPINGS, GESTURE_NAMES
from honor_control.core.models import (
    POWER_PROFILE_NAMES,
    Capability,
    CapabilityStatus,
    FanMode,
    GestureEntry,
    GesturesSnapshot,
    OperationResult,
    PowerProfileEntry,
    ServiceHealth,
    SystemSnapshot,
    derive_charge_mode,
    utc_now,
)
from honor_control.core.touchpad import (
    SUPPORTED_GESTURE_BITS,
    parse_touchpad_setting,
    parse_touchpad_value,
)
from honor_control.core.validation import (
    thresholds_for_mode,
    validate_charge_mode,
    validate_gesture_id,
    validate_key_combo,
    validate_log_lines,
    validate_manual_speed,
    validate_manual_ttl,
    validate_power_profile,
    validate_thresholds,
)

log = logging.getLogger("honor_control.backend.application")

FAN_SPEED_WRITE_HYSTERESIS = 3
FAN_RECOVERY_WAIT_SECONDS = 10.0
AUTO_SWITCH_POLL_SECONDS = 2.0
AUTO_SWITCH_MAX_RETRY_SECONDS = 60.0


def _serialized_mutation(method):
    """Hold the application mutation lock for one complete use case."""

    @functools.wraps(method)
    async def wrapped(self, *args, **kwargs):
        async with self._mutation_lock:
            if self._closing:
                raise DomainException(
                    DomainError.UNAVAILABLE,
                    "Service is shutting down",
                )
            if self._fan_restore_pending:
                raise DomainException(
                    DomainError.BUSY,
                    "Stock fan restoration is still pending",
                )
            return await method(self, *args, **kwargs)

    return wrapped


def _power_definition(profile: PowerProfileState) -> dict[str, Any]:
    """Return the hardware-facing fields of a persisted power profile."""
    return {
        "pl1_uw": profile.pl1_uw,
        "pl2_uw": profile.pl2_uw,
        "governor": profile.governor,
        "epp": profile.epp,
        "ppd_profile": profile.ppd_profile,
        "turbo_enabled": profile.turbo_enabled,
        "max_perf_pct": profile.max_perf_pct,
    }


class ApplicationService:
    """Orchestrates hardware, config, and state for all features.

    The D-Bus layer calls these async methods directly.  Each mutation:
      1. validates input;
      2. captures old observed/desired state;
      3. writes through the command queue;
      4. reads back / verifies;
      5. returns a structured :class:`OperationResult`.
    """

    def __init__(
        self,
        hardware: HardwarePort,
        config_store: ConfigStore | None = None,
        snapshot_store: SnapshotStore | None = None,
        command_queue: HardwareCommandQueue | None = None,
        supervisor: RuntimeSupervisor | None = None,
        gesture_runtime: GestureRuntime | None = None,
    ) -> None:
        self._hw = hardware
        self._config = config_store or ConfigStore()
        self._snapshots = snapshot_store or SnapshotStore()
        self._queue = command_queue or HardwareCommandQueue()
        self._supervisor = supervisor or RuntimeSupervisor()
        self._gesture_runtime = gesture_runtime
        self._start_time = time.time()
        self._mutation_lock = asyncio.Lock()
        self._closing = False
        self._shutdown_complete = False
        self._manual_ttl_seconds = 0
        self._fan_fail_safe_error = ""
        self._fan_restore_pending = False
        self._fan_restore_task: asyncio.Task[bool] | None = None
        self._last_auto_script_status = "Not run"
        self._supervisor.register(
            "manual_fan_ttl", self._manual_fan_expiry, self._restore_fan_auto
        )
        if self._gesture_runtime is not None:
            self._supervisor.register("gesture_daemon", self._gesture_runtime.run)

    @property
    def snapshots(self) -> SnapshotStore:
        """Return the snapshot store."""
        return self._snapshots

    @property
    def config_store(self) -> ConfigStore:
        """Return the config store."""
        return self._config

    @property
    def supervisor(self) -> RuntimeSupervisor:
        """Return the runtime supervisor."""
        return self._supervisor

    @property
    def queue(self) -> HardwareCommandQueue:
        """Return the hardware command queue."""
        return self._queue

    # -- Lifecycle --

    async def initialize(self) -> None:
        """Load config, publish observed state, and reconcile user intent."""
        self._config.load()
        await self._refresh_all()
        await self._initialize_fan_safety()
        await self._reconcile_power_profile()

    async def start_background(self) -> None:
        """Start service-owned monitors after initial state is published."""
        self._supervisor.register("refresh", self._refresh_loop)
        self._supervisor.register("auto_switch", self._auto_switch_loop)
        self._supervisor.register(
            "fan_curve", self._fan_curve_loop, self._restore_fan_auto
        )
        await self._supervisor.start("refresh")
        await self._supervisor.start("auto_switch")
        await self._supervisor.start("fan_curve")
        await self._reconcile_gesture_runtime()
        await self._refresh_gestures()
        await self._refresh_service_health()

    async def shutdown(self) -> None:
        """Close mutation admission, drain one use case, then clean up."""
        if self._shutdown_complete:
            return
        self._closing = True
        async with self._mutation_lock:
            if self._shutdown_complete:
                return
            await self._supervisor.stop_all()
            fan_cap = self._snapshots.snapshot.capabilities.get("fan")
            if (
                fan_cap is not None
                and fan_cap.writable
                and not self._fan_restore_pending
            ):
                await self._restore_fan_auto()
            recovery = self._fan_restore_task
            if recovery is not None and not recovery.done():
                try:
                    await asyncio.wait_for(asyncio.shield(recovery), timeout=2.0)
                except TimeoutError:
                    log.warning("fan restoration is still pending during shutdown")
            # Let the serialized worker drain its stop marker when possible.
            # The worker is a daemon and the join is bounded, so a wedged
            # firmware call still cannot prevent process shutdown.
            self._queue.shutdown(wait=True, timeout=2.0)
            self._shutdown_complete = True

    # -- Reads --

    async def get_snapshot(self) -> SystemSnapshot:
        """Return the current system snapshot (cached, no hardware I/O)."""
        return self._snapshots.snapshot

    async def get_api_version(self) -> int:
        """Return the D-Bus API version."""
        from honor_control.contract import API_VERSION

        return API_VERSION

    async def get_schema_version(self) -> int:
        """Return the snapshot schema version."""
        from honor_control.contract import SCHEMA_VERSION

        return SCHEMA_VERSION

    @_serialized_mutation
    async def reload(self) -> OperationResult:
        """Reload config and reconcile only supported runtime transitions."""
        config_loaded = False
        try:
            previous = self._config.state
            self._config.reload()
            if not self._config.valid:
                return OperationResult.failed(
                    code="config_invalid",
                    message=self._config.last_error or "Configuration is invalid",
                    sequence=self._snapshots.sequence,
                )
            current = self._config.state
            config_loaded = True
            changed = current != previous
            applied = False
            failures: list[str] = []
            details: dict[str, Any] = {}

            if previous.fan.mode != current.fan.mode:
                if current.fan.mode == "stock":
                    await self._supervisor.stop("manual_fan_ttl")
                    await self._supervisor.stop("fan_curve")
                    restored = await self._restore_fan_auto()
                    details["fan"] = "stock_restored" if restored else "restore_failed"
                    if restored:
                        applied = True
                    else:
                        failures.append("fan stock-auto restoration failed")
                elif current.fan.mode == "curve":
                    started = await self._supervisor.start("fan_curve")
                    details["fan"] = (
                        "curve_controller_started" if started else "controller_start_failed"
                    )
                    if not started:
                        failures.append("fan curve controller did not start")

            gesture_changed = previous.gestures != current.gestures
            if gesture_changed:
                await self._reconcile_gesture_runtime()
                if self._gesture_runtime is not None:
                    expected = current.gestures.daemon_enabled
                    running = self._gesture_runtime.status.running
                    details["gestures"] = "running" if running else "stopped"
                    if running == expected:
                        applied = True
                    else:
                        failures.append("gesture runtime did not reach desired state")

            if previous.power != current.power:
                await self._refresh_power()
                target = self._power_reconcile_target()
                power_result = await self._reconcile_power_profile(target)
                if power_result is None:
                    details["power"] = (
                        "already_applied"
                        if self._snapshots.snapshot.power.applied_profile == target
                        else "not_writable"
                    )
                else:
                    details["power"] = power_result.to_dict()
                    if power_result.applied:
                        applied = True
                    else:
                        failures.append(power_result.message)

            await self._refresh_all()
            if failures:
                return OperationResult.partial(
                    code="reload_reconcile_failed",
                    message="Config loaded, but runtime reconciliation was incomplete",
                    persisted=False,
                    applied=False,
                    sequence=self._snapshots.sequence,
                    details={**details, "failures": failures},
                )
            return OperationResult.success(
                message="Config reloaded",
                changed=changed,
                persisted=False,
                applied=applied,
                sequence=self._snapshots.sequence,
                details=details,
            )
        except Exception as exc:  # noqa: BLE001
            if config_loaded:
                return OperationResult.partial(
                    code="reload_reconcile_failed",
                    message="Config loaded, but runtime reconciliation failed",
                    persisted=False,
                    applied=False,
                    sequence=self._snapshots.sequence,
                    details={"error": str(exc)},
                )
            return OperationResult.failed(
                code="reload_failed",
                message=str(exc),
                sequence=self._snapshots.sequence,
            )

    # -- Battery --

    @_serialized_mutation
    async def set_battery_thresholds(self, end: int, start: int) -> OperationResult:
        """Write both charge thresholds, read back, and persist desired state."""
        validate_thresholds(end, start)
        cap = await self._queue.run("battery_cap", self._hw.get_battery_capability)
        if not cap.writable:
            return OperationResult.unavailable(
                code="battery_unavailable",
                message=cap.message or "Battery charge control unavailable",
                sequence=self._snapshots.sequence,
            )
        old = self._snapshots.snapshot.battery
        result = await self._queue.run(
            "battery_write", self._hw.write_battery_thresholds, end, start
        )
        end_ok = bool(result.get("end_write_ok", False))
        start_ok = bool(result.get("start_write_ok", False))
        readback_ok = bool(result.get("readback_ok", False))
        if end_ok and start_ok and readback_ok:
            # Persist desired state.
            try:
                await self._config.update(
                    lambda s: ServiceState(
                        schema_version=s.schema_version,
                        battery=BatteryState(
                            end_threshold=end,
                            start_threshold=start,
                            mode=str(derive_charge_mode(end, start)),
                        ),
                        power=s.power,
                        fan=s.fan,
                        gestures=s.gestures,
                        touchpad=s.touchpad,
                        gpu=s.gpu,
                    )
                )
            except Exception as exc:  # noqa: BLE001
                await self._refresh_battery()
                return OperationResult.partial(
                    code="battery_applied_not_persisted",
                    message="Battery thresholds changed but could not be saved",
                    persisted=False,
                    applied=True,
                    sequence=self._snapshots.sequence,
                    details={**result, "persistence_error": str(exc)},
                )
            await self._refresh_battery()
            return OperationResult.success(
                message=f"Thresholds set to {end}%/{start}%",
                changed=True,
                persisted=True,
                applied=True,
                sequence=self._snapshots.sequence,
                details=result,
            )
        # Partial or failed: attempt rollback to old pair.
        if end_ok or start_ok:
            rollback: dict[str, Any] | None = None
            if old.observed_end is not None and old.observed_start is not None:
                try:
                    rollback = await self._queue.run(
                        "battery_rollback",
                        self._hw.write_battery_thresholds,
                        old.observed_end,
                        old.observed_start,
                    )
                except Exception as exc:  # noqa: BLE001
                    rollback = {"error": str(exc), "readback_ok": False}
            await self._refresh_battery()
            rollback_ok = bool(rollback and rollback.get("readback_ok"))
            return OperationResult.partial(
                code="battery_partial_write",
                message=(
                    f"Partial write: end_ok={end_ok}, start_ok={start_ok}; "
                    f"rollback={'verified' if rollback_ok else 'not verified'}"
                ),
                applied=False,
                sequence=self._snapshots.sequence,
                details={**result, "rollback": rollback or {}},
            )
        return OperationResult.failed(
            code="battery_write_failed",
            message="Battery threshold write failed",
            sequence=self._snapshots.sequence,
            details=result,
        )

    async def set_battery_mode(self, mode: str) -> OperationResult:
        """Apply a charge-mode preset."""
        validated = validate_charge_mode(mode)
        end, start = thresholds_for_mode(validated)
        return await self.set_battery_thresholds(end, start)

    # -- Power --

    @_serialized_mutation
    async def set_power_profile(self, profile: str) -> OperationResult:
        """Apply a power profile after validating against the registry."""
        name = validate_power_profile(
            profile, frozenset(self._config.state.power.profiles)
        )
        return await self._apply_power_profile(name, persist_desired=True)

    async def _apply_power_profile(
        self, name: str, *, persist_desired: bool
    ) -> OperationResult:
        """Apply one profile; auto-switch calls do not rewrite manual intent."""
        try:
            cap = await self._queue.run("power_cap", self._hw.get_power_capability)
        except Exception as exc:  # noqa: BLE001
            return OperationResult.failed(
                code="power_preflight_failed",
                message=str(exc),
                sequence=self._snapshots.sequence,
            )
        if not cap.writable:
            return OperationResult.unavailable(
                code="power_unavailable",
                message=cap.message or "Power profile control unavailable",
                sequence=self._snapshots.sequence,
            )
        profile = self._config.state.power.profiles[name]
        definition = _power_definition(profile)
        try:
            result = await self._queue.run(
                "power_apply", self._hw.apply_power_profile, name, definition
            )
        except Exception as exc:  # noqa: BLE001
            await self._refresh_power()
            return OperationResult.failed(
                code="power_apply_failed",
                message=str(exc),
                sequence=self._snapshots.sequence,
            )
        if result.get("error"):
            await self._refresh_power()
            return OperationResult.failed(
                code="power_apply_failed",
                message=str(result["error"]),
                sequence=self._snapshots.sequence,
                details=result,
            )
        checks = {
            key: result.get(key) is True
            for key in (
                "governor_ok",
                "epp_ok",
                "ppd_ok",
                "rapl_ok",
                "misc_ok",
            )
        }
        if all(checks.values()):
            persisted = False
            if persist_desired:
                try:
                    await self._config.update(
                        lambda s: replace(
                            s,
                            power=PowerState(
                                profile=name,
                                auto_switch=s.power.auto_switch,
                                profiles=s.power.profiles,
                            ),
                        )
                    )
                    persisted = True
                except Exception as exc:  # noqa: BLE001
                    await self._refresh_power()
                    return OperationResult.partial(
                        code="power_applied_not_persisted",
                        message=f"Profile '{name}' applied, but could not be saved",
                        persisted=False,
                        applied=True,
                        sequence=self._snapshots.sequence,
                        details={**result, "persistence_error": str(exc)},
                    )

            refreshed = await self._refresh_power()
            if not refreshed:
                return OperationResult.partial(
                    code="power_observation_failed",
                    message=f"Profile '{name}' applied but could not be verified",
                    persisted=persisted,
                    applied=False,
                    sequence=self._snapshots.sequence,
                    details=result,
                )
            observed = self._snapshots.snapshot.power.observed_summary
            final_checks = verify_power_definition(observed, definition)
            if not all(final_checks.values()):
                return OperationResult.partial(
                    code="power_convergence_lost",
                    message=f"Profile '{name}' did not remain applied",
                    persisted=persisted,
                    applied=False,
                    sequence=self._snapshots.sequence,
                    details={
                        **result,
                        "final_verification": final_checks,
                        "final_observed": observed,
                    },
                )
            return OperationResult.success(
                message=f"Profile '{name}' applied",
                changed=True,
                persisted=persisted,
                applied=True,
                sequence=self._snapshots.sequence,
                details=result,
            )

        await self._refresh_power()
        return OperationResult.partial(
            code="power_partial_apply",
            message="Partial apply: "
            + ", ".join(
                f"{key.removesuffix('_ok')}={value}" for key, value in checks.items()
            ),
            persisted=False,
            applied=False,
            sequence=self._snapshots.sequence,
            details=result,
        )

    @_serialized_mutation
    async def set_auto_switch(self, enabled: bool) -> OperationResult:
        """Enable or disable AC/battery auto-profile switching."""
        await self._config.update(
            lambda s: replace(
                s,
                power=PowerState(
                    profile=s.power.profile,
                    auto_switch=PowerAutoSwitchState(
                        enabled=enabled,
                        on_ac=s.power.auto_switch.on_ac,
                        on_battery=s.power.auto_switch.on_battery,
                        on_ac_script=s.power.auto_switch.on_ac_script,
                        on_battery_script=s.power.auto_switch.on_battery_script,
                    ),
                    profiles=s.power.profiles,
                ),
            )
        )
        await self._supervisor.start("auto_switch")
        await self._refresh_power()
        return OperationResult.success(
            message=f"Auto-switch {'enabled' if enabled else 'disabled'}",
            changed=True,
            persisted=True,
            applied=False,
            sequence=self._snapshots.sequence,
        )

    @_serialized_mutation
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
        """Create or update a complete, typed power profile."""
        from honor_control.core.validation import validate_profile_name

        profile_name = validate_profile_name(name)
        definition = PowerProfileState(
            label=label.strip(),
            description=description.strip(),
            pl1_uw=int(pl1_uw),
            pl2_uw=int(pl2_uw),
            governor=governor.strip(),
            epp=epp.strip(),
            ppd_profile=ppd_profile.strip(),
            turbo_enabled=bool(turbo_enabled),
            max_perf_pct=int(max_perf_pct),
        )
        active = self._snapshots.snapshot.power.applied_profile == profile_name
        await self._config.update(
            lambda state: replace(
                state,
                power=PowerState(
                    profile=state.power.profile,
                    auto_switch=state.power.auto_switch,
                    profiles={**state.power.profiles, profile_name: definition},
                ),
            )
        )
        if active:
            result = await self._apply_power_profile(
                profile_name, persist_desired=False
            )
            if not result.applied:
                await self._refresh_power()
                return OperationResult.partial(
                    code="power_profile_saved_not_applied",
                    message=f"Profile '{profile_name}' was saved but not applied",
                    persisted=True,
                    applied=False,
                    sequence=self._snapshots.sequence,
                    details={"apply_result": result.to_dict()},
                )
        await self._refresh_power()
        return OperationResult.success(
            message=f"Profile '{profile_name}' saved"
            + (" and applied" if active else ""),
            changed=True,
            persisted=True,
            applied=active,
            sequence=self._snapshots.sequence,
        )

    @_serialized_mutation
    async def delete_power_profile(self, name: str) -> OperationResult:
        """Delete a custom profile that is not referenced by active policy."""
        profile_name = validate_power_profile(
            name, frozenset(self._config.state.power.profiles)
        )
        if profile_name in POWER_PROFILE_NAMES:
            raise DomainException(
                DomainError.INVALID_ARGUMENT,
                "The Silent, Balanced, and Performance defaults cannot be deleted",
            )
        power = self._config.state.power
        referenced = {
            power.profile,
            power.auto_switch.on_ac,
            power.auto_switch.on_battery,
        }
        if profile_name in referenced:
            raise DomainException(
                DomainError.BUSY,
                f"Profile '{profile_name}' is selected by current or automatic policy",
            )
        await self._config.update(
            lambda state: replace(
                state,
                power=replace(
                    state.power,
                    profiles={
                        key: value
                        for key, value in state.power.profiles.items()
                        if key != profile_name
                    },
                ),
                fan=replace(
                    state.fan,
                    curves={
                        key: value
                        for key, value in state.fan.curves.items()
                        if key != profile_name
                    },
                ),
            )
        )
        await self._refresh_power()
        await self._refresh_fan()
        return OperationResult.success(
            message=f"Profile '{profile_name}' deleted",
            changed=True,
            persisted=True,
            applied=False,
            sequence=self._snapshots.sequence,
        )

    @_serialized_mutation
    async def configure_auto_switch(
        self,
        enabled: bool,
        on_ac: str,
        on_battery: str,
        on_ac_script: str,
        on_battery_script: str,
    ) -> OperationResult:
        """Persist complete automatic-switch policy including optional hooks."""
        names = frozenset(self._config.state.power.profiles)
        ac_profile = validate_power_profile(on_ac, names)
        battery_profile = validate_power_profile(on_battery, names)
        self._validate_script_command(on_ac_script)
        self._validate_script_command(on_battery_script)
        await self._config.update(
            lambda state: replace(
                state,
                power=PowerState(
                    profile=state.power.profile,
                    profiles=state.power.profiles,
                    auto_switch=PowerAutoSwitchState(
                        enabled=bool(enabled),
                        on_ac=ac_profile,
                        on_battery=battery_profile,
                        on_ac_script=on_ac_script.strip(),
                        on_battery_script=on_battery_script.strip(),
                    ),
                ),
            )
        )
        await self._supervisor.start("auto_switch")
        await self._refresh_power()
        return OperationResult.success(
            message="Automatic switching configuration saved",
            changed=True,
            persisted=True,
            applied=False,
            sequence=self._snapshots.sequence,
        )

    # -- Fan --

    @_serialized_mutation
    async def set_fan_stock_auto(self) -> OperationResult:
        """Restore the EC's stock auto fan mode."""
        cap = await self._queue.run("fan_cap", self._hw.get_fan_capability)
        if not cap.writable:
            return OperationResult.unavailable(
                code="fan_unavailable",
                message=cap.message or "Fan control unavailable",
                sequence=self._snapshots.sequence,
            )
        ok = await self._queue.run("fan_auto", self._hw.set_fan_auto)
        if ok:
            self._fan_fail_safe_error = ""
            await self._supervisor.stop("manual_fan_ttl")
            # The explicit request above was verified; a redundant TTL cleanup
            # attempt cannot invalidate that successful transition.
            self._fan_fail_safe_error = ""
            await self._config.update(
                lambda s: replace(
                    s,
                    fan=FanState(mode="stock", curves=s.fan.curves),
                )
            )
            await self._refresh_fan()
            return OperationResult.success(
                message="Fan set to stock auto",
                changed=True,
                persisted=True,
                applied=True,
                sequence=self._snapshots.sequence,
            )
        return OperationResult.failed(
            code="fan_auto_failed",
            message="Failed to set fan to stock auto",
            sequence=self._snapshots.sequence,
        )

    @_serialized_mutation
    async def set_fan_curve(self, profile: str, curve_str: str) -> OperationResult:
        """Validate, save a fan curve, and apply if active."""
        from honor_control.core.validation import parse_curve

        points = parse_curve(curve_str)
        from honor_control.core.validation import format_curve

        curve_str = format_curve(points)
        profile = (
            "default"
            if profile == "default"
            else validate_power_profile(
                profile, frozenset(self._config.state.power.profiles)
            )
        )
        cap = await self._queue.run("fan_cap", self._hw.get_fan_capability)
        if not cap.writable:
            return OperationResult.unavailable(
                code="fan_unavailable",
                message=cap.message or "Fan control unavailable",
                sequence=self._snapshots.sequence,
            )

        await self._config.update(
            lambda s: replace(
                s,
                fan=FanState(
                    mode="curve",
                    curves={**s.fan.curves, profile: curve_str},
                ),
            )
        )
        await self._supervisor.start("fan_curve")
        await self._refresh_fan()
        return OperationResult.success(
            message=f"Curve saved for '{profile}'",
            changed=True,
            persisted=True,
            applied=False,  # applied only if controller transitions
            sequence=self._snapshots.sequence,
        )

    @_serialized_mutation
    async def set_fan_manual(self, speed: int, ttl_seconds: int) -> OperationResult:
        """Set a fixed fan speed with a required TTL."""
        from honor_control.core.validation import (
            MANUAL_EMERGENCY_MIN_SPEED,
            MANUAL_EMERGENCY_TEMP_MC,
            MANUAL_TTL_DEFAULT_SECONDS,
        )

        validated_speed = validate_manual_speed(speed)
        validated_ttl = validate_manual_ttl(ttl_seconds or MANUAL_TTL_DEFAULT_SECONDS)
        cap = await self._queue.run("fan_cap", self._hw.get_fan_capability)
        if not cap.writable:
            return OperationResult.unavailable(
                code="fan_unavailable",
                message=cap.message or "Fan control unavailable",
                sequence=self._snapshots.sequence,
            )
        temp = await self._queue.run("fan_manual_temp", self._hw.read_fan_temp)
        if temp is None or not 1_000 <= temp <= 150_000:
            return OperationResult.failed(
                code="fan_temperature_unavailable",
                message="Manual fan control requires a plausible live temperature",
                sequence=self._snapshots.sequence,
            )
        if temp >= 95_000 and validated_speed < 100:
            raise DomainException(
                DomainError.INVALID_ARGUMENT,
                "Manual fan speed must be 100% at or above 95°C",
            )
        if (
            temp >= MANUAL_EMERGENCY_TEMP_MC
            and validated_speed < MANUAL_EMERGENCY_MIN_SPEED
        ):
            raise DomainException(
                DomainError.INVALID_ARGUMENT,
                f"Manual fan speed must be at least {MANUAL_EMERGENCY_MIN_SPEED}% "
                f"at or above {MANUAL_EMERGENCY_TEMP_MC // 1000}°C",
            )
        await self._supervisor.stop("manual_fan_ttl")
        failure = ""
        try:
            applied = await self._apply_manual_fan_speed(
                validated_speed,
                command_prefix="fan_manual",
            )
        except CommandTimeoutError as exc:
            failure = str(exc)
            applied = False
        except Exception as exc:  # noqa: BLE001
            failure = str(exc)
            applied = False
        if applied:
            self._fan_fail_safe_error = ""
            await self._refresh_fan()
            await self._snapshots.update(
                "fan",
                replace(
                    self._snapshots.snapshot.fan,
                    mode=FanMode.MANUAL_OVERRIDE,
                    target_speed=validated_speed,
                    manual_expires_at=utc_now() + timedelta(seconds=validated_ttl),
                ),
            )
            self._manual_ttl_seconds = validated_ttl
            if not await self._supervisor.start("manual_fan_ttl"):
                restored = await self._restore_fan_auto()
                return OperationResult.partial(
                    code="fan_manual_watchdog_failed",
                    message=(
                        "Manual fan speed was set but its safety watchdog could not "
                        f"start; stock auto was {'restored' if restored else 'not restored'}"
                    ),
                    applied=False,
                    sequence=self._snapshots.sequence,
                )
            return OperationResult.success(
                message=f"Fan speed set to {validated_speed}% for {validated_ttl}s",
                changed=True,
                persisted=False,  # manual override is never persisted
                applied=True,
                sequence=self._snapshots.sequence,
                details={"ttl_seconds": validated_ttl},
            )
        if self._fan_restore_pending:
            return OperationResult.partial(
                code="fan_manual_restore_pending",
                message=(
                    "Manual fan control timed out; stock-auto restoration is queued "
                    "as the next hardware command"
                ),
                applied=False,
                sequence=self._snapshots.sequence,
                details={"error": failure},
            )
        if self._fan_fail_safe_error:
            return OperationResult.partial(
                code="fan_manual_restore_failed",
                message=(
                    "Manual fan control failed and stock-auto restoration could not "
                    "be verified"
                ),
                applied=False,
                sequence=self._snapshots.sequence,
                details={"error": failure},
            )
        return OperationResult.failed(
            code="fan_manual_failed",
            message=(
                "Failed to set manual fan speed; stock auto was restored"
                + (f": {failure}" if failure else "")
            ),
            sequence=self._snapshots.sequence,
        )

    async def _apply_manual_fan_speed(
        self,
        speed: int,
        *,
        command_prefix: str,
    ) -> bool:
        """Enter EC manual mode and write a speed with exception-safe cleanup."""
        try:
            manual_ok = await self._run_fan_command(
                command_prefix,
                self._hw.set_fan_manual,
                speed,
                recovery_context=f"{command_prefix} mode request",
            )
            if not manual_ok:
                await self._restore_fan_auto()
                return False
            speed_ok = await self._run_fan_command(
                f"{command_prefix}_speed",
                self._hw.set_fan_speed,
                speed,
                recovery_context=f"{command_prefix} speed request",
            )
            if not speed_ok:
                await self._restore_fan_auto()
                return False
            return True
        except CommandTimeoutError:
            raise
        except asyncio.CancelledError:
            if not self._fan_restore_pending:
                await asyncio.shield(self._restore_fan_auto())
            raise
        except Exception:
            await self._restore_fan_auto()
            raise

    async def _run_fan_command(
        self,
        name: str,
        func: Callable[..., Any],
        *args: Any,
        recovery_context: str,
    ) -> Any:
        """Run one fan call with stock-auto reserved after timeout/cancellation."""
        try:
            return await self._queue.run(
                name,
                func,
                *args,
                timeout_recovery=(f"{name}_auto_recovery", self._hw.set_fan_auto, ()),
            )
        except CommandTimeoutError as exc:
            self._track_fan_timeout_recovery(
                exc.recovery_future,
                context=f"{recovery_context} timed out",
            )
            raise
        except asyncio.CancelledError:
            self._track_fan_timeout_recovery(
                self._queue.pending_timeout_completion,
                context=f"{recovery_context} was cancelled",
            )
            raise

    # -- Gestures --

    @_serialized_mutation
    async def set_gesture_mapping(self, gesture_id: str, combo: str) -> OperationResult:
        """Persist a key combo consumed live by the service-owned runtime."""
        gid = validate_gesture_id(gesture_id)
        tokens = validate_key_combo(combo)
        normalized = ",".join(tokens)
        await self._config.update(
            lambda s: replace(
                s,
                gestures=GesturesState(
                    mappings={
                        **s.gestures.mappings,
                        gid: GestureMappingState(
                            enabled=s.gestures.mappings.get(
                                gid, GestureMappingState()
                            ).enabled,
                            mapping=normalized,
                        ),
                    },
                    daemon_enabled=s.gestures.daemon_enabled,
                ),
            )
        )
        await self._refresh_gestures()
        running = bool(self._gesture_runtime and self._gesture_runtime.status.running)
        return OperationResult.success(
            message=f"Mapping for '{gid}' updated",
            changed=True,
            persisted=True,
            applied=running,
            sequence=self._snapshots.sequence,
        )

    @_serialized_mutation
    async def set_gesture_enabled(
        self, gesture_id: str, enabled: bool
    ) -> OperationResult:
        """Persist daemon-level dispatch state, preserving the key mapping."""
        gid = validate_gesture_id(gesture_id)
        if (
            gid not in DEFAULT_GESTURE_MAPPINGS
            and gid not in self._config.state.gestures.mappings
        ):
            raise DomainException(
                DomainError.INVALID_ARGUMENT,
                f"Unknown gesture ID '{gid}'; set its mapping first",
            )
        await self._config.update(
            lambda s: replace(
                s,
                gestures=GesturesState(
                    mappings={
                        **s.gestures.mappings,
                        gid: GestureMappingState(
                            enabled=enabled,
                            mapping=s.gestures.mappings.get(
                                gid, GestureMappingState()
                            ).mapping,
                        ),
                    },
                    daemon_enabled=s.gestures.daemon_enabled,
                ),
            )
        )
        await self._refresh_gestures()
        running = bool(self._gesture_runtime and self._gesture_runtime.status.running)
        return OperationResult.success(
            message=f"Gesture '{gid}' {'enabled' if enabled else 'disabled'}",
            changed=True,
            persisted=True,
            applied=running,
            sequence=self._snapshots.sequence,
        )

    @_serialized_mutation
    async def set_all_gestures_enabled(self, enabled: bool) -> OperationResult:
        """Atomically enable or disable all gestures (one config transaction)."""
        await self._config.update(
            lambda s: replace(
                s,
                gestures=GesturesState(
                    mappings={
                        gid: GestureMappingState(
                            enabled=enabled,
                            mapping=s.gestures.mappings.get(
                                gid, GestureMappingState()
                            ).mapping,
                        )
                        for gid in dict.fromkeys(
                            [*DEFAULT_GESTURE_MAPPINGS, *s.gestures.mappings]
                        )
                    },
                    daemon_enabled=s.gestures.daemon_enabled,
                ),
            )
        )
        await self._refresh_gestures()
        running = bool(self._gesture_runtime and self._gesture_runtime.status.running)
        return OperationResult.success(
            message=f"All gestures {'enabled' if enabled else 'disabled'}",
            changed=True,
            persisted=True,
            applied=running,
            sequence=self._snapshots.sequence,
        )

    @_serialized_mutation
    async def set_gesture_daemon_enabled(self, enabled: bool) -> OperationResult:
        """Persist and reconcile the service-owned gesture reader."""
        if self._gesture_runtime is None:
            return OperationResult.unavailable(
                code="gesture_runtime_unavailable",
                message="Gesture runtime is not configured in this service mode",
                sequence=self._snapshots.sequence,
            )
        await self._config.update(
            lambda s: replace(
                s,
                gestures=GesturesState(
                    mappings=s.gestures.mappings,
                    daemon_enabled=enabled,
                ),
            )
        )
        await self._reconcile_gesture_runtime()
        await self._refresh_gestures()
        running = self._gesture_runtime.status.running
        if enabled and not running:
            probe = self._gesture_runtime.probe()
            return OperationResult.partial(
                code="gesture_runtime_not_ready",
                message=probe.error or self._gesture_runtime.status.last_error,
                persisted=True,
                applied=False,
                sequence=self._snapshots.sequence,
                details={"device_path": probe.device_path},
            )
        return OperationResult.success(
            message=f"Gesture daemon {'enabled' if enabled else 'disabled'}",
            changed=True,
            persisted=True,
            applied=True,
            sequence=self._snapshots.sequence,
        )

    # -- Touchpad firmware --

    async def probe_touchpad_firmware(self) -> dict[str, Any]:
        """Return a read-only, descriptor-verified firmware endpoint probe."""
        probe = await self._queue.run(
            "touchpad_probe", self._hw.probe_touchpad_firmware
        )
        return {
            "available": probe.available,
            "platform_verified": probe.platform_verified,
            "dmi_vendor": probe.dmi_vendor,
            "dmi_product": probe.dmi_product,
            "device_found": probe.device_found,
            "permission_denied": probe.permission_denied,
            "descriptor_verified": probe.descriptor_verified,
            "device_path": probe.device_path,
            "report_id": probe.report_id if probe.report_id is not None else -1,
            "input_report_bytes": probe.input_report_bytes,
            "output_report_bytes": probe.output_report_bytes,
            "error": probe.error,
        }

    @_serialized_mutation
    async def apply_touchpad_settings(
        self, settings: dict[str, int]
    ) -> OperationResult:
        """Validate and apply a profile, persisting only accepted writes."""
        if not isinstance(settings, dict) or not settings:
            raise DomainException(
                DomainError.INVALID_ARGUMENT,
                "Touchpad settings profile must be a non-empty dictionary",
            )
        normalized: dict[str, int] = {}
        try:
            for name, value in settings.items():
                if not isinstance(name, str):
                    raise ValueError("touchpad setting names must be strings")
                canonical = parse_touchpad_setting(name)
                if canonical.value in normalized:
                    raise ValueError(f"duplicate touchpad setting {canonical.value!r}")
                normalized[canonical.value] = parse_touchpad_value(canonical, value)
        except ValueError as exc:
            raise DomainException(DomainError.INVALID_ARGUMENT, str(exc)) from exc

        probe = await self._queue.run(
            "touchpad_probe", self._hw.probe_touchpad_firmware
        )
        if not probe.available:
            return OperationResult.unavailable(
                code="touchpad_firmware_unavailable",
                message=probe.error or "Touchpad firmware endpoint unavailable",
                sequence=self._snapshots.sequence,
            )
        try:
            result = await self._queue.run(
                "touchpad_apply", self._hw.apply_touchpad_settings, normalized
            )
        except TouchpadFirmwareError as exc:
            completed = {
                item.setting.value: item.value for item in exc.completed_settings
            }
            details = {
                "device_path": exc.device_path,
                "reports_applied": exc.reports_applied,
                "total_reports": exc.total_reports,
                "completed_settings": completed,
                "readback": "unavailable",
            }
            if exc.reports_applied:
                persisted = False
                if completed:
                    try:
                        await self._config.update(
                            lambda state: replace(
                                state,
                                touchpad=TouchpadState(
                                    settings={
                                        **state.touchpad.settings,
                                        **completed,
                                    }
                                ),
                            )
                        )
                        persisted = True
                    except Exception as persist_exc:  # noqa: BLE001
                        details["persistence_error"] = str(persist_exc)
                await self._refresh_gestures()
                return OperationResult.partial(
                    code="touchpad_partial_apply",
                    message=str(exc),
                    persisted=persisted,
                    applied=False,
                    sequence=self._snapshots.sequence,
                    details=details,
                )
            return OperationResult.failed(
                code="touchpad_apply_failed",
                message=str(exc),
                sequence=self._snapshots.sequence,
                details=details,
            )
        except Exception as exc:  # noqa: BLE001
            return OperationResult.failed(
                code="touchpad_apply_failed",
                message=str(exc),
                sequence=self._snapshots.sequence,
            )

        details = {
            "device_path": result.device_path,
            "settings": normalized,
            "reports_applied": result.reports_applied,
            "total_reports": result.total_reports,
            "clock_synchronized": result.clock_synchronized,
            "readback": "unavailable",
        }
        try:
            await self._config.update(
                lambda state: replace(
                    state,
                    touchpad=TouchpadState(
                        settings={**state.touchpad.settings, **normalized}
                    ),
                )
            )
        except Exception as exc:  # noqa: BLE001
            return OperationResult.partial(
                code="touchpad_applied_not_persisted",
                message="Touchpad settings were accepted but could not be saved",
                persisted=False,
                applied=True,
                sequence=self._snapshots.sequence,
                details={**details, "persistence_error": str(exc)},
            )
        await self._refresh_gestures()
        return OperationResult.success(
            message=f"Applied {len(normalized)} touchpad setting(s)",
            changed=True,
            persisted=True,
            applied=True,
            sequence=self._snapshots.sequence,
            details=details,
        )

    @_serialized_mutation
    async def set_touchpad_setting(self, setting: str, value: int) -> OperationResult:
        """Apply and persist one typed firmware setting."""
        return await self.apply_touchpad_settings.__wrapped__(self, {setting: value})

    @_serialized_mutation
    async def query_touchpad_support(self) -> dict[str, Any]:
        """Query capabilities while holding exclusive ownership of the reader."""
        restart_runtime = bool(
            self._gesture_runtime is not None
            and self._config.state.gestures.daemon_enabled
        )
        if self._gesture_runtime is not None:
            await self._supervisor.stop("gesture_daemon")
        try:
            bits = await self._queue.run(
                "touchpad_support", self._hw.query_touchpad_support
            )
        finally:
            if restart_runtime:
                await self._supervisor.start("gesture_daemon")
                await asyncio.sleep(0)
            await self._refresh_gestures()
        return {
            "supported_bits": sorted(bits),
            "known": {
                str(bit): SUPPORTED_GESTURE_BITS[bit]
                for bit in sorted(bits & SUPPORTED_GESTURE_BITS.keys())
            },
            "unknown_bits": sorted(bits - SUPPORTED_GESTURE_BITS.keys()),
        }

    # -- GPU --

    @_serialized_mutation
    async def set_gpu_mitigation_enabled(self, enabled: bool) -> OperationResult:
        """Apply or restore GPU IRQ mitigation."""
        cap = await self._queue.run("gpu_cap", self._hw.get_gpu_capability)
        if not cap.writable:
            return OperationResult.unavailable(
                code="gpu_unavailable",
                message=cap.message or "GPU mitigation unavailable",
                sequence=self._snapshots.sequence,
            )
        if enabled:
            result = await self._queue.run("gpu_apply", self._hw.apply_gpu_mitigation)
        else:
            result = await self._queue.run(
                "gpu_restore", self._hw.restore_gpu_mitigation
            )
        if result.get("error") and not result.get("restored"):
            return OperationResult.failed(
                code="gpu_mitigation_failed",
                message=str(result["error"]),
                sequence=self._snapshots.sequence,
                details=result,
            )
        await self._config.update(
            lambda s: replace(
                s,
                gpu=GpuState(mitigation_enabled=enabled),
            )
        )
        await self._refresh_gpu()
        return OperationResult.success(
            message=f"GPU mitigation {'enabled' if enabled else 'restored'}",
            changed=True,
            persisted=True,
            applied=True,
            sequence=self._snapshots.sequence,
            details=result,
        )

    # -- Diagnostics --

    async def run_checks(self) -> dict[str, Any]:
        """Run diagnostic checks and return results."""
        from honor_control.core.models import DiagnosticCheck, DiagnosticSeverity

        checks: list[DiagnosticCheck] = []
        caps = self._snapshots.snapshot.capabilities
        for name, cap in caps.items():
            severity = (
                DiagnosticSeverity.PASS
                if cap.status == CapabilityStatus.SUPPORTED
                else DiagnosticSeverity.SKIPPED
            )
            checks.append(
                DiagnosticCheck(
                    id=name,
                    severity=severity,
                    message=cap.message,
                    detail=cap.reason_code,
                )
            )
        dep_ok = self._hw.check_dependency()
        checks.append(
            DiagnosticCheck(
                id="honor_tools",
                severity=DiagnosticSeverity.PASS if dep_ok else DiagnosticSeverity.FAIL,
                message="honor-tools package importable"
                if dep_ok
                else "honor-tools not importable",
            )
        )
        overall = (
            "pass"
            if all(c.severity != DiagnosticSeverity.FAIL for c in checks)
            else "fail"
        )
        return {
            "overall": overall,
            "checks": [c.__dict__ if hasattr(c, "__dict__") else c for c in checks],
        }

    async def get_debug_bundle(self) -> dict[str, Any]:
        """Return a bounded, redacted JSON-serializable debug bundle."""
        snap = self._snapshots.snapshot
        return {
            "api_version": snap.api_version,
            "schema_version": snap.schema_version,
            "sequence": snap.sequence,
            "service": {
                "version": __version__,
                "uptime": int(time.time() - self._start_time),
                "health": self._supervisor.check_health(),
            },
            "platform": {
                "vendor": snap.platform.vendor,
                "model": snap.platform.model,
                "matched": snap.platform.matched,
            },
            "capabilities": {
                name: {"status": str(c.status), "reason": c.reason_code}
                for name, c in snap.capabilities.items()
            },
            "stale_domains": list(snap.stale_domains),
        }

    async def get_recent_logs(self, lines: int) -> list[str]:
        """Return recent backend log lines (bounded 1-500)."""
        count = validate_log_lines(lines)
        process: asyncio.subprocess.Process | None = None
        try:
            process = await asyncio.create_subprocess_exec(
                "journalctl",
                "-u",
                "honor-control.service",
                "-n",
                str(count),
                "--no-pager",
                "-o",
                "short-iso",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            stdout, _stderr = await asyncio.wait_for(process.communicate(), timeout=3)
            return stdout.decode("utf-8", errors="replace").splitlines()[-count:]
        except FileNotFoundError:
            return []
        except TimeoutError:
            if process is not None and process.returncode is None:
                process.kill()
                await process.wait()
            return []

    # -- Refresh helpers --

    def _power_reconcile_target(self) -> str:
        """Return the profile currently selected by manual/automatic policy."""
        power = self._config.state.power
        ac_online = self._snapshots.snapshot.power.ac_online
        if power.auto_switch.enabled and ac_online is not None:
            return power.auto_switch.on_ac if ac_online else power.auto_switch.on_battery
        return power.profile

    async def _reconcile_power_profile(
        self,
        desired: str | None = None,
    ) -> OperationResult | None:
        """Re-apply the persisted desired power profile on startup.

        Without this, a service restart (update, crash, reboot) leaves the
        hardware at the kernel/PPD default (balanced) even though the user's
        desired profile is e.g. performance.  The desired state is already
        persisted in config, so ``persist_desired=False`` is passed to avoid
        a redundant state write.  Failures are logged but never prevent
        service startup.
        """
        desired = desired or self._config.state.power.profile
        if not desired:
            return None
        cap = self._snapshots.snapshot.capabilities.get("power")
        if cap is None or not cap.writable:
            return None
        applied = self._snapshots.snapshot.power.applied_profile
        if applied == desired:
            return None
        log.info(
            "reconciling power profile: desired=%s applied=%s",
            desired,
            applied or "(unknown)",
        )
        try:
            result = await self._apply_power_profile(desired, persist_desired=False)
            if result.applied:
                log.info("power profile reconciled to '%s'", desired)
            else:
                log.warning("power profile reconciliation failed: %s", result.message)
            return result
        except Exception as exc:  # noqa: BLE001
            log.warning("power profile reconciliation error: %s", exc)
            return OperationResult.failed(
                code="power_reconcile_failed",
                message=str(exc),
                sequence=self._snapshots.sequence,
            )

    async def _refresh_all(self) -> None:
        """Refresh all domains and publish one snapshot."""
        await asyncio.gather(
            self._refresh_platform(),
            self._refresh_capabilities(),
            self._refresh_battery(),
            self._refresh_power(),
            self._refresh_fan(),
            self._refresh_gestures(),
            self._refresh_gpu(),
            self._refresh_service_health(),
            return_exceptions=True,
        )

    async def _refresh_platform(self) -> None:
        try:
            plat = await self._queue.run("platform", self._hw.detect_platform)
            await self._snapshots.update("platform", plat)
        except Exception as exc:  # noqa: BLE001
            await self._snapshots.mark_stale("platform", str(exc))

    async def _refresh_capabilities(self) -> None:
        try:
            if self._gesture_runtime is not None:
                gesture_capability = self._gesture_capability()
            else:
                gesture_capability = await self._queue.run(
                    "ges_cap", self._hw.get_gestures_capability
                )
            caps = {
                "battery": await self._queue.run(
                    "bat_cap", self._hw.get_battery_capability
                ),
                "power": await self._queue.run("pw_cap", self._hw.get_power_capability),
                "fan": await self._queue.run("fan_cap", self._hw.get_fan_capability),
                "gestures": gesture_capability,
                "gpu": await self._queue.run("gpu_cap", self._hw.get_gpu_capability),
            }
            await self._snapshots.update("capabilities", caps)
        except Exception as exc:  # noqa: BLE001
            await self._snapshots.mark_stale("capabilities", str(exc))

    async def _refresh_battery(self) -> None:
        try:
            bat = await self._queue.run("bat_read", self._hw.read_battery)
            desired = self._config.state.battery
            bat = replace(
                bat,
                desired_end=desired.end_threshold,
                desired_start=desired.start_threshold,
            )
            await self._snapshots.update("battery", bat)
        except Exception as exc:  # noqa: BLE001
            await self._snapshots.mark_stale("battery", str(exc))

    async def _refresh_power(self) -> bool:
        try:
            pw = await self._queue.run("pw_read", self._hw.read_power)
            desired = self._config.state.power
            ordered_names = [
                *[
                    name
                    for name in ("silent", "balanced", "performance")
                    if name in desired.profiles
                ],
                *sorted(
                    name for name in desired.profiles if name not in POWER_PROFILE_NAMES
                ),
            ]
            profiles = tuple(
                PowerProfileEntry(
                    name=name,
                    label=desired.profiles[name].label,
                    description=desired.profiles[name].description,
                    pl1_uw=desired.profiles[name].pl1_uw,
                    pl2_uw=desired.profiles[name].pl2_uw,
                    governor=desired.profiles[name].governor,
                    epp=desired.profiles[name].epp,
                    ppd_profile=desired.profiles[name].ppd_profile,
                    turbo_enabled=desired.profiles[name].turbo_enabled,
                    max_perf_pct=desired.profiles[name].max_perf_pct,
                    built_in=name in POWER_PROFILE_NAMES,
                )
                for name in ordered_names
            )
            matched_names = [
                name
                for name, profile in desired.profiles.items()
                if all(
                    verify_power_definition(
                        pw.observed_summary,
                        _power_definition(profile),
                    ).values()
                )
            ]
            if pw.applied_profile in matched_names:
                applied_profile = pw.applied_profile
            elif desired.profile in matched_names:
                applied_profile = desired.profile
            elif len(matched_names) == 1:
                applied_profile = matched_names[0]
            else:
                applied_profile = ""
            pw = replace(
                pw,
                desired_profile=desired.profile,
                applied_profile=applied_profile,
                auto_switch_enabled=desired.auto_switch.enabled,
                auto_switch_on_ac=desired.auto_switch.on_ac,
                auto_switch_on_battery=desired.auto_switch.on_battery,
                auto_switch_on_ac_script=desired.auto_switch.on_ac_script,
                auto_switch_on_battery_script=desired.auto_switch.on_battery_script,
                auto_switch_last_script_status=self._last_auto_script_status,
                profiles=profiles,
            )
            await self._snapshots.update("power", pw)
            return True
        except Exception as exc:  # noqa: BLE001
            await self._snapshots.mark_stale("power", str(exc))
            return False

    async def _refresh_fan(self) -> None:
        try:
            fan = await self._queue.run("fan_read", self._hw.read_fan)
            desired = self._config.state.fan
            current = self._snapshots.snapshot.fan
            manual_active = self._supervisor.get_health("manual_fan_ttl").running
            fan = replace(
                fan,
                mode=(
                    FanMode.FAILED_SAFE
                    if self._fan_fail_safe_error
                    else FanMode.MANUAL_OVERRIDE
                    if manual_active
                    else FanMode.CURVE
                    if desired.mode == "curve"
                    else fan.mode
                ),
                desired_mode=FanMode(desired.mode),
                curves=dict(desired.curves),
                target_speed=current.target_speed
                if manual_active or desired.mode == "curve"
                else fan.target_speed,
                manual_expires_at=current.manual_expires_at if manual_active else None,
                last_error=self._fan_fail_safe_error or fan.last_error,
            )
            await self._snapshots.update("fan", fan)
        except Exception as exc:  # noqa: BLE001
            await self._snapshots.mark_stale("fan", str(exc))

    async def _refresh_gestures(self) -> None:
        try:
            if self._gesture_runtime is not None:
                probe = self._gesture_runtime.probe()
                firmware_probe = await self._queue.run(
                    "touchpad_probe", self._hw.probe_touchpad_firmware
                )
                status = self._gesture_runtime.status
                ges = GesturesSnapshot(
                    available=probe.available,
                    daemon_enabled=self._config.state.gestures.daemon_enabled,
                    daemon_running=status.running,
                    device_found=status.device_found or probe.device_found,
                    permission_denied=(
                        status.permission_denied or probe.permission_denied
                    ),
                    device_path=status.device_path or probe.device_path,
                    reports_seen=status.reports_seen,
                    gestures_emitted=status.gestures_emitted,
                    mappings=self._gesture_entries(),
                    wmi_transport_present=wmi_transport_present(),
                    firmware_settings_supported=firmware_probe.available,
                    firmware_settings=dict(self._config.state.touchpad.settings),
                    last_error=status.last_error or probe.error,
                )
            else:
                ges = await self._queue.run("ges_read", self._hw.read_gestures)
                entries = await self._queue.run(
                    "ges_mappings", self._hw.list_gesture_mappings
                )
                ges = replace(
                    ges,
                    daemon_enabled=self._config.state.gestures.daemon_enabled,
                    mappings=self._gesture_entries(entries),
                    firmware_settings=dict(self._config.state.touchpad.settings),
                )
            await self._snapshots.update("gestures", ges)
        except Exception as exc:  # noqa: BLE001
            await self._snapshots.mark_stale("gestures", str(exc))

    def _gesture_entries(
        self, base_entries: list[GestureEntry] | None = None
    ) -> tuple[GestureEntry, ...]:
        """Build deterministic rows from confirmed defaults plus saved custom IDs."""
        base = {entry.id: entry for entry in (base_entries or [])}
        desired = self._config.state.gestures.mappings
        ids = dict.fromkeys([*DEFAULT_GESTURE_MAPPINGS, *base, *desired])
        entries: list[GestureEntry] = []
        for gid in ids:
            saved = desired.get(gid)
            fallback = base.get(gid)
            default = DEFAULT_GESTURE_MAPPINGS.get(
                gid, fallback.default_mapping if fallback else ""
            )
            entries.append(
                GestureEntry(
                    id=gid,
                    label=GESTURE_NAMES.get(
                        gid, fallback.label if fallback else f"Custom gesture {gid}"
                    ),
                    enabled=saved.enabled
                    if saved is not None
                    else fallback.enabled
                    if fallback is not None
                    else True,
                    mapping=saved.mapping
                    if saved is not None and saved.mapping
                    else fallback.mapping
                    if fallback is not None and fallback.mapping
                    else default,
                    default_mapping=default,
                )
            )
        return tuple(entries)

    def _gesture_capability(self) -> Capability:
        assert self._gesture_runtime is not None
        probe = self._gesture_runtime.probe()
        resources = tuple(path for path in (probe.device_path, "/dev/uinput") if path)
        if probe.available:
            return Capability(
                status=CapabilityStatus.SUPPORTED,
                message="HID gesture reports can be dispatched through uinput",
                resources=resources,
            )
        if probe.permission_denied:
            reason = "gesture_device_permission_denied"
        elif not probe.device_found:
            reason = "gesture_hidraw_missing"
        elif not probe.uinput_found:
            reason = "uinput_missing"
        else:
            reason = "gesture_runtime_unavailable"
        return Capability(
            status=CapabilityStatus.UNAVAILABLE,
            reason_code=reason,
            message=probe.error,
            resources=resources,
        )

    async def _reconcile_gesture_runtime(self) -> None:
        if self._gesture_runtime is None:
            return
        if self._config.state.gestures.daemon_enabled:
            await self._supervisor.start("gesture_daemon")
            # Let the task perform its synchronous device setup before snapshotting.
            await asyncio.sleep(0)
        else:
            await self._supervisor.stop("gesture_daemon")

    async def _refresh_gpu(self) -> None:
        try:
            gpu = await self._queue.run("gpu_read", self._hw.read_gpu)
            await self._snapshots.update("gpu", gpu)
        except Exception as exc:  # noqa: BLE001
            await self._snapshots.mark_stale("gpu", str(exc))

    async def _refresh_service_health(self) -> None:
        dependency_ok = self._hw.check_dependency()
        controller_health = self._supervisor.health
        controller_failed = any(
            health.state.value == "failed" for health in controller_health.values()
        )
        gesture_not_ready = bool(
            self._config.state.gestures.daemon_enabled
            and self._gesture_runtime is not None
            and not self._gesture_runtime.status.running
        )
        degraded = (
            not self._config.valid
            or not dependency_ok
            or bool(self._snapshots.snapshot.stale_domains)
            or controller_failed
            or gesture_not_ready
        )
        controller_fault = next(
            (
                health.last_fault
                for health in controller_health.values()
                if health.last_fault
            ),
            "",
        )
        health = ServiceHealth(
            version=__version__,
            uptime=int(time.time() - self._start_time),
            overall="degraded" if degraded else "healthy",
            controller_health=self._supervisor.check_health(),
            dependency_ok=dependency_ok,
            config_valid=self._config.valid,
            stale_domains=self._snapshots.snapshot.stale_domains,
            last_fault=self._config.last_error or controller_fault,
        )
        await self._snapshots.update("service", health)

    async def _refresh_loop(self) -> None:
        while True:
            await asyncio.sleep(5)
            await self._refresh_all()

    async def _auto_switch_loop(self) -> None:
        last_ac: bool | None = None
        last_policy: tuple[str, str, str, str] | None = None
        failed_key: tuple[bool, str, str, str, str] | None = None
        failure_count = 0
        retry_at = 0.0
        while True:
            await asyncio.sleep(AUTO_SWITCH_POLL_SECONDS)
            state = self._config.state.power.auto_switch
            if not state.enabled:
                last_ac = None
                last_policy = None
                failed_key = None
                failure_count = 0
                continue
            await self._refresh_power()
            ac = self._snapshots.snapshot.power.ac_online
            policy = (
                state.on_ac,
                state.on_battery,
                state.on_ac_script,
                state.on_battery_script,
            )
            if ac is None or (ac == last_ac and policy == last_policy):
                continue
            key = (ac, *policy)
            if key == failed_key and time.monotonic() < retry_at:
                continue
            async with self._mutation_lock:
                if self._closing or self._fan_restore_pending:
                    continue
                state = self._config.state.power.auto_switch
                if not state.enabled:
                    last_ac = None
                    last_policy = None
                    continue
                policy = (
                    state.on_ac,
                    state.on_battery,
                    state.on_ac_script,
                    state.on_battery_script,
                )
                profile = state.on_ac if ac else state.on_battery
                result = await self._apply_power_profile(
                    validate_power_profile(
                        profile, frozenset(self._config.state.power.profiles)
                    ),
                    persist_desired=False,
                )
                if result.applied:
                    last_ac = ac
                    last_policy = policy
                    failed_key = None
                    failure_count = 0
                    command = state.on_ac_script if ac else state.on_battery_script
                    self._last_auto_script_status = await self._run_transition_script(
                        command,
                        transition="ac" if ac else "battery",
                        profile=profile,
                    )
                    await self._refresh_power()
            if not result.applied:
                failure_count = failure_count + 1 if failed_key == key else 1
                failed_key = key
                delay = min(
                    AUTO_SWITCH_MAX_RETRY_SECONDS,
                    AUTO_SWITCH_POLL_SECONDS * (2 ** min(failure_count - 1, 10)),
                )
                retry_at = time.monotonic() + delay
                log.warning(
                    "auto-switch failed; retrying in %.0fs: %s",
                    delay,
                    result.message,
                )

    @staticmethod
    def _validate_script_command(command: str) -> list[str]:
        """Validate a direct argv command; shell syntax is never evaluated."""
        if not isinstance(command, str) or len(command) > 1024 or "\0" in command:
            raise DomainException(
                DomainError.INVALID_ARGUMENT, "Invalid script command"
            )
        if not command.strip():
            return []
        try:
            argv = shlex.split(command, posix=True)
        except ValueError as exc:
            raise DomainException(
                DomainError.INVALID_ARGUMENT, f"Invalid script arguments: {exc}"
            ) from exc
        if not argv or len(argv) > 32:
            raise DomainException(
                DomainError.INVALID_ARGUMENT, "Script must contain 1–32 arguments"
            )
        executable = pathlib.Path(argv[0])
        if not executable.is_absolute():
            raise DomainException(
                DomainError.INVALID_ARGUMENT,
                "Auto-switch script executable must use an absolute path",
            )
        if not executable.is_file() or not os.access(executable, os.X_OK):
            raise DomainException(
                DomainError.INVALID_ARGUMENT,
                f"Script executable is missing or not executable: {executable}",
            )
        resolved = executable.resolve()
        for path in (resolved, *resolved.parents):
            metadata = path.stat()
            if metadata.st_uid != 0 or metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
                raise DomainException(
                    DomainError.INVALID_ARGUMENT,
                    "Script and every parent directory must be root-owned and "
                    f"not group/world-writable: {path}",
                )
        argv[0] = str(resolved)
        return argv

    async def _run_transition_script(
        self, command: str, *, transition: str, profile: str
    ) -> str:
        """Run one configured hook without a shell, bounded to 15 seconds."""
        if not command.strip():
            return f"No {transition} script configured"
        try:
            argv = self._validate_script_command(command)
            process = await asyncio.create_subprocess_exec(
                *argv,
                cwd="/",
                env={
                    "PATH": "/usr/sbin:/usr/bin:/sbin:/bin",
                    "LANG": "C.UTF-8",
                    "HONOR_CONTROL_TRANSITION": transition,
                    "HONOR_CONTROL_PROFILE": profile,
                },
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                start_new_session=True,
            )
            try:
                await asyncio.wait_for(process.wait(), timeout=15)
            except TimeoutError:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                await process.wait()
                return f"{transition} script timed out"
            if process.returncode == 0:
                return f"{transition} script completed"
            return f"{transition} script failed ({process.returncode})"
        except Exception as exc:  # noqa: BLE001
            log.error("%s transition script failed: %s", transition, exc)
            return f"{transition} script error: {exc}"

    async def _fan_curve_loop(self) -> None:
        last_speed: int | None = None
        failures = 0
        while True:
            await asyncio.sleep(2)
            state = self._config.state.fan
            if state.mode != "curve":
                last_speed = None
                continue
            if self._supervisor.get_health("manual_fan_ttl").running:
                last_speed = None
                continue
            if self._fan_fail_safe_error or self._fan_restore_pending or self._closing:
                last_speed = None
                continue
            cap = self._snapshots.snapshot.capabilities.get("fan")
            if cap is None or not cap.writable:
                last_speed = None
                continue
            active_profile = (
                self._snapshots.snapshot.power.applied_profile
                or self._config.state.power.profile
            )
            curve = state.curves.get(active_profile) or state.curves.get("default")
            if not curve:
                continue
            try:
                async with self._mutation_lock:
                    # Re-check after waiting for an interactive mutation.
                    if (
                        self._config.state.fan.mode != "curve"
                        or self._supervisor.get_health("manual_fan_ttl").running
                        or bool(self._fan_fail_safe_error)
                        or self._fan_restore_pending
                        or self._closing
                    ):
                        continue
                    from honor_control.core.validation import parse_curve

                    temp = await self._run_fan_command(
                        "fan_temp",
                        self._hw.read_fan_temp,
                        recovery_context="fan curve temperature read",
                    )
                    if temp is None or not 1_000 <= temp <= 150_000:
                        raise RuntimeError(
                            f"fan temperature unavailable or implausible: {temp!r}"
                        )
                    points = parse_curve(curve)
                    speed = 100 if temp >= 95000 else _curve_speed(points, temp)
                    if (
                        last_speed is not None
                        and abs(speed - last_speed) < FAN_SPEED_WRITE_HYSTERESIS
                    ):
                        speed = last_speed
                    if speed != last_speed:
                        if not await self._apply_manual_fan_speed(
                            speed,
                            command_prefix="fan_curve",
                        ):
                            raise RuntimeError("EC rejected fan curve speed")
                        last_speed = speed
                failures = 0
                self._fan_fail_safe_error = ""
                await self._snapshots.update(
                    "fan",
                    replace(
                        self._snapshots.snapshot.fan,
                        available=True,
                        mode=FanMode.CURVE,
                        desired_mode=FanMode.CURVE,
                        temp_mc=temp,
                        target_speed=speed,
                    ),
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                failures += 1
                log.error("fan curve controller failure %d: %s", failures, exc)
                if failures >= 3:
                    async with self._mutation_lock:
                        await self._restore_fan_auto()
                        await self._config.update(
                            lambda current: replace(
                                current,
                                fan=FanState(
                                    mode="stock",
                                    curves=current.fan.curves,
                                ),
                            )
                        )
                        self._fan_fail_safe_error = (
                            "Fan curve disabled after repeated failures; "
                            "stock-auto restoration was requested"
                        )
                        await self._snapshots.update(
                            "fan",
                            replace(
                                self._snapshots.snapshot.fan,
                                mode=FanMode.FAILED_SAFE,
                                desired_mode=FanMode.STOCK,
                                target_speed=None,
                                manual_expires_at=None,
                                last_error=self._fan_fail_safe_error,
                            ),
                        )
                    raise RuntimeError(
                        "fan curve disabled after repeated failures"
                    ) from exc

    async def _manual_fan_expiry(self) -> None:
        from honor_control.core.validation import (
            MANUAL_EMERGENCY_MIN_SPEED,
            MANUAL_EMERGENCY_TEMP_MC,
        )

        deadline = asyncio.get_running_loop().time() + self._manual_ttl_seconds
        try:
            while True:
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    break
                await asyncio.sleep(min(2.0, remaining))
                async with self._mutation_lock:
                    if self._closing:
                        return
                    temp = await self._run_fan_command(
                        "fan_manual_watchdog_temp",
                        self._hw.read_fan_temp,
                        recovery_context="manual fan temperature watchdog",
                    )
                    if temp is None or not 1_000 <= temp <= 150_000:
                        if await self._restore_fan_auto():
                            await self._publish_fan_stock_auto()
                        return
                    target = self._snapshots.snapshot.fan.target_speed or 0
                    emergency_target = (
                        100
                        if temp >= 95_000
                        else MANUAL_EMERGENCY_MIN_SPEED
                        if temp >= MANUAL_EMERGENCY_TEMP_MC
                        else 0
                    )
                    if emergency_target > target:
                        ok = await self._run_fan_command(
                            "fan_manual_watchdog_speed",
                            self._hw.set_fan_speed,
                            emergency_target,
                            recovery_context="manual fan thermal watchdog",
                        )
                        if not ok:
                            if await self._restore_fan_auto():
                                await self._publish_fan_stock_auto()
                            return
                        await self._snapshots.update(
                            "fan",
                            replace(
                                self._snapshots.snapshot.fan,
                                temp_mc=temp,
                                target_speed=emergency_target,
                                last_error="Manual speed raised by thermal watchdog",
                            ),
                        )
            async with self._mutation_lock:
                if await self._restore_fan_auto():
                    await self._publish_fan_stock_auto()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("manual fan watchdog failed; restoring stock auto")
            async with self._mutation_lock:
                if await self._restore_fan_auto():
                    await self._publish_fan_stock_auto()
            raise

    def _track_fan_timeout_recovery(
        self,
        future: asyncio.Future[Any] | None,
        *,
        context: str,
    ) -> None:
        """Track the stock-auto command reserved behind a timed-out call."""
        if future is None:
            return
        current = self._fan_restore_task
        if current is not None and not current.done():
            return
        self._fan_restore_pending = True
        self._fan_fail_safe_error = f"{context}; stock-auto restoration pending"
        self._fan_restore_task = asyncio.create_task(
            self._complete_fan_timeout_recovery(future),
            name="fan-timeout-recovery",
        )

    async def _complete_fan_timeout_recovery(
        self,
        future: asyncio.Future[Any],
    ) -> bool:
        """Publish and observe a stock-auto recovery reserved by the queue."""
        try:
            await self._publish_fan_failed_safe(self._fan_fail_safe_error)
            restored = bool(await asyncio.shield(future))
            if not restored:
                raise RuntimeError("EC did not confirm queued stock-auto recovery")
            self._fan_fail_safe_error = ""
            if not self._closing:
                await self._refresh_fan()
            return True
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.exception("queued stock-auto recovery failed")
            await self._publish_fan_failed_safe(
                f"Queued stock-auto restoration failed: {exc}"
            )
            return False
        finally:
            self._fan_restore_pending = False

    async def _publish_fan_failed_safe(self, message: str) -> None:
        """Publish one explicit failed-safe fan state."""
        self._fan_fail_safe_error = message
        await self._snapshots.update(
            "fan",
            replace(
                self._snapshots.snapshot.fan,
                mode=FanMode.FAILED_SAFE,
                target_speed=None,
                manual_expires_at=None,
                last_error=message,
            ),
        )

    async def _publish_fan_stock_auto(self) -> None:
        """Publish a verified transition back to firmware-owned fan control."""
        await self._snapshots.update(
            "fan",
            replace(
                self._snapshots.snapshot.fan,
                mode=FanMode.STOCK,
                target_speed=None,
                manual_expires_at=None,
            ),
        )

    async def _restore_fan_auto(self) -> bool:
        pending = self._fan_restore_task
        if (
            pending is not None
            and not pending.done()
            and pending is not asyncio.current_task()
        ):
            try:
                return bool(
                    await asyncio.wait_for(
                        asyncio.shield(pending),
                        timeout=FAN_RECOVERY_WAIT_SECONDS,
                    )
                )
            except TimeoutError:
                await self._publish_fan_failed_safe(
                    "Stock-auto restoration is still pending behind a timed-out "
                    "hardware command"
                )
                return False
        try:
            restored = await self._queue.run(
                "fan_auto_cleanup",
                self._hw.set_fan_auto,
                timeout_recovery=(
                    "fan_auto_cleanup_retry",
                    self._hw.set_fan_auto,
                    (),
                ),
            )
            if not restored:
                raise RuntimeError("EC did not confirm stock-auto request")
            self._fan_fail_safe_error = ""
            await self._refresh_fan()
            return True
        except CommandTimeoutError as exc:
            self._track_fan_timeout_recovery(
                exc.recovery_future,
                context="stock-auto restoration timed out",
            )
            await self._publish_fan_failed_safe(self._fan_fail_safe_error)
            return False
        except Exception as exc:  # noqa: BLE001
            log.exception("failed to restore stock fan mode")
            await self._publish_fan_failed_safe(
                f"Stock-auto restoration failed: {exc}"
            )
            return False

    async def _initialize_fan_safety(self) -> None:
        """Request firmware-owned fan control on startup when writes are verified."""
        cap = self._snapshots.snapshot.capabilities.get("fan")
        if cap is None or not cap.writable:
            return
        await self._restore_fan_auto()


def _curve_speed(points, temp_mc: int) -> int:
    if temp_mc <= points[0].temp_mc:
        return points[0].speed
    for left, right in zip(points, points[1:], strict=False):
        if temp_mc <= right.temp_mc:
            span = right.temp_mc - left.temp_mc
            offset = temp_mc - left.temp_mc
            return round(left.speed + (right.speed - left.speed) * offset / span)
    return points[-1].speed
