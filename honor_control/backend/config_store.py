"""Versioned system state store.

State split:

* ``/var/lib/honor-control/state.toml`` — service-owned desired state with
  ``schema_version``.
* ``QSettings`` under the user's account — window geometry, refresh
  preference, notification preference only.

The store loads into a fresh immutable model, fully validates, then swaps
the active snapshot.  A malformed file leaves the last-known-good state
active and marks service health degraded.  Saves are atomic: write a
same-directory temp file, ``flush`` + ``fsync``, mode ``0640``,
``os.replace``, then fsync the directory.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import pathlib
import re
import shutil
import tomllib
from dataclasses import asdict, dataclass, field
from typing import Any

from honor_control.core.errors import DomainError, DomainException
from honor_control.core.models import POWER_PROFILES
from honor_control.core.touchpad import parse_touchpad_setting, parse_touchpad_value
from honor_control.core.validation import (
    parse_curve,
    validate_fan_mode,
    validate_gesture_id,
    validate_key_combo,
    validate_power_profile,
    validate_thresholds,
)

log = logging.getLogger("honor_control.backend.config_store")

#: Current state schema version.
STATE_SCHEMA_VERSION = 3

#: Default state file path.
DEFAULT_STATE_PATH = "/var/lib/honor-control/state.toml"


@dataclass(frozen=True)
class BatteryState:
    """Desired battery thresholds/mode."""

    end_threshold: int = 90
    start_threshold: int = 85
    mode: str = "home"


@dataclass(frozen=True)
class PowerAutoSwitchState:
    """Desired AC auto-switch policy."""

    enabled: bool = False
    on_ac: str = "balanced"
    on_battery: str = "silent"
    on_ac_script: str = ""
    on_battery_script: str = ""


@dataclass(frozen=True)
class PowerProfileState:
    """One editable power profile persisted by the system service."""

    label: str = "Custom"
    description: str = ""
    pl1_uw: int = 25_000_000
    pl2_uw: int = 35_000_000
    governor: str = "powersave"
    epp: str = "balance_power"
    ppd_profile: str = "balanced"
    turbo_enabled: bool = True
    max_perf_pct: int = 100


def default_power_profiles() -> dict[str, PowerProfileState]:
    return {
        entry.name: PowerProfileState(
            label=entry.label,
            description=entry.description,
            pl1_uw=entry.pl1_uw,
            pl2_uw=entry.pl2_uw,
            governor=entry.governor,
            epp=entry.epp,
            ppd_profile=entry.ppd_profile,
            turbo_enabled=entry.turbo_enabled,
            max_perf_pct=entry.max_perf_pct,
        )
        for entry in POWER_PROFILES
    }


@dataclass(frozen=True)
class PowerState:
    """Desired power profile and auto-switch policy."""

    profile: str = "balanced"
    auto_switch: PowerAutoSwitchState = field(default_factory=PowerAutoSwitchState)
    profiles: dict[str, PowerProfileState] = field(
        default_factory=default_power_profiles
    )


@dataclass(frozen=True)
class FanState:
    """Desired fan mode/curves."""

    mode: str = "stock"
    curves: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class GestureMappingState:
    """A single gesture's desired mapping."""

    enabled: bool = True
    mapping: str = ""


@dataclass(frozen=True)
class GesturesState:
    """Desired gesture mappings/enabled state and daemon enablement."""

    mappings: dict[str, GestureMappingState] = field(default_factory=dict)
    daemon_enabled: bool = False


@dataclass(frozen=True)
class TouchpadState:
    """Desired firmware settings accepted by the touchpad transport."""

    settings: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class GpuState:
    """Desired GPU mitigation enablement."""

    mitigation_enabled: bool = False


@dataclass(frozen=True)
class ServiceState:
    """The complete desired state persisted to disk."""

    schema_version: int = STATE_SCHEMA_VERSION
    battery: BatteryState = field(default_factory=BatteryState)
    power: PowerState = field(default_factory=PowerState)
    fan: FanState = field(default_factory=FanState)
    gestures: GesturesState = field(default_factory=GesturesState)
    touchpad: TouchpadState = field(default_factory=TouchpadState)
    gpu: GpuState = field(default_factory=GpuState)


def default_state() -> ServiceState:
    """Return the default service state."""
    return ServiceState()


def _state_to_dict(state: ServiceState) -> dict[str, Any]:
    """Convert ServiceState to a TOML-serializable dict."""
    d = asdict(state)
    d["schema_version"] = STATE_SCHEMA_VERSION
    return d


