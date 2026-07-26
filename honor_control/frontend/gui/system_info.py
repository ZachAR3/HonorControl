"""Read-only local system information used by the Diagnostics page."""

from __future__ import annotations

import os
import platform
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class LocalSystemInfo:
    """A compact, display-ready view of local Linux system specifications."""

    hostname: str = "Unknown"
    os_name: str = "Unknown"
    kernel: str = "Unknown"
    desktop: str = "Unknown"
    memory: str = "Unknown"
    graphics: str = "Unknown"
    gpu_temperature: str = "Unavailable"
    battery_health: str = "Unavailable"
    battery_cycles: str = "Unavailable"


def probe_local_system(
    *,
    proc_root: Path = Path("/proc"),
    sys_root: Path = Path("/sys"),
    etc_root: Path = Path("/etc"),
    environ: Mapping[str, str] | None = None,
) -> LocalSystemInfo:
    """Collect specs without privileged access or hardware writes."""
    environment = os.environ if environ is None else environ
    return LocalSystemInfo(
        hostname=platform.node() or "Unknown",
        os_name=_os_name(etc_root),
        kernel=platform.release() or "Unknown",
        desktop=_desktop(environment),
        memory=_memory(proc_root),
        graphics=_graphics(sys_root),
        gpu_temperature=_gpu_temperature(sys_root),
        battery_health=_battery_health(sys_root),
        battery_cycles=_battery_cycles(sys_root),
    )


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""


def _read_int(path: Path) -> int | None:
    value = _read(path)
    try:
        return int(value)
    except ValueError:
        return None


def _os_name(etc_root: Path) -> str:
    values: dict[str, str] = {}
    for line in _read(etc_root / "os-release").splitlines():
        key, separator, value = line.partition("=")
        if separator:
            values[key] = value.strip().strip("\"'")
    return values.get("PRETTY_NAME") or values.get("NAME") or "Unknown Linux"


def _desktop(environ: Mapping[str, str]) -> str:
    desktop = environ.get("XDG_CURRENT_DESKTOP") or environ.get("DESKTOP_SESSION", "")
    session_type = environ.get("XDG_SESSION_TYPE", "")
    parts = [part for part in (desktop, session_type) if part]
    return " · ".join(parts) if parts else "Unknown"


def _memory(proc_root: Path) -> str:
    values: dict[str, int] = {}
    for line in _read(proc_root / "meminfo").splitlines():
        key, separator, remainder = line.partition(":")
        if not separator:
            continue
        try:
            values[key] = int(remainder.strip().split()[0])
        except (IndexError, ValueError):
            continue
    total = values.get("MemTotal")
    available = values.get("MemAvailable")
    if total is None:
        return "Unknown"
    used = max(0, total - available) if available is not None else None
    total_gib = total / 1024 / 1024
    if used is None:
        return f"{total_gib:.1f} GiB total"
    return f"{used / 1024 / 1024:.1f} / {total_gib:.1f} GiB used"


def _drm_devices(sys_root: Path) -> list[Path]:
    drm_root = sys_root / "class/drm"
    try:
        cards = sorted(drm_root.glob("card*"))
    except OSError:
        return []
    return [
        card / "device"
        for card in cards
        if card.name.removeprefix("card").isdigit() and (card / "device").exists()
    ]


def _graphics(sys_root: Path) -> str:
    devices: list[str] = []
    for device in _drm_devices(sys_root):
        uevent = {
            key: value
            for line in _read(device / "uevent").splitlines()
            for key, separator, value in (line.partition("="),)
            if separator
        }
        pci_id = uevent.get("PCI_ID", "").lower()
        driver = uevent.get("DRIVER") or Path(uevent.get("DRIVER_PATH", "")).name
        vendor, separator, product = pci_id.partition(":")
        if vendor == "8086":
            label = "Intel integrated graphics"
        elif vendor == "1002":
            label = "AMD graphics"
        elif vendor == "10de":
            label = "NVIDIA graphics"
        else:
            label = "Graphics adapter"
        details = []
        if separator:
            details.append(f"PCI {vendor}:{product}")
        if driver:
            details.append(driver)
        devices.append(f"{label} · {' · '.join(details)}" if details else label)
    return "; ".join(devices) if devices else "Not detected through DRM"


def _gpu_temperature(sys_root: Path) -> str:
    for device in _drm_devices(sys_root):
        for path in sorted(device.glob("hwmon/hwmon*/temp1_input")):
            value = _read_int(path)
            if value is not None and 0 < value < 150_000:
                return f"{value / 1000:.0f} °C"
    return "Unavailable"


def _battery_paths(sys_root: Path) -> list[Path]:
    root = sys_root / "class/power_supply"
    try:
        return sorted(
            path for path in root.glob("BAT*") if path.is_dir() or path.is_symlink()
        )
    except OSError:
        return []


def _battery_health(sys_root: Path) -> str:
    for battery in _battery_paths(sys_root):
        for prefix in ("energy", "charge"):
            full = _read_int(battery / f"{prefix}_full")
            design = _read_int(battery / f"{prefix}_full_design")
            if full is not None and design and design > 0:
                return f"{round(full / design * 100)}% of design capacity"
    return "Unavailable"


def _battery_cycles(sys_root: Path) -> str:
    for battery in _battery_paths(sys_root):
        cycles = _read_int(battery / "cycle_count")
        if cycles is not None:
            return f"{cycles:,}"
    return "Unavailable"
