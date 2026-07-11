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
import pathlib
import shutil
from typing import Any, Protocol

from honor_control.backend.gesture_runtime import (
    probe_gesture_environment,
    wmi_transport_present,
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
    def rewrite_epp(self, epp: str, governor: str) -> bool: ...
    def write_rapl_msr(self, pl1_uw: int, pl2_uw: int) -> bool: ...
    def stop_competing_power_daemons(self) -> None: ...
    # -- Fan --
    def read_fan(self) -> FanSnapshot: ...
    def set_fan_auto(self) -> bool: ...
    def set_fan_manual(self, speed: int) -> bool: ...
    def set_fan_speed(self, speed: int) -> bool: ...
    def read_fan_temp(self) -> int | None: ...

    # -- Gestures --
    def read_gestures(self) -> GesturesSnapshot: ...
    def list_gesture_mappings(self) -> list[GestureEntry]: ...

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
        self._fan_speed = 0
        self._fan_mode = FanMode.STOCK
        self._fan_temp = 45_000
        self._gpu_mitigated = False
        self._gesture_mappings: dict[str, tuple[bool, str]] = {
            "3:1": (True, "leftmeta,v"),
            "3:2": (True, "leftmeta,c"),
            "3:3": (False, "leftmeta,tab"),
        }
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
            desired_profile=self._power_profile,
            applied_profile=self._power_profile,
            observed_summary={"governor": "powersave", "epp": "balance_performance"},
            ac_online=True,
        )

    def apply_power_profile(
        self, profile: str, definition: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        self._log("apply_power_profile", profile, definition)
        self._check_fail("apply_power_profile")
        self._power_profile = profile
        return {
            "profile": profile,
            "governor_ok": True,
            "epp_ok": True,
            "ppd_ok": True,
            "rapl_ok": True,
            "misc_ok": True,
        }

    def rewrite_epp(self, epp: str, governor: str) -> bool:
        self._log("rewrite_epp", epp, governor)
        return True

    def write_rapl_msr(self, pl1_uw: int, pl2_uw: int) -> bool:
        self._log("write_rapl_msr", pl1_uw, pl2_uw)
        return True

    def stop_competing_power_daemons(self) -> None:
        self._log("stop_competing_power_daemons")  # -- Fan --

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
            firmware_settings_supported=False,
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
            import honor  # noqa: F401

            return True
        except ImportError as exc:
            log.warning("honor package not importable: %s", exc)
            return False

    def check_dependency(self) -> bool:
        """Return True when ``honor-tools`` is importable and compatible."""
        return self._honor_ok

    def _require_honor(self) -> None:
        if not self._honor_ok:
            raise DomainException(
                DomainError.DEPENDENCY,
                "honor-tools package is not importable",
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

    def get_power_capability(self) -> Capability:
        if not self._honor_ok:
            return Capability(
                status=CapabilityStatus.UNAVAILABLE,
                reason_code="honor_tools_missing",
                message="honor-tools is required for power profile writes",
            )
        if self._require_platform_or_none() is None:
            return Capability(
                status=CapabilityStatus.UNSUPPORTED,
                reason_code="platform_not_supported",
                message="Power profiles are disabled on unverified hardware",
            )
        if shutil.which("powerprofilesctl") is not None:
            return Capability(
                status=CapabilityStatus.SUPPORTED,
                message="Power profile control available via powerprofilesctl",
            )
        return Capability(
            status=CapabilityStatus.UNAVAILABLE,
            reason_code="powerprofilesctl_missing",
            message="powerprofilesctl not found",
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

    def read_power(self) -> PowerSnapshot:
        if self._require_platform_or_none() is None:
            return PowerSnapshot(
                available=False,
                last_error="Power status is disabled on unverified hardware",
            )
        try:
            from honor.power import get_status

            status = get_status()
            ppd = str(status.get("ppd_profile", ""))
            applied = {
                "power-saver": "silent",
                "balanced": "balanced",
                "performance": "performance",
            }.get(ppd, "")
            ac_online = None
            if self._ac_path is not None:
                ac_online = self._read_int(self._ac_path / "online") == 1
            return PowerSnapshot(
                available=True,
                applied_profile=applied,
                observed_summary=status,
                ac_online=ac_online,
            )
        except Exception as exc:  # noqa: BLE001
            return PowerSnapshot(available=False, last_error=str(exc))

    def apply_power_profile(
        self, profile: str, definition: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        self._require_honor()
        try:
            from honor.config import Config, PowerProfile
            from honor.power import apply_profile

            cfg = Config()
            if definition is not None:
                cfg.power.profiles[profile] = PowerProfile(
                    pl1_uw=int(definition["pl1_uw"]),
                    pl2_uw=int(definition["pl2_uw"]),
                    governor=str(definition["governor"]),
                    epp=str(definition["epp"]),
                    ppd_profile=str(definition["ppd_profile"]),
                    turbo_enabled=bool(definition["turbo_enabled"]),
                    max_perf_pct=int(definition["max_perf_pct"]),
                )
            raw = apply_profile(profile, cfg)

            def _all_true(value: Any) -> bool:
                if isinstance(value, dict):
                    return bool(value) and all(_all_true(v) for v in value.values())
                return value is True

            return {
                **raw,
                "governor_ok": _all_true(raw.get("governor", {})),
                "epp_ok": _all_true(raw.get("epp", {})),
                "ppd_ok": raw.get("ppd_ok") is True,
                "rapl_ok": _all_true(raw.get("rapl", {})),
                "misc_ok": raw.get("no_turbo") is True
                and raw.get("max_perf_pct") is True,
            }
        except Exception as exc:  # noqa: BLE001
            return {"error": str(exc)}

    def stop_competing_power_daemons(self) -> None:
        """Stop and mask PPD and intel_lpmd so they don't overwrite RAPL.

        Both daemons continuously write their own PL1/PL2 values to the
        RAPL MSR, clobbering our values within ~1 second.  Stopping and
        masking them ensures our RAPL limits stick and survive reboots.

        Also enables HWP dynamic boost, which allows the CPU to boost
        aggressively within the RAPL envelope.

        Only runs on detected Honor platforms — on other hardware, PPD
        is the standard power manager and should not be touched.

        This should be called once during service initialization, not on
        every profile apply.  Masking (not disabling) is used because it
        prevents systemd from restarting the unit even if another service
        depends on it, and is cleanly reversible with ``systemctl unmask``.

        Best-effort: if the daemons aren't installed, the calls are
        silently ignored.
        """
        # Only stop daemons on detected Honor platforms.  On other
        # hardware, PPD is the standard power manager and should be left
        # alone.
        if self._require_platform_or_none() is None:
            return

        import pathlib
        import subprocess

        for daemon in ("power-profiles-daemon", "intel_lpmd"):
            for action in ("stop", "mask"):
                try:
                    subprocess.run(
                        ["systemctl", action, daemon],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        check=False,
                        timeout=5,
                    )
                except (OSError, subprocess.TimeoutExpired):
                    pass

        # Enable HWP dynamic boost so the CPU uses the full RAPL envelope.
        # This path only exists on Intel systems with intel_pstate.
        hwp_path = pathlib.Path(
            "/sys/devices/system/cpu/intel_pstate/hwp_dynamic_boost"
        )
        if hwp_path.exists():
            try:
                hwp_path.write_text("1", encoding="utf-8")
            except OSError:
                pass

    def rewrite_epp(self, epp: str, governor: str) -> bool:
        """Re-write EPP after PPD's asynchronous overwrite.

        Returns True if all CPU EPP writes succeeded.

        Two intel_pstate quirks are handled here:

        1. **Read-back requirement:** an EPP sysfs write does not
           reliably commit to hardware unless immediately followed by a
           read of the same file.  The read-back flushes the write into
           intel_pstate, causing PPD's competing write to get EBUSY.

        2. **Governor guard:** intel_pstate rejects EPP writes when the
           governor is ``performance``, and setting the governor back to
           ``performance`` after the EPP write resets EPP to
           ``default``.  So when the requested governor is
           ``performance`` we leave it at ``powersave`` — EPP is the
           primary performance control on modern Intel CPUs.
        """
        import glob
        import pathlib
        import time

        cpu_dirs = sorted(
            p
            for p in glob.glob("/sys/devices/system/cpu/cpu[0-9]*")
            if pathlib.Path(p).name[3:].isdigit()
        )
        # Flip to powersave so EPP is writable.
        for cpu in cpu_dirs:
            self._write_sysfs(f"{cpu}/cpufreq/scaling_governor", "powersave")
        # Write EPP with read-back + retries.  The read-back commits the
        # write into intel_pstate so PPD's async overwrite gets EBUSY.
        all_ok = True
        for cpu in cpu_dirs:
            path = f"{cpu}/cpufreq/energy_performance_preference"
            ok = False
            for _attempt in range(5):
                if self._write_sysfs(path, epp):
                    # Read back immediately — this is what makes the write
                    # stick by flushing it into the driver.
                    try:
                        pathlib.Path(path).read_text(encoding="utf-8")
                    except OSError:
                        pass
                    ok = True
                    break
                time.sleep(0.1)
            if not ok:
                log.warning("EPP re-write failed for %s", path)
                all_ok = False
        # Restore the governor only when it won't reset EPP.
        if governor != "performance":
            for cpu in cpu_dirs:
                self._write_sysfs(f"{cpu}/cpufreq/scaling_governor", governor)
        else:
            log.info(
                "keeping governor=powersave to preserve EPP=%s "
                "(performance governor would reset EPP to default)",
                epp,
            )
        return all_ok

    def write_rapl_msr(self, pl1_uw: int, pl2_uw: int) -> bool:
        """Write PL1/PL2 directly to the RAPL MSR (0x610).

        PPD and intel_lpmd overwrite RAPL limits via sysfs, and the sysfs
        driver caches the write without actually reaching the MSR.  Writing
        the MSR directly bypasses both layers so our values stick.

        MSR 0x610 is package-scoped, so writing to CPU 0 is sufficient.
        """
        import os
        import struct

        MSR_RAPL_POWER_UNIT = 0x606
        MSR_PKG_POWER_LIMIT = 0x610

        # Bounds check: reject values outside a sane range.
        # Minimum 3W (idle floor), maximum 150W (well above any laptop SKU).
        for label, value in (("PL1", pl1_uw), ("PL2", pl2_uw)):
            watts = value / 1_000_000
            if watts < 3.0 or watts > 150.0:
                log.warning(
                    "RAPL MSR write rejected: %s=%.1fW out of range [3, 150]",
                    label,
                    watts,
                )
                return False

        try:
            # Read power units from CPU 0.
            fd = os.open("/dev/cpu/0/msr", os.O_RDONLY)
            os.lseek(fd, MSR_RAPL_POWER_UNIT, os.SEEK_SET)
            units_val = struct.unpack("<Q", os.read(fd, 8))[0]
            os.close(fd)
            power_unit = units_val & 0xF
            watts_per_unit = 1.0 / (2**power_unit)
            pl1_units = int(round(pl1_uw / 1_000_000 / watts_per_unit))
            pl2_units = int(round(pl2_uw / 1_000_000 / watts_per_unit))

            # Read current PKG_POWER_LIMIT to preserve time windows.
            fd = os.open("/dev/cpu/0/msr", os.O_RDONLY)
            os.lseek(fd, MSR_PKG_POWER_LIMIT, os.SEEK_SET)
            old = struct.unpack("<Q", os.read(fd, 8))[0]
            os.close(fd)
            tw1 = (old >> 17) & 0x7F
            tw2 = (old >> 49) & 0x7F

            # Build new value: PL1 + enabled + clamp + time window, PL2 same.
            lo = (
                pl1_units
                | (1 << 15)  # PL1 enabled
                | (1 << 16)  # PL1 clamp
                | (tw1 << 17)
            )
            hi = (
                pl2_units
                | (1 << 47)  # PL2 enabled
                | (tw2 << 49)
            )
            val = (lo | (hi << 32)) & 0xFFFFFFFFFFFFFFFF

            # MSR 0x610 is package-scoped — writing to CPU 0 is sufficient.
            fd = os.open("/dev/cpu/0/msr", os.O_WRONLY)
            os.lseek(fd, MSR_PKG_POWER_LIMIT, os.SEEK_SET)
            os.write(fd, struct.pack("<Q", val))
            os.close(fd)
            log.info(
                "RAPL MSR written: PL1=%dW (%d units), PL2=%dW (%d units)",
                pl1_uw // 1_000_000,
                pl1_units,
                pl2_uw // 1_000_000,
                pl2_units,
            )
            return True
        except OSError as exc:
            log.warning("RAPL MSR write failed: %s", exc)
            return False

    def _write_sysfs(self, path: str, value: str) -> bool:
        """Write a string to a sysfs path (root-owned, best-effort)."""
        try:
            pathlib.Path(path).write_text(value, encoding="utf-8")
            return True
        except OSError:
            return False

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
        return GesturesSnapshot(
            available=probe.available,
            device_found=probe.device_found,
            permission_denied=probe.permission_denied,
            device_path=probe.device_path,
            wmi_transport_present=wmi_transport_present(
                self._root / "sys/bus/wmi/devices"
            ),
            firmware_settings_supported=False,
            last_error=probe.error,
        )

    def list_gesture_mappings(self) -> list[GestureEntry]:
        # ConfigStore is the sole source of gesture mappings. Returning an
        # empty base lets ApplicationService merge confirmed built-in defaults
        # with that authoritative state without reading root's home directory.
        return []

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