def _state_from_dict(data: dict[str, Any]) -> ServiceState:
    """Parse a dict into a validated ServiceState.

    Unknown keys are ignored within the current schema. Invalid values raise
    :class:`DomainException`.
    """
    if not isinstance(data, dict):
        raise DomainException(
            DomainError.INVALID_ARGUMENT,
            "State document must be a TOML table",
        )

    def table(parent: dict[str, Any], key: str) -> dict[str, Any]:
        value = parent.get(key, {})
        if not isinstance(value, dict):
            raise DomainException(
                DomainError.INVALID_ARGUMENT,
                f"'{key}' must be a table",
            )
        return value

    def integer(parent: dict[str, Any], key: str, default: int) -> int:
        value = parent.get(key, default)
        if isinstance(value, bool) or not isinstance(value, int):
            raise DomainException(
                DomainError.INVALID_ARGUMENT,
                f"'{key}' must be an integer",
            )
        return value

    def boolean(parent: dict[str, Any], key: str, default: bool) -> bool:
        value = parent.get(key, default)
        if not isinstance(value, bool):
            raise DomainException(
                DomainError.INVALID_ARGUMENT,
                f"'{key}' must be a boolean",
            )
        return value

    def string(parent: dict[str, Any], key: str, default: str) -> str:
        value = parent.get(key, default)
        if not isinstance(value, str):
            raise DomainException(
                DomainError.INVALID_ARGUMENT,
                f"'{key}' must be a string",
            )
        return value

    schema_version = data.get("schema_version", 1)
    if (
        isinstance(schema_version, bool)
        or not isinstance(schema_version, int)
        or schema_version < 1
    ):
        raise DomainException(
            DomainError.INVALID_ARGUMENT,
            f"Invalid schema_version: {schema_version!r}",
        )
    if schema_version > STATE_SCHEMA_VERSION:
        raise DomainException(
            DomainError.UNSUPPORTED,
            f"State schema {schema_version} is newer than supported schema "
            f"{STATE_SCHEMA_VERSION}",
        )
    # Future: apply ordered migration functions for old schema versions.

    # Battery
    bat_data = table(data, "battery")
    end = integer(bat_data, "end_threshold", 90)
    start = integer(bat_data, "start_threshold", 85)
    validate_thresholds(end, start)
    battery = BatteryState(
        end_threshold=end,
        start_threshold=start,
        mode=string(bat_data, "mode", "home"),
    )

    # Power
    pw_data = table(data, "power")
    as_data = table(pw_data, "auto_switch")
    raw_profiles = table(pw_data, "profiles")
    profiles = default_power_profiles()
    for name, profile_data in raw_profiles.items():
        if not isinstance(name, str) or not isinstance(profile_data, dict):
            raise DomainException(
                DomainError.INVALID_ARGUMENT,
                "Power profiles must be tables keyed by profile name",
            )
        profiles[name] = PowerProfileState(
            label=string(profile_data, "label", name.replace("_", " ").title()),
            description=string(profile_data, "description", ""),
            pl1_uw=integer(profile_data, "pl1_uw", 25_000_000),
            pl2_uw=integer(profile_data, "pl2_uw", 35_000_000),
            governor=string(profile_data, "governor", "powersave"),
            epp=string(profile_data, "epp", "balance_power"),
            ppd_profile=string(profile_data, "ppd_profile", "balanced"),
            turbo_enabled=boolean(profile_data, "turbo_enabled", True),
            max_perf_pct=integer(profile_data, "max_perf_pct", 100),
        )
    power = PowerState(
        profile=string(pw_data, "profile", "balanced"),
        auto_switch=PowerAutoSwitchState(
            enabled=boolean(as_data, "enabled", False),
            on_ac=string(as_data, "on_ac", "balanced"),
            on_battery=string(as_data, "on_battery", "silent"),
            on_ac_script=string(as_data, "on_ac_script", ""),
            on_battery_script=string(as_data, "on_battery_script", ""),
        ),
        profiles=profiles,
    )

    # Fan
    fan_data = table(data, "fan")
    raw_curves = table(fan_data, "curves")
    curves: dict[str, str] = {}
    for key, value in raw_curves.items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise DomainException(
                DomainError.INVALID_ARGUMENT,
                "Fan curve names and values must be strings",
            )
        curves[key] = value
    fan = FanState(
        mode=string(fan_data, "mode", "stock"),
        curves=curves,
    )

    # Gestures
    ges_data = table(data, "gestures")
    raw_mappings = table(ges_data, "mappings")
    mappings: dict[str, GestureMappingState] = {}
    for gid, mdata in raw_mappings.items():
        if not isinstance(gid, str) or not isinstance(mdata, dict):
            raise DomainException(
                DomainError.INVALID_ARGUMENT,
                "Gesture mappings must be tables keyed by gesture ID",
            )
        mappings[gid] = GestureMappingState(
            enabled=boolean(mdata, "enabled", True),
            mapping=string(mdata, "mapping", ""),
        )
    gestures = GesturesState(
        mappings=mappings,
        daemon_enabled=boolean(ges_data, "daemon_enabled", False),
    )

    # Touchpad firmware desired state (introduced in schema 3).
    touchpad_data = table(data, "touchpad")
    raw_touchpad_settings = table(touchpad_data, "settings")
    touchpad_settings: dict[str, int] = {}
    for name, value in raw_touchpad_settings.items():
        if not isinstance(name, str):
            raise DomainException(
                DomainError.INVALID_ARGUMENT,
                "Touchpad setting names must be strings",
            )
        try:
            canonical = parse_touchpad_setting(name)
            touchpad_settings[canonical.value] = parse_touchpad_value(canonical, value)
        except ValueError as exc:
            raise DomainException(DomainError.INVALID_ARGUMENT, str(exc)) from exc
    touchpad = TouchpadState(settings=touchpad_settings)

    # GPU
    gpu_data = table(data, "gpu")
    gpu = GpuState(
        mitigation_enabled=boolean(gpu_data, "mitigation_enabled", False),
    )

    state = ServiceState(
        schema_version=STATE_SCHEMA_VERSION,
        battery=battery,
        power=power,
        fan=fan,
        gestures=gestures,
        touchpad=touchpad,
        gpu=gpu,
    )
    _validate_state(state)
    return state


