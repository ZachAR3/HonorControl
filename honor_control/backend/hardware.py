"""Hardware port: isolates all ``honor-tools`` imports behind one adapter.

No other module may ``import honor``.  Tests use :class:`FakeHardware`;
production uses :class:`HonorToolsAdapter`.

Key safety invariants:
  * Unknown/unsupported hardware never performs writes.
  * Empty/missing reads become ``Unavailable``, not numeric zero.
  * Battery/AC/hwmon paths are discovered, not hard-coded to BAT0/ADP1.
  * No private ``honor.*`` imports; no ``_patch_acpi_call`` monkeypatch.
  * No ``sudo`` subprocess.
"""

from __future__ import annotations

import logging
import os
import pathlib
import shutil
import subprocess
import time
from collections.abc import Mapping
from typing import Any, Protocol

from honor_control.backend.gesture_runtime import (
    probe_gesture_environment,
    wmi_transport_present,
)
from honor_control.backend.touchpad_firmware import (
    TouchpadBatchApplyResult,
    TouchpadFirmwareProbe,
    TouchpadFirmwareTransport,
    probe_touchpad_firmware,
)
from honor_control.core.errors import CapabilityStatus, DomainError, DomainException
from honor_control.core.models import (
    BatterySnapshot,
    BatteryStatusKind,
    Capability,
    FanMode,
    FanSnapshot,
    GestureEntry,
    GesturesSnapshot,
    GpuSnapshot,
    PlatformInfo,
    PowerSnapshot,
)

log = logging.getLogger("honor_control.backend.hardware")

# Fan EC commands are hardware-specific. This is the only DMI/CPU identity
# verified by this repository's captures and the live target machine.
_SUPPORTED_FAN_IDENTITIES: dict[str, tuple[str, ...]] = {
    "mra-xxx": (
        "core(tm) ultra 5 125h",
        "core(tm) ultra 7 155h",
        "core(tm) ultra 9 185h",
        "meteor lake",
    ),
}

_CPU_ROOT = "/sys/devices/system/cpu"
_INTEL_PSTATE_ROOT = f"{_CPU_ROOT}/intel_pstate"
_RAPL_TREES = (
    "/sys/class/powercap/intel-rapl:0",
    "/sys/class/powercap/intel-rapl-mmio:0",
)
_RAPL_LIMIT_FILES = (
    "constraint_0_power_limit_uw",
    "constraint_1_power_limit_uw",
)
PPD_SETTLE_SECONDS = 0.5
_POWER_CHECK_KEYS = (
    "governor_ok",
    "epp_ok",
    "ppd_ok",
    "rapl_ok",
    "misc_ok",
)
_POWER_DEFINITION_FIELDS = frozenset(
    {
        "pl1_uw",
        "pl2_uw",
        "governor",
        "epp",
        "ppd_profile",
        "turbo_enabled",
        "max_perf_pct",
    }
)


def _all_true(value: Any) -> bool:
    """Return whether a nested result contains only successful leaves."""
    if isinstance(value, dict):
        return bool(value) and all(_all_true(item) for item in value.values())
    return value is True


def verify_power_definition(
    observed: dict[str, Any], definition: dict[str, Any]
) -> dict[str, bool]:
    """Compare every supported power-profile field with live observation."""
    if not _POWER_DEFINITION_FIELDS.issubset(definition):
        return dict.fromkeys(_POWER_CHECK_KEYS, False)
    rapl = observed.get("rapl")
    rapl_ok = isinstance(rapl, dict) and bool(rapl)
    if rapl_ok:
        rapl_ok = all(
            isinstance(values, dict)
            and values.get(_RAPL_LIMIT_FILES[0]) == definition.get("pl1_uw")
            and values.get(_RAPL_LIMIT_FILES[1]) == definition.get("pl2_uw")
            for values in rapl.values()
        )

    governors = observed.get("governors")
    governor_ok = (
        isinstance(governors, dict)
        and bool(governors)
        and all(value == definition.get("governor") for value in governors.values())
    )
    epp = observed.get("epp")
    epp_ok = (
        isinstance(epp, dict)
        and bool(epp)
        and all(value == definition.get("epp") for value in epp.values())
    )
    ppd_ok = observed.get("ppd_profile") == definition.get("ppd_profile")
    no_turbo = 0 if definition.get("turbo_enabled") is True else 1
    misc_ok = observed.get("no_turbo") == no_turbo and observed.get(
        "max_perf_pct"
    ) == definition.get("max_perf_pct")
    return {
        "governor_ok": governor_ok,
        "epp_ok": epp_ok,
        "ppd_ok": ppd_ok,
        "rapl_ok": rapl_ok,
        "misc_ok": misc_ok,
    }


def _normalize_power_definition(definition: dict[str, Any]) -> dict[str, Any]:
    """Validate untrusted power-profile data at the hardware boundary."""
    if not _POWER_DEFINITION_FIELDS.issubset(definition):
        raise ValueError("Power profile definition is incomplete")
    integer_fields = ("pl1_uw", "pl2_uw", "max_perf_pct")
    if any(
        isinstance(definition[key], bool) or not isinstance(definition[key], int)
        for key in integer_fields
    ):
        raise ValueError("Power limits and max_perf_pct must be integers")
    if not isinstance(definition["turbo_enabled"], bool):
        raise ValueError("turbo_enabled must be a boolean")
    string_fields = ("governor", "epp", "ppd_profile")
    if any(not isinstance(definition[key], str) for key in string_fields):
        raise ValueError("Governor, EPP, and PPD profile must be strings")

    normalized = {key: definition[key] for key in _POWER_DEFINITION_FIELDS}
    if not 3_000_000 <= normalized["pl1_uw"] <= 100_000_000:
        raise ValueError("PL1 is outside the supported range")
    if not normalized["pl1_uw"] <= normalized["pl2_uw"] <= 150_000_000:
        raise ValueError("PL2 is outside the supported range")
    if normalized["governor"] not in {"powersave", "performance"}:
        raise ValueError("Governor is unsupported")
    if normalized["epp"] not in {
        "power",
        "default",
        "balance_power",
        "balance_performance",
        "performance",
    }:
        raise ValueError("EPP value is unsupported")
    if normalized["ppd_profile"] not in {
        "power-saver",
        "balanced",
        "performance",
    }:
        raise ValueError("PPD profile is unsupported")
    if not 1 <= normalized["max_perf_pct"] <= 100:
        raise ValueError("max_perf_pct is outside the supported range")
    return normalized