class ConfigStore:
    """Atomic, versioned, serialized state store.

    All mutations go through :meth:`update` which acquires an async lock,
    applies a pure mutator function, validates the result, and saves
    atomically.  Concurrent updates preserve both changes because each
    mutator receives the latest snapshot.
    """

    def __init__(
        self,
        state_path: str | pathlib.Path = DEFAULT_STATE_PATH,
    ) -> None:
        self._state_path = pathlib.Path(state_path)
        self._lock = asyncio.Lock()
        self._state: ServiceState = default_state()
        self._valid = True
        self._last_error = ""

    @property
    def state(self) -> ServiceState:
        """Return the current (immutable) desired state."""
        return self._state

    @property
    def valid(self) -> bool:
        """True when the last load/save succeeded."""
        return self._valid

    @property
    def last_error(self) -> str:
        """Return the last load/save error (empty when valid)."""
        return self._last_error

    @property
    def state_path(self) -> pathlib.Path:
        """Return the state file path."""
        return self._state_path

    def load(self) -> ServiceState:
        """Load state from disk into the active snapshot.

        A missing file defaults to the default state.  A corrupt file
        leaves the last-known-good state active and marks the store
        degraded.
        """
        try:
            if not self._state_path.exists():
                log.info("state file %s missing; using defaults", self._state_path)
                self._state = default_state()
                self._valid = True
                self._last_error = ""
                return self._state
            data = self._read_toml(self._state_path)
            self._state = _state_from_dict(data)
            self._valid = True
            self._last_error = ""
            log.info("loaded state from %s", self._state_path)
        except Exception as exc:  # noqa: BLE001
            log.error("state load failed: %s", exc)
            self._valid = False
            self._last_error = str(exc)
        return self._state

    def reload(self) -> ServiceState:
        """Reload state from disk (same as :meth:`load`)."""
        return self.load()

    async def update(self, mutator) -> ServiceState:
        """Apply ``mutator(current_state) -> new_state`` atomically.

        The mutator is a pure function that receives the current
        :class:`ServiceState` and returns a replacement.  The result is
        validated before saving.  On failure the old state is retained.
        """
        async with self._lock:
            new_state = mutator(self._state)
            if not isinstance(new_state, ServiceState):
                raise DomainException(
                    DomainError.INTERNAL,
                    "Mutator did not return a ServiceState",
                )
            _validate_state(new_state)
            try:
                self._save_atomic(new_state)
            except DomainException:
                raise
            except Exception as exc:  # noqa: BLE001
                self._valid = False
                self._last_error = str(exc)
                raise DomainException(
                    DomainError.INTERNAL,
                    "Failed to persist service state",
                    detail=str(exc),
                ) from exc
            self._state = new_state
            return self._state

    def _save_atomic(self, state: ServiceState) -> None:
        """Write state atomically: temp + fsync + rename + dir fsync."""
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        data = _state_to_dict(state)
        # Keep one bounded .bak of the last valid file.
        if self._state_path.exists():
            bak = self._state_path.with_suffix(".toml.bak")
            try:
                shutil.copy2(self._state_path, bak)
            except OSError:
                pass  # non-fatal
        tmp = self._state_path.with_suffix(".toml.tmp")
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o640)
        try:
            # The service unit uses a restrictive umask; enforce the documented
            # state-file mode after creation rather than inheriting 0600.
            os.fchmod(fd, 0o640)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(_toml_dump(data))
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(str(tmp), str(self._state_path))
            self._fsync_dir(self._state_path.parent)
        except Exception:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            self._valid = False
            self._last_error = "Atomic state save failed"
            raise
        self._valid = True
        self._last_error = ""

    @staticmethod
    def _read_toml(path: pathlib.Path) -> dict[str, Any]:
        """Read and parse a TOML file."""
        with open(path, "rb") as fh:
            return tomllib.load(fh)

    @staticmethod
    def _fsync_dir(path: pathlib.Path) -> None:
        """Best-effort fsync of the directory containing the state file."""
        fd = os.open(str(path), os.O_RDONLY)
        try:
            os.fsync(fd)
        except OSError:
            pass  # not all filesystems support directory fsync
        finally:
            os.close(fd)

    def export_state_dict(self) -> dict[str, Any]:
        """Return the current state as a serializable dict (for debug/import)."""
        return _state_to_dict(self._state)

    def import_state_dict(self, data: dict[str, Any]) -> ServiceState:
        """Validate and adopt a state dict (used by the import command)."""
        state = _state_from_dict(data)
        _validate_state(state)
        try:
            self._save_atomic(state)
        except Exception as exc:  # noqa: BLE001
            self._valid = False
            self._last_error = str(exc)
            raise DomainException(
                DomainError.INTERNAL,
                "Failed to import service state",
                detail=str(exc),
            ) from exc
        self._state = state
        return state


def _validate_state(state: ServiceState) -> None:
    """Validate a ServiceState before persisting.

    Raises :class:`DomainException` if any field is out of range.
    """
    if state.schema_version != STATE_SCHEMA_VERSION:
        raise DomainException(
            DomainError.INVALID_ARGUMENT,
            f"Expected state schema {STATE_SCHEMA_VERSION}, got "
            f"{state.schema_version!r}",
        )
    validate_thresholds(state.battery.end_threshold, state.battery.start_threshold)
    if state.battery.mode not in {"off", "home", "travel", "storage", "custom"}:
        raise DomainException(
            DomainError.INVALID_ARGUMENT,
            f"Unknown battery mode '{state.battery.mode}'",
        )
    profile_names = frozenset(state.power.profiles)
    if not profile_names:
        raise DomainException(
            DomainError.INVALID_ARGUMENT, "At least one profile is required"
        )
    for name, profile in state.power.profiles.items():
        # Syntax is checked against the complete dynamic registry.
        validate_power_profile(name, profile_names)
        if not profile.label.strip() or len(profile.label) > 64:
            raise DomainException(
                DomainError.INVALID_ARGUMENT, f"Invalid label for profile '{name}'"
            )
        if len(profile.description) > 256:
            raise DomainException(
                DomainError.INVALID_ARGUMENT,
                f"Description is too long for profile '{name}'",
            )
        if not 3_000_000 <= profile.pl1_uw <= 100_000_000:
            raise DomainException(
                DomainError.INVALID_ARGUMENT, f"PL1 out of range for profile '{name}'"
            )
        if not profile.pl1_uw <= profile.pl2_uw <= 150_000_000:
            raise DomainException(
                DomainError.INVALID_ARGUMENT, f"PL2 out of range for profile '{name}'"
            )
        if profile.governor not in {"powersave", "performance"}:
            raise DomainException(
                DomainError.INVALID_ARGUMENT, f"Invalid governor for profile '{name}'"
            )
        if profile.epp not in {
            "power",
            "default",
            "balance_power",
            "balance_performance",
            "performance",
        }:
            raise DomainException(
                DomainError.INVALID_ARGUMENT, f"Invalid EPP for profile '{name}'"
            )
        if profile.ppd_profile not in {"power-saver", "balanced", "performance"}:
            raise DomainException(
                DomainError.INVALID_ARGUMENT, f"Invalid PPD profile for '{name}'"
            )
        if not isinstance(profile.turbo_enabled, bool):
            raise DomainException(
                DomainError.INVALID_ARGUMENT, f"Invalid turbo setting for '{name}'"
            )
        if not 1 <= profile.max_perf_pct <= 100:
            raise DomainException(
                DomainError.INVALID_ARGUMENT,
                f"Max performance percentage out of range for '{name}'",
            )
    validate_power_profile(state.power.profile, profile_names)
    if not isinstance(state.power.auto_switch.enabled, bool):
        raise DomainException(
            DomainError.INVALID_ARGUMENT,
            "Power auto-switch enabled flag must be a boolean",
        )
    validate_power_profile(state.power.auto_switch.on_ac, profile_names)
    validate_power_profile(state.power.auto_switch.on_battery, profile_names)
    for script in (
        state.power.auto_switch.on_ac_script,
        state.power.auto_switch.on_battery_script,
    ):
        if not isinstance(script, str) or len(script) > 1024 or "\0" in script:
            raise DomainException(
                DomainError.INVALID_ARGUMENT, "Invalid auto-switch script"
            )
    validate_fan_mode(state.fan.mode)
    for profile, curve in state.fan.curves.items():
        if profile != "default":
            validate_power_profile(profile, profile_names)
        parse_curve(curve)
    for gesture_id, mapping in state.gestures.mappings.items():
        validate_gesture_id(gesture_id)
        if not isinstance(mapping.enabled, bool):
            raise DomainException(
                DomainError.INVALID_ARGUMENT,
                f"Gesture '{gesture_id}' enabled flag must be a boolean",
            )
        if mapping.mapping:
            validate_key_combo(mapping.mapping)
    if not isinstance(state.gestures.daemon_enabled, bool):
        raise DomainException(
            DomainError.INVALID_ARGUMENT,
            "Gesture daemon enabled flag must be a boolean",
        )
    for name, value in state.touchpad.settings.items():
        try:
            canonical = parse_touchpad_setting(name)
            if canonical.value != name:
                raise ValueError(f"touchpad setting {name!r} is not canonical")
            parse_touchpad_value(canonical, value)
        except ValueError as exc:
            raise DomainException(DomainError.INVALID_ARGUMENT, str(exc)) from exc
    if not isinstance(state.gpu.mitigation_enabled, bool):
        raise DomainException(
            DomainError.INVALID_ARGUMENT,
            "GPU mitigation enabled flag must be a boolean",
        )