class HardwarePort(Protocol):
    """The narrow interface every hardware implementation must provide.

    Grouped by feature.  All methods are synchronous (they perform real
    I/O) and are called through the serialized command queue.
    """

    # -- Platform / capability --
    def detect_platform(self) -> PlatformInfo: ...
    def get_battery_capability(self) -> Capability: ...
    def get_power_capability(self) -> Capability: ...
    def get_fan_capability(self) -> Capability: ...
    def get_gestures_capability(self) -> Capability: ...
    def get_gpu_capability(self) -> Capability: ...

    # -- Battery --
    def read_battery(self) -> BatterySnapshot: ...
    def write_battery_thresholds(self, end: int, start: int) -> dict[str, Any]: ...

    # -- Power --
    def read_power(self) -> PowerSnapshot: ...
    def apply_power_profile(
        self, profile: str, definition: dict[str, Any] | None = None
    ) -> dict[str, Any]: ...

    # -- Fan --
    def read_fan(self) -> FanSnapshot: ...
    def set_fan_auto(self) -> bool: ...
    def set_fan_manual(self, speed: int) -> bool: ...
    def set_fan_speed(self, speed: int) -> bool: ...
    def read_fan_temp(self) -> int | None: ...

    # -- Gestures --
    def read_gestures(self) -> GesturesSnapshot: ...
    def list_gesture_mappings(self) -> list[GestureEntry]: ...

    # -- Touchpad firmware --
    def probe_touchpad_firmware(self) -> TouchpadFirmwareProbe: ...
    def apply_touchpad_settings(
        self, settings: Mapping[str, int]
    ) -> TouchpadBatchApplyResult: ...
    def query_touchpad_support(self) -> frozenset[int]: ...

    # -- GPU --
    def read_gpu(self) -> GpuSnapshot: ...
    def apply_gpu_mitigation(self) -> dict[str, Any]: ...
    def restore_gpu_mitigation(self) -> dict[str, Any]: ...

    # -- Dependency --
    def check_dependency(self) -> bool: ...


# -- FakeHardware (for tests and dev session-bus mode) ------------------------