_BARE_TOML_KEY = re.compile(r"^[A-Za-z0-9_-]+$")


def _toml_key(value: str) -> str:
    """Return a valid TOML key segment."""
    return value if _BARE_TOML_KEY.fullmatch(value) else json.dumps(value)


def _toml_value(value: Any) -> str:
    """Serialize one TOML scalar or array, rejecting unsupported values."""
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("Non-finite floats are not supported in state TOML")
        return repr(value)
    if isinstance(value, str):
        return json.dumps(value)
    if isinstance(value, list):
        return "[" + ", ".join(_toml_value(item) for item in value) + "]"
    raise TypeError(f"Unsupported TOML value type: {type(value).__name__}")


def _toml_dump(data: dict[str, Any]) -> str:
    """Serialize a dict to TOML (minimal, no external dependency).

    Handles nested dicts, strings, ints, floats, and bools.  This is a
    small, tested serializer sufficient for the state file format.
    """
    if not isinstance(data, dict):
        raise TypeError("TOML document root must be a dict")
    lines: list[str] = []

    def _dump_table(obj: dict[str, Any], path: tuple[str, ...]) -> None:
        if path:
            if lines and lines[-1] != "":
                lines.append("")
            lines.append("[" + ".".join(_toml_key(part) for part in path) + "]")

        child_tables: list[tuple[str, dict[str, Any]]] = []
        for key, value in obj.items():
            if not isinstance(key, str):
                raise TypeError("TOML keys must be strings")
            if isinstance(value, dict):
                child_tables.append((key, value))
            else:
                lines.append(f"{_toml_key(key)} = {_toml_value(value)}")

        for key, child in child_tables:
            _dump_table(child, (*path, key))

    _dump_table(data, ())
    return "\n".join(lines) + "\n"