class FakeHardware:
    """In-memory hardware implementation for tests and development.

    Every method returns plausible typed data.  Writes are recorded in
    ``call_log`` so tests can assert on the exact sequence of operations.
    Failures can be queued via ``fail_next``.
    """

    def __init__(self) -> None:
        self.call_log: list[tuple[str, tuple, dict]] = []
        self._fail_next: str | None = None
        self._platform = PlatformInfo(
            vendor="Honor",
            product="MAGICBOOK_X14_2024",
            model="Honor MagicBook X14 2024",
            cpu_model="Intel Core i5-1240P",
            matched=True,
            confidence="high",
        )
        self._battery_end = 90
        self._battery_start = 85
        self._power_profile = "balanced"
        self._power_observed: dict[str, Any] = {
            "rapl": {
                "intel-rapl:0": {
                    _RAPL_LIMIT_FILES[0]: 25_000_000,
                    _RAPL_LIMIT_FILES[1]: 35_000_000,
                }
            },
            "governors": {"0": "powersave"},
            "epp": {"0": "balance_power"},
            "ppd_profile": "balanced",
            "no_turbo": 0,
            "max_perf_pct": 100,
        }
        self._fan_speed = 0
        self._fan_mode = FanMode.STOCK
        self._fan_temp = 45_000
        self._gpu_mitigated = False
        self._gesture_mappings: dict[str, tuple[bool, str]] = {
            "3:1": (True, "leftmeta,v"),
            "3:2": (True, "leftmeta,c"),
            "3:3": (False, "leftmeta,tab"),
        }
        self._touchpad_settings: dict[str, int] = {}
        self._touchpad_support = frozenset({1, 3, 4, 20, 27})
        self._dependency_ok = True

    def fail_next(self, operation: str) -> None:
        """Queue a failure for the next call to ``operation``."""
        self._fail_next = operation

    def _log(self, name: str, *args: Any, **kwargs: Any) -> None:
        self.call_log.append((name, args, kwargs))

    def _check_fail(self, name: str) -> None:
        if self._fail_next == name:
            self._fail_next = None
            raise DomainException(
                DomainError.UNAVAILABLE,
                f"FakeHardware: queued failure for {name}",
            )

    # -- Platform / capability --

    def detect_platform(self) -> PlatformInfo:
        self._log("detect_platform")
        return self._platform

    def get_battery_capability(self) -> Capability:
        self._log("get_battery_capability")
        return Capability(
            status=CapabilityStatus.SUPPORTED,
            reason_code="",
            message="Battery charge control available",
            resources=("/sys/class/power_supply/BAT0/charge_control_end_threshold",),
        )

    def get_power_capability(self) -> Capability:
        self._log("get_power_capability")
        return Capability(
            status=CapabilityStatus.SUPPORTED,
            message="Power profile control available",
        )

    def get_fan_capability(self) -> Capability:
        self._log("get_fan_capability")
        return Capability(
            status=CapabilityStatus.SUPPORTED,
            message="Fan EC control available",
            resources=("/proc/acpi/call",),
        )

    def get_gestures_capability(self) -> Capability:
        self._log("get_gestures_capability")
        return Capability(
            status=CapabilityStatus.SUPPORTED,
            message="Touchpad gesture daemon available",
        )

    def get_gpu_capability(self) -> Capability:
        self._log("get_gpu_capability")
        return Capability(
            status=CapabilityStatus.SUPPORTED,
            message="GPU IRQ mitigation available",
        )

    # -- Battery --

    def read_battery(self) -> BatterySnapshot:
        self._log("read_battery")
        self._check_fail("read_battery")
        return BatterySnapshot(
            available=True,
            capacity_percent=75,
            status=BatteryStatusKind.CHARGING,
            ac_online=True,
            observed_end=self._battery_end,
            observed_start=self._battery_start,
            desired_end=self._battery_end,
            desired_start=self._battery_start,
        )

    def write_battery_thresholds(self, end: int, start: int) -> dict[str, Any]:
        self._log("write_battery_thresholds", end, start)
        self._check_fail("write_battery_thresholds")
        self._battery_end = end
        self._battery_start = start
        return {"end_write_ok": True, "start_write_ok": True, "readback_ok": True}

    # -- Power --

    def read_power(self) -> PowerSnapshot:
        self._log("read_power")
        self._check_fail("read_power")
        return PowerSnapshot(
            available=True,
            applied_profile=self._power_profile,
            observed_summary=self._power_observed,
            ac_online=True,
        )

    def apply_power_profile(
        self, profile: str, definition: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        self._log("apply_power_profile", profile, definition)
        self._check_fail("apply_power_profile")
        if definition is None:
            return {"error": "A complete power profile definition is required"}
        self._power_profile = profile
        self._power_observed = {
            "rapl": {
                "intel-rapl:0": {
                    _RAPL_LIMIT_FILES[0]: definition["pl1_uw"],
                    _RAPL_LIMIT_FILES[1]: definition["pl2_uw"],
                }
            },
            "governors": {"0": definition["governor"]},
            "epp": {"0": definition["epp"]},
            "ppd_profile": definition["ppd_profile"],
            "no_turbo": 0 if definition["turbo_enabled"] else 1,
            "max_perf_pct": definition["max_perf_pct"],
        }
        return {
            "profile": profile,
            "observed": self._power_observed,
            **verify_power_definition(self._power_observed, definition),
        }

    # -- Fan --

    def read_fan(self) -> FanSnapshot:
        self._log("read_fan")
        self._check_fail("read_fan")
        return FanSnapshot(
            available=True,
            mode=self._fan_mode,
            desired_mode=self._fan_mode,
            temp_mc=self._fan_temp,
            target_speed=self._fan_speed if self._fan_mode != FanMode.STOCK else None,
        )

    def set_fan_auto(self) -> bool:
        self._log("set_fan_auto")
        self._check_fail("set_fan_auto")
        self._fan_mode = FanMode.STOCK
        return True

    def set_fan_manual(self, speed: int) -> bool:
        self._log("set_fan_manual", speed)
        self._check_fail("set_fan_manual")
        self._fan_mode = FanMode.MANUAL_OVERRIDE
        return True

    def set_fan_speed(self, speed: int) -> bool:
        self._log("set_fan_speed", speed)
        self._check_fail("set_fan_speed")
        self._fan_speed = speed
        return True

    def read_fan_temp(self) -> int | None:
        self._log("read_fan_temp")
        self._check_fail("read_fan_temp")
        return self._fan_temp

    # -- Gestures --

    def read_gestures(self) -> GesturesSnapshot:
        self._log("read_gestures")
        return GesturesSnapshot(
            available=True,
            daemon_running=False,
            device_found=True,
            mappings=tuple(self.list_gesture_mappings()),
            wmi_transport_present=True,
            firmware_settings_supported=True,
            firmware_settings=dict(self._touchpad_settings),
        )

    def list_gesture_mappings(self) -> list[GestureEntry]:
        self._log("list_gesture_mappings")
        entries: list[GestureEntry] = []
        for gid, (enabled, mapping) in self._gesture_mappings.items():
            entries.append(
                GestureEntry(
                    id=gid,
                    label=gid,
                    enabled=enabled,
                    mapping=mapping,
                    default_mapping=mapping,
                )
            )
        return entries

    # -- Touchpad firmware --

    def probe_touchpad_firmware(self) -> TouchpadFirmwareProbe:
        self._log("probe_touchpad_firmware")
        self._check_fail("probe_touchpad_firmware")
        return TouchpadFirmwareProbe(
            available=True,
            platform_verified=True,
            dmi_vendor="HONOR",
            dmi_product="MRA-XXX",
            device_found=True,
            descriptor_verified=True,
            device_path="/dev/hidraw0",
            report_id=0x0E,
            input_report_bytes=9,
            output_report_bytes=9,
        )

    def apply_touchpad_settings(
        self, settings: Mapping[str, int]
    ) -> TouchpadBatchApplyResult:
        self._log("apply_touchpad_settings", dict(settings))
        self._check_fail("apply_touchpad_settings")
        from honor_control.backend.touchpad_firmware import TouchpadApplyResult
        from honor_control.core.touchpad import (
            TouchpadSetting,
            encode_touchpad_setting,
            parse_touchpad_setting,
            parse_touchpad_value,
        )

        normalized = {
            parse_touchpad_setting(name): parse_touchpad_value(name, value)
            for name, value in settings.items()
        }
        results = tuple(
            TouchpadApplyResult(
                setting=setting,
                value=normalized[setting],
                device_path="/dev/hidraw0",
                reports=encode_touchpad_setting(setting, normalized[setting]),
                reports_applied=len(encode_touchpad_setting(setting, normalized[setting])),
                clock_synchronized=True,
            )
            for setting in TouchpadSetting
            if setting in normalized
        )
        total = sum(len(item.reports) for item in results)
        self._touchpad_settings.update(
            {setting.value: value for setting, value in normalized.items()}
        )
        return TouchpadBatchApplyResult(
            device_path="/dev/hidraw0",
            settings=results,
            reports_applied=total,
            total_reports=total,
            clock_synchronized=True,
        )

    def query_touchpad_support(self) -> frozenset[int]:
        self._log("query_touchpad_support")
        self._check_fail("query_touchpad_support")
        return self._touchpad_support

    def set_gesture_mapping(self, gesture_id: str, mapping: str) -> bool:
        self._log("set_gesture_mapping", gesture_id, mapping)
        self._check_fail("set_gesture_mapping")
        if gesture_id in self._gesture_mappings:
            enabled, _ = self._gesture_mappings[gesture_id]
            self._gesture_mappings[gesture_id] = (enabled, mapping)
            return True
        return False

    def set_gesture_enabled(self, gesture_id: str, enabled: bool) -> bool:
        self._log("set_gesture_enabled", gesture_id, enabled)
        self._check_fail("set_gesture_enabled")
        if gesture_id in self._gesture_mappings:
            _, mapping = self._gesture_mappings[gesture_id]
            self._gesture_mappings[gesture_id] = (enabled, mapping)
            return True
        return False

    # -- GPU --

    def read_gpu(self) -> GpuSnapshot:
        self._log("read_gpu")
        return GpuSnapshot(
            available=True,
            mitigation_enabled=self._gpu_mitigated,
            target_cpu=0,
            irqs=("i915",),
        )

    def apply_gpu_mitigation(self) -> dict[str, Any]:
        self._log("apply_gpu_mitigation")
        self._check_fail("apply_gpu_mitigation")
        self._gpu_mitigated = True
        return {"affinity_ok": True, "cstate_ok": True}

    def restore_gpu_mitigation(self) -> dict[str, Any]:
        self._log("restore_gpu_mitigation")
        self._check_fail("restore_gpu_mitigation")
        self._gpu_mitigated = False
        return {"restored": True}

    # -- Dependency --

    def check_dependency(self) -> bool:
        self._log("check_dependency")
        return self._dependency_ok


# -- HonorToolsAdapter (production) ------------------------------------------


def _discover_power_supply(
    root: pathlib.Path = pathlib.Path("/sys/class/power_supply"),
) -> tuple[pathlib.Path | None, pathlib.Path | None]:
    """Discover battery and AC adapter paths.

    Returns ``(battery_path, ac_path)``.  ``None`` when not found.
    Does not assume BAT0/ADP1.
    """
    if not root.exists():
        return None, None
    battery: pathlib.Path | None = None
    ac: pathlib.Path | None = None
    for entry in sorted(root.iterdir()):
        if not entry.is_dir():
            continue
        type_file = entry / "type"
        if not type_file.exists():
            continue
        try:
            ptype = type_file.read_text(encoding="utf-8").strip().lower()
        except OSError:
            continue
        if ptype == "battery" and battery is None:
            battery = entry
        elif ptype == "mains" and ac is None:
            ac = entry
    return battery, ac


def _discover_temperature_inputs(
    root: pathlib.Path = pathlib.Path("/sys/class/hwmon"),
) -> tuple[pathlib.Path, ...]:
    """Return readable CPU temperature inputs from known hwmon drivers."""
    if not root.is_dir():
        return ()
    for entry in sorted(root.glob("hwmon*")):
        try:
            name = (entry / "name").read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if name not in {"coretemp", "k10temp"}:
            continue
        inputs = tuple(sorted(entry.glob("temp*_input")))
        if inputs:
            return inputs
    return ()


def _discover_fan_inputs(
    root: pathlib.Path = pathlib.Path("/sys/class/hwmon"),
) -> tuple[pathlib.Path, ...]:
    """Return all readable hwmon RPM inputs, independent of temp driver."""
    if not root.is_dir():
        return ()
    inputs: list[pathlib.Path] = []
    for entry in sorted(root.glob("hwmon*")):
        for path in sorted(entry.glob("fan*_input")):
            try:
                value = int(path.read_text(encoding="utf-8").strip())
            except (OSError, ValueError):
                continue
            if value >= 0:
                inputs.append(path)
    return tuple(inputs)


class HonorToolsAdapter:
    """Production adapter wrapping the ``honor-tools`` package.

    All ``import honor.*`` statements live here.  No other module may
    import ``honor``.  The adapter translates dependency booleans/dicts
    into typed domain models.
    """

    def __init__(self, root_path: pathlib.Path | None = None) -> None:
        self._root = root_path or pathlib.Path("/")
        self._honor_error = ""
        self._honor_ok = self._try_import()
        self._platform: Any = None
        self._platform_detected = False
        self._battery_path, self._ac_path = _discover_power_supply(
            self._root / "sys/class/power_supply"
        )
        self._temperature_inputs = _discover_temperature_inputs(
            self._root / "sys/class/hwmon"
        )
        self._fan_inputs = _discover_fan_inputs(self._root / "sys/class/hwmon")

    def _rooted(self, path: str) -> pathlib.Path:
        return self._root / path.lstrip("/")

    def _read_text(self, path: str) -> str:
        try:
            return self._rooted(path).read_text(encoding="utf-8").strip()
        except OSError:
            return ""

    def _read_cpu_model(self) -> str:
        model = self._read_text("/sys/devices/system/cpu/cpu0/model_name")
        if model:
            return model
        cpuinfo = self._read_text("/proc/cpuinfo")
        for line in cpuinfo.splitlines():
            if line.casefold().startswith("model name") and ":" in line:
                return line.split(":", 1)[1].strip()
        return ""

    def _try_import(self) -> bool:
        try:
            from importlib.metadata import version

            import honor  # noqa: F401
            from honor.config import Config  # noqa: F401
            from honor.fan import read_state, set_auto, set_manual, set_speed
            from honor.gpu import apply_irq_fix, find_gpu_irqs, get_status
            from honor.platform import detect

            required = (
                read_state,
                set_auto,
                set_manual,
                set_speed,
                apply_irq_fix,
                find_gpu_irqs,
                get_status,
                detect,
            )
            if not all(callable(item) for item in required):
                raise TypeError("honor-tools exports an incompatible API")
            installed = version("honor-tools")
            if installed != "0.1.0":
                raise RuntimeError(f"honor-tools 0.1.0 is required; found {installed}")
            return True
        except Exception as exc:  # noqa: BLE001
            self._honor_error = str(exc)
            log.warning("honor-tools is unavailable or incompatible: %s", exc)
            return False

    def check_dependency(self) -> bool:
        """Return True when ``honor-tools`` is importable and compatible."""
        return self._honor_ok

    def _require_honor(self) -> None:
        if not self._honor_ok:
            raise DomainException(
                DomainError.DEPENDENCY,
                self._honor_error or "honor-tools package is not compatible",
            )

    def _require_platform(self) -> Any:
        platform = self._require_platform_or_none()
        if platform is None:
            raise DomainException(
                DomainError.UNSUPPORTED,
                "Platform not detected; hardware writes are disabled",
            )
        return platform

    def _require_platform_or_none(self) -> Any:
        """Return the one-time platform detection result, including cached None."""
        if not self._platform_detected:
            self._platform = self._detect_platform_obj()
            self._platform_detected = True
        return self._platform

    def _detect_platform_obj(self) -> Any:
        """Detect the platform using honor.platform.detect().

        Unlike the old code, we do NOT fall back to a default platform
        for unknown CPUs.  ``detect()`` returning a non-None object does
        not mean writes are safe; we require a positive model match.
        """
        if self._platform_detected:
            return self._platform
        try:
            from honor.platform import detect

            vendor = self._read_text("/sys/class/dmi/id/sys_vendor")
            product = self._read_text("/sys/class/dmi/id/product_name")
            cpu_model = self._read_cpu_model().casefold()
            expected_cpus = _SUPPORTED_FAN_IDENTITIES.get(product.casefold(), ())
            if (
                vendor.casefold() != "honor"
                or not expected_cpus
                or not any(marker in cpu_model for marker in expected_cpus)
            ):
                log.warning(
                    "platform is not in the verified fan allowlist: %r %r %r",
                    vendor,
                    product,
                    cpu_model,
                )
                return None
            plat = detect()
            if plat and getattr(plat, "name", "") and plat.name != "Unknown":
                return plat
            log.warning("platform detect returned unknown hardware; writes disabled")
            return None
        except Exception as exc:  # noqa: BLE001
            log.warning("platform detect failed: %s", exc)
            return None

    # -- Platform / capability --

    def detect_platform(self) -> PlatformInfo:
        import platform as stdlib_platform

        cpu_model = self._read_cpu_model()
        if not cpu_model:
            try:
                cpu_model = stdlib_platform.processor() or ""
            except Exception:  # noqa: BLE001
                pass
        plat = self._require_platform_or_none()
        if plat is None:
            return PlatformInfo(cpu_model=cpu_model, matched=False, confidence="none")
        return PlatformInfo(
            vendor=self._read_text("/sys/class/dmi/id/sys_vendor"),
            product=self._read_text("/sys/class/dmi/id/product_name"),
            model=getattr(plat, "name", "") or "",
            cpu_model=cpu_model,
            matched=True,
            confidence="high",
        )

    def get_battery_capability(self) -> Capability:
        if self._battery_path is None:
            return Capability(
                status=CapabilityStatus.UNAVAILABLE,
                reason_code="battery_sysfs_missing",
                message="No battery sysfs device found",
            )
        end_file = self._battery_path / "charge_control_end_threshold"
        start_file = self._battery_path / "charge_control_start_threshold"
        if not end_file.exists() or not start_file.exists():
            missing = [
                str(path) for path in (end_file, start_file) if not path.exists()
            ]
            return Capability(
                status=CapabilityStatus.UNAVAILABLE,
                reason_code="charge_control_threshold_missing",
                message="Both battery charge-control threshold files are required",
                resources=tuple(missing),
            )
        return Capability(
            status=CapabilityStatus.SUPPORTED,
            message="Battery charge control available",
            resources=(str(end_file), str(start_file)),
        )

    def _power_cpu_dirs(self) -> tuple[pathlib.Path, ...]:
        root = self._rooted(_CPU_ROOT)
        if not root.is_dir():
            return ()
        return tuple(
            path
            for path in sorted(root.glob("cpu[0-9]*"))
            if path.name[3:].isdigit() and (path / "cpufreq").is_dir()
        )

    def _rapl_trees(self) -> tuple[pathlib.Path, ...]:
        return tuple(
            path
            for path in (self._rooted(item) for item in _RAPL_TREES)
            if path.is_dir()
        )

    @staticmethod
    def _path_is_readable_and_writable(path: pathlib.Path) -> bool:
        try:
            path.read_text(encoding="utf-8")
        except OSError:
            return False
        return os.access(path, os.W_OK)

    @staticmethod
    def _run_powerprofilesctl(*args: str) -> subprocess.CompletedProcess[str] | None:
        try:
            return subprocess.run(
                ["powerprofilesctl", *args],
                text=True,
                capture_output=True,
                check=False,
                timeout=3,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None

    def _get_ppd_profile(self) -> str:
        result = self._run_powerprofilesctl("get")
        if result is None or result.returncode != 0:
            return ""
        return result.stdout.strip()

    def _set_ppd_profile(self, profile: str) -> bool:
        result = self._run_powerprofilesctl("set", profile)
        if result is None or result.returncode != 0:
            if result is not None and result.stderr.strip():
                log.warning(
                    "powerprofilesctl set %s failed: %s",
                    profile,
                    result.stderr.strip(),
                )
            return False
        return True

    def get_power_capability(self) -> Capability:
        if not self._honor_ok:
            return Capability(
                status=CapabilityStatus.UNAVAILABLE,
                reason_code="honor_tools_incompatible",
                message=self._honor_error or "honor-tools is unavailable",
            )
        if self._require_platform_or_none() is None:
            return Capability(
                status=CapabilityStatus.UNSUPPORTED,
                reason_code="platform_not_supported",
                message="Power profiles are disabled on unverified hardware",
            )
        if shutil.which("powerprofilesctl") is None:
            return Capability(
                status=CapabilityStatus.UNAVAILABLE,
                reason_code="powerprofilesctl_missing",
                message="powerprofilesctl not found",
            )
        if not self._get_ppd_profile():
            return Capability(
                status=CapabilityStatus.UNAVAILABLE,
                reason_code="power_profiles_daemon_unavailable",
                message="power-profiles-daemon did not report an active profile",
            )

        cpu_dirs = self._power_cpu_dirs()
        rapl_trees = self._rapl_trees()
        required = [
            *(
                path / "cpufreq" / filename
                for path in cpu_dirs
                for filename in (
                    "scaling_governor",
                    "energy_performance_preference",
                )
            ),
            *(path / filename for path in rapl_trees for filename in _RAPL_LIMIT_FILES),
            self._rooted(f"{_INTEL_PSTATE_ROOT}/no_turbo"),
            self._rooted(f"{_INTEL_PSTATE_ROOT}/max_perf_pct"),
        ]
        if not cpu_dirs or not rapl_trees:
            return Capability(
                status=CapabilityStatus.UNAVAILABLE,
                reason_code="power_sysfs_missing",
                message="CPU frequency or Intel RAPL controls are unavailable",
            )
        inaccessible = [
            str(path)
            for path in required
            if not self._path_is_readable_and_writable(path)
        ]
        if inaccessible:
            return Capability(
                status=CapabilityStatus.UNAVAILABLE,
                reason_code="power_sysfs_inaccessible",
                message="Required power controls are not readable and writable",
                resources=tuple(inaccessible),
            )
        return Capability(
            status=CapabilityStatus.SUPPORTED,
            message="PPD-coordinated power profile control is available",
            resources=tuple(str(path) for path in required),
        )

    def get_fan_capability(self) -> Capability:
        plat = self._require_platform_or_none()
        if plat is None:
            return Capability(
                status=CapabilityStatus.UNSUPPORTED,
                reason_code="platform_not_supported",
                message="Fan control requires a recognized Honor platform",
            )
        acpi_path = getattr(plat, "acpi_call_path", "/proc/acpi/call")
        rooted_acpi_path = self._rooted(acpi_path)
        if not rooted_acpi_path.exists():
            return Capability(
                status=CapabilityStatus.UNAVAILABLE,
                reason_code="acpi_call_missing",
                message=f"{acpi_path} not found",
            )
        temperatures = [
            value
            for value in (self._read_int(path) for path in self._temperature_inputs)
            if value is not None and value > 0
        ]
        if not temperatures:
            return Capability(
                status=CapabilityStatus.UNAVAILABLE,
                reason_code="fan_temperature_missing",
                message="No readable coretemp/k10temp sensor found",
            )
        return Capability(
            status=CapabilityStatus.SUPPORTED,
            message="Fan EC control available",
            resources=(
                acpi_path,
                *(str(path) for path in self._temperature_inputs),
            ),
        )

    def get_gestures_capability(self) -> Capability:
        probe = probe_gesture_environment(
            sysfs_root=self._root / "sys/class/hidraw",
            dev_root=self._root / "dev",
        )
        if not probe.available:
            return Capability(
                status=CapabilityStatus.UNAVAILABLE,
                reason_code="gesture_runtime_unavailable",
                message=probe.error,
                resources=(probe.device_path,) if probe.device_path else (),
            )
        return Capability(
            status=CapabilityStatus.SUPPORTED,
            message="Touchpad HID input and uinput are accessible",
            resources=(probe.device_path, str(self._root / "dev/uinput")),
        )

    def get_gpu_capability(self) -> Capability:
        try:
            from honor.gpu import find_gpu_irqs

            irqs = find_gpu_irqs()
            if irqs:
                return Capability(
                    status=CapabilityStatus.EXPERIMENTAL,
                    reason_code="restore_not_supported",
                    message="GPU mitigation disabled until reversible restore is available",
                    resources=tuple(irqs),
                )
        except Exception:  # noqa: BLE001
            pass
        return Capability(
            status=CapabilityStatus.UNAVAILABLE,
            reason_code="no_gpu_irqs",
            message="No i915/xe GPU IRQs found",
        )

    # -- Battery --

    def read_battery(self) -> BatterySnapshot:
        cap = self.get_battery_capability()
        if not cap.writable or self._battery_path is None:
            return BatterySnapshot(available=False)
        try:
            end = self._read_int(self._battery_path / "charge_control_end_threshold")
            start = self._read_int(
                self._battery_path / "charge_control_start_threshold"
            )
            from honor_control.core.models import derive_charge_mode

            mode = (
                derive_charge_mode(end or 0, start or 0) if end else ChargeMode.CUSTOM
            )
            raw_status = (
                (self._battery_path / "status")
                .read_text(encoding="utf-8")
                .strip()
                .lower()
                .replace(" ", "_")
            )
            try:
                battery_status = BatteryStatusKind(raw_status)
            except ValueError:
                battery_status = BatteryStatusKind.UNKNOWN
            return BatterySnapshot(
                available=True,
                capacity_percent=self._read_int(self._battery_path / "capacity"),
                status=battery_status,
                ac_online=self._read_int(self._ac_path / "online") == 1
                if self._ac_path
                else None,
                observed_end=end,
                observed_start=start,
                desired_end=end,
                desired_start=start,
                mode=mode,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("battery read failed: %s", exc)
            return BatterySnapshot(available=False, last_error=str(exc))

    def write_battery_thresholds(self, end: int, start: int) -> dict[str, Any]:
        if self._battery_path is None:
            return {"end_write_ok": False, "start_write_ok": False}
        try:
            end_path = self._battery_path / "charge_control_end_threshold"
            start_path = self._battery_path / "charge_control_start_threshold"
            old_start = self._read_int(start_path)
            # Preserve the kernel invariant during transitions. For example,
            # moving 90/85 -> 60/55 must lower start before end.
            if old_start is not None and end < old_start:
                start_ok = self._write_int(start_path, start)
                end_ok = self._write_int(end_path, end) if start_ok else False
                write_order = ("start", "end")
            else:
                end_ok = self._write_int(end_path, end)
                start_ok = self._write_int(start_path, start) if end_ok else False
                write_order = ("end", "start")
            result = {"end_write_ok": end_ok, "start_write_ok": start_ok}
            result["write_order"] = write_order
            observed_end = (
                self._read_int(self._battery_path / "charge_control_end_threshold")
                if self._battery_path
                else None
            )
            observed_start = (
                self._read_int(self._battery_path / "charge_control_start_threshold")
                if self._battery_path
                else None
            )
            result["observed_end"] = observed_end
            result["observed_start"] = observed_start
            result["readback_ok"] = observed_end == end and observed_start == start
            return result
        except Exception as exc:  # noqa: BLE001
            log.error("battery write failed: %s", exc)
            return {"end_write_ok": False, "start_write_ok": False, "error": str(exc)}

    # -- Power --

    @staticmethod
    def _read_path(path: pathlib.Path) -> str:
        try:
            return path.read_text(encoding="utf-8").strip()
        except OSError:
            return ""

    def _read_power_status(self) -> dict[str, Any]:
        rapl: dict[str, dict[str, int | None]] = {}
        for tree in self._rapl_trees():
            rapl[tree.name] = {
                filename: self._read_int(tree / filename)
                for filename in _RAPL_LIMIT_FILES
            }

        governors: dict[str, str] = {}
        epp: dict[str, str] = {}
        for cpu in self._power_cpu_dirs():
            index = cpu.name[3:]
            cpufreq = cpu / "cpufreq"
            governors[index] = self._read_path(cpufreq / "scaling_governor")
            epp[index] = self._read_path(cpufreq / "energy_performance_preference")

        return {
            "rapl": rapl,
            "governors": governors,
            "epp": epp,
            "ppd_profile": self._get_ppd_profile(),
            "no_turbo": self._read_int(self._rooted(f"{_INTEL_PSTATE_ROOT}/no_turbo")),
            "max_perf_pct": self._read_int(
                self._rooted(f"{_INTEL_PSTATE_ROOT}/max_perf_pct")
            ),
        }

    def read_power(self) -> PowerSnapshot:
        if self._require_platform_or_none() is None:
            return PowerSnapshot(
                available=False,
                last_error="Power status is disabled on unverified hardware",
            )
        try:
            status = self._read_power_status()
            rapl = status["rapl"]
            complete = (
                isinstance(rapl, dict)
                and bool(rapl)
                and all(
                    isinstance(values, dict)
                    and all(value is not None for value in values.values())
                    for values in rapl.values()
                )
                and bool(status["governors"])
                and all(status["governors"].values())
                and bool(status["epp"])
                and all(status["epp"].values())
                and bool(status["ppd_profile"])
                and status["no_turbo"] is not None
                and status["max_perf_pct"] is not None
            )
            ac_online = None
            if self._ac_path is not None:
                ac_online = self._read_int(self._ac_path / "online") == 1
            return PowerSnapshot(
                available=complete,
                observed_summary=status,
                ac_online=ac_online,
                last_error="" if complete else "Power observation is incomplete",
            )
        except Exception as exc:  # noqa: BLE001
            return PowerSnapshot(available=False, last_error=str(exc))

    def apply_power_profile(
        self, profile: str, definition: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        self._require_honor()
        self._require_platform()
        if definition is None:
            return {"error": "A complete power profile definition is required"}
        try:
            normalized = _normalize_power_definition(definition)
            capability = self.get_power_capability()
            if not capability.writable:
                return {
                    "error": capability.message or "Power control is unavailable",
                    "reason_code": capability.reason_code,
                }

            # intel_pstate rejects EPP changes while the performance governor
            # is active.  Move every policy to powersave before asking PPD to
            # transition; PPD must write EPP as part of that transition.
            cpu_dirs = self._power_cpu_dirs()
            governor_pre = {
                cpu.name[3:]: self._write_text_verified(
                    cpu / "cpufreq/scaling_governor", "powersave"
                )
                for cpu in cpu_dirs
            }
            ppd_write_ok = self._set_ppd_profile(normalized["ppd_profile"])
            if not ppd_write_ok:
                observed = self._read_power_status()
                return {
                    "profile": profile,
                    "writes": {"governor_pre": governor_pre, "ppd": False},
                    "observed": observed,
                    "governor_ok": _all_true(governor_pre)
                    and verify_power_definition(observed, normalized)["governor_ok"],
                    "epp_ok": False,
                    "ppd_ok": False,
                    "rapl_ok": False,
                    "misc_ok": False,
                }

            # Let PPD finish its profile transition before applying the
            # definition-specific sysfs values that are verified below.
            time.sleep(PPD_SETTLE_SECONDS)
            rapl_writes = {
                tree.name: {
                    _RAPL_LIMIT_FILES[0]: self._write_text_verified(
                        tree / _RAPL_LIMIT_FILES[0], normalized["pl1_uw"]
                    ),
                    _RAPL_LIMIT_FILES[1]: self._write_text_verified(
                        tree / _RAPL_LIMIT_FILES[1], normalized["pl2_uw"]
                    ),
                }
                for tree in self._rapl_trees()
            }
            # Intel P-State may reset EPP when the final governor changes to
            # performance.  Apply the governor first, then make EPP the last
            # CPU-policy write so the verified value is the one that remains.
            governor_writes = {
                cpu.name[3:]: self._write_text_verified(
                    cpu / "cpufreq/scaling_governor", normalized["governor"]
                )
                for cpu in cpu_dirs
            }
            epp_writes = self._write_epp(normalized["epp"], cpu_dirs)
            misc_writes = {
                "no_turbo": self._write_text_verified(
                    self._rooted(f"{_INTEL_PSTATE_ROOT}/no_turbo"),
                    0 if normalized["turbo_enabled"] else 1,
                ),
                "max_perf_pct": self._write_text_verified(
                    self._rooted(f"{_INTEL_PSTATE_ROOT}/max_perf_pct"),
                    normalized["max_perf_pct"],
                ),
            }
            observed = self._read_power_status()
            verified = verify_power_definition(observed, normalized)
            writes = {
                "ppd": ppd_write_ok,
                "governor_pre": governor_pre,
                "rapl": rapl_writes,
                "epp": epp_writes,
                "governor": governor_writes,
                "misc": misc_writes,
            }
            return {
                "profile": profile,
                "writes": writes,
                "observed": observed,
                "governor_ok": _all_true(governor_pre)
                and _all_true(governor_writes)
                and verified["governor_ok"],
                "epp_ok": _all_true(epp_writes) and verified["epp_ok"],
                "ppd_ok": ppd_write_ok and verified["ppd_ok"],
                "rapl_ok": _all_true(rapl_writes) and verified["rapl_ok"],
                "misc_ok": _all_true(misc_writes) and verified["misc_ok"],
            }
        except Exception as exc:  # noqa: BLE001
            return {"error": str(exc)}

    @staticmethod
    def _write_text_verified(path: pathlib.Path, value: str | int) -> bool:
        """Write one sysfs value and require exact immediate readback."""
        try:
            expected = str(value)
            path.write_text(expected, encoding="utf-8")
            return path.read_text(encoding="utf-8").strip() == expected
        except OSError:
            return False

    def _write_epp(
        self, value: str, cpu_dirs: tuple[pathlib.Path, ...]
    ) -> dict[str, bool]:
        """Write and verify EPP with one bounded retry budget."""
        deadline = time.monotonic() + 2.0
        result: dict[str, bool] = {}
        for cpu in cpu_dirs:
            path = cpu / "cpufreq/energy_performance_preference"
            ok = False
            for attempt in range(3):
                ok = self._write_text_verified(path, value)
                if ok or time.monotonic() >= deadline:
                    break
                if attempt < 2:
                    time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))
            result[cpu.name[3:]] = ok
        return result

    # -- Fan --

    def read_fan(self) -> FanSnapshot:
        plat = self._require_platform_or_none()
        if plat is None:
            return FanSnapshot(available=False)
        try:
            from honor.fan import read_state

            state = read_state(plat)
            temp = state.get("temp")
            if not isinstance(temp, int) or temp <= 0:
                return FanSnapshot(
                    available=False,
                    last_error="CPU temperature sensor returned no data",
                )
            measured_rpms = [
                value
                for value in (self._read_int(path) for path in self._fan_inputs)
                if value is not None and value > 0
            ]
            dependency_rpm = state.get("fan_rpm")
            if isinstance(dependency_rpm, int) and dependency_rpm > 0:
                measured_rpms.append(dependency_rpm)
            return FanSnapshot(
                available=True,
                temp_mc=temp,
                measured_rpm=max(measured_rpms) if measured_rpms else None,
            )
        except Exception as exc:  # noqa: BLE001
            return FanSnapshot(available=False, last_error=str(exc))

    def set_fan_auto(self) -> bool:
        self._require_honor()
        plat = self._require_platform()
        try:
            from honor.fan import set_auto

            return bool(set_auto(plat))
        except Exception as exc:  # noqa: BLE001
            log.error("fan set_auto failed: %s", exc)
            return False

    def set_fan_manual(self, speed: int) -> bool:
        self._require_honor()
        plat = self._require_platform()
        try:
            from honor.fan import set_manual

            return bool(set_manual(plat))
        except Exception as exc:  # noqa: BLE001
            log.error("fan set_manual failed: %s", exc)
            return False

    def set_fan_speed(self, speed: int) -> bool:
        self._require_honor()
        plat = self._require_platform()
        try:
            from honor.fan import set_speed

            result = set_speed(speed, plat)
            cmds = result.get("cmds", []) if isinstance(result, dict) else []
            return all(c.get("ok") for c in cmds) if cmds else bool(result)
        except Exception as exc:  # noqa: BLE001
            log.error("fan set_speed failed: %s", exc)
            return False

    def read_fan_temp(self) -> int | None:
        state = self.read_fan()
        return state.temp_mc if state.available else None

    # -- Gestures --

    def read_gestures(self) -> GesturesSnapshot:
        probe = probe_gesture_environment(
            sysfs_root=self._root / "sys/class/hidraw",
            dev_root=self._root / "dev",
        )
        firmware_probe = self.probe_touchpad_firmware()
        return GesturesSnapshot(
            available=probe.available,
            device_found=probe.device_found,
            permission_denied=probe.permission_denied,
            device_path=probe.device_path,
            wmi_transport_present=wmi_transport_present(
                self._root / "sys/bus/wmi/devices"
            ),
            firmware_settings_supported=firmware_probe.available,
            firmware_settings={},
            last_error=probe.error,
        )

    def list_gesture_mappings(self) -> list[GestureEntry]:
        # ConfigStore is the sole source of gesture mappings. Returning an
        # empty base lets ApplicationService merge confirmed built-in defaults
        # with that authoritative state without reading root's home directory.
        return []

    # -- Touchpad firmware --

    def _touchpad_transport(self) -> TouchpadFirmwareTransport:
        return TouchpadFirmwareTransport(
            sysfs_root=self._root / "sys/class/hidraw",
            dev_root=self._root / "dev",
            dmi_root=self._root / "sys/class/dmi/id",
        )

    def probe_touchpad_firmware(self) -> TouchpadFirmwareProbe:
        return probe_touchpad_firmware(
            sysfs_root=self._root / "sys/class/hidraw",
            dev_root=self._root / "dev",
            dmi_root=self._root / "sys/class/dmi/id",
        )

    def apply_touchpad_settings(
        self, settings: Mapping[str, int]
    ) -> TouchpadBatchApplyResult:
        return self._touchpad_transport().apply_settings(settings)

    def query_touchpad_support(self) -> frozenset[int]:
        return self._touchpad_transport().query_supported_gestures()

    # -- GPU --

    def read_gpu(self) -> GpuSnapshot:
        try:
            from honor.gpu import get_status

            status = get_status()
            return GpuSnapshot(
                available=True,
                mitigation_enabled=False,
                irqs=tuple(str(x) for x in status.get("gpu_irqs", [])),
            )
        except Exception as exc:  # noqa: BLE001
            return GpuSnapshot(available=False, last_error=str(exc))

    def apply_gpu_mitigation(self) -> dict[str, Any]:
        self._require_honor()
        plat = self._require_platform()
        try:
            from honor.config import Config
            from honor.gpu import apply_irq_fix

            cfg = Config()
            return apply_irq_fix(cfg, plat)
        except Exception as exc:  # noqa: BLE001
            return {"error": str(exc)}

    def restore_gpu_mitigation(self) -> dict[str, Any]:
        # The honor-tools package does not provide a restore function.
        # This is a known limitation documented in the GPU feature service.
        return {"error": "restore not implemented in honor-tools", "restored": False}

    # -- Helpers --

    @staticmethod
    def _read_int(path: pathlib.Path) -> int | None:
        try:
            return int(path.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            return None

    @staticmethod
    def _write_int(path: pathlib.Path, value: int) -> bool:
        try:
            path.write_text(str(value), encoding="utf-8")
            return True
        except OSError:
            return False


# Late import to avoid circular dependency at module load time.
from honor_control.core.models import ChargeMode  # noqa: E402
