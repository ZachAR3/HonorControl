"""Frozen domain models (DTOs) shared across all layers.

Every dataclass here is frozen (immutable) so snapshots can be safely
shared between threads and asyncio tasks without copying.  No Qt, D-Bus,
sdbus, or ``honor-tools`` types appear here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from honor_control.contract import API_VERSION, SCHEMA_VERSION
from honor_control.core.errors import CapabilityStatus, OperationStatus


def utc_now() -> datetime:
    """Return the current UTC time."""
    return datetime.now(UTC)


# -- Enums --------------------------------------------------------------------


class ChargeMode(StrEnum):
    """Battery charge-mode presets.

    ``custom`` is a *display* state produced when the threshold pair does
    not match a preset.  It is never sent to ``SetMode``.
    """

    OFF = "off"
    HOME = "home"
    TRAVEL = "travel"
    STORAGE = "storage"
    CUSTOM = "custom"


class FanMode(StrEnum):
    """Explicit fan operating mode."""

    STOCK = "stock"
    CURVE = "curve"
    MANUAL_OVERRIDE = "manual_override"
    FAILED_SAFE = "failed_safe"


class BatteryStatusKind(StrEnum):
    """Kernel-reported battery status."""

    CHARGING = "charging"
    DISCHARGING = "discharging"
    FULL = "full"
    NOT_CHARGING = "not_charging"
    UNKNOWN = "unknown"


class DiagnosticSeverity(StrEnum):
    """Severity of a single diagnostic check result."""

    PASS = "pass"
    WARNING = "warning"
    SKIPPED = "skipped"
    FAIL = "fail"


# -- Charge presets (single source of truth) ---------------------------------

#: Charge-mode presets: name -> (end_threshold, start_threshold).
CHARGE_PRESETS: dict[str, tuple[int, int]] = {
    "off": (100, 95),
    "home": (90, 85),
    "travel": (80, 75),
    "storage": (60, 55),
}


def derive_charge_mode(end: int, start: int) -> ChargeMode:
    """Return the preset matching ``(end, start)`` or ``custom``."""
    for name, pair in CHARGE_PRESETS.items():
        if (end, start) == pair:
            return ChargeMode(name)
    return ChargeMode.CUSTOM


# -- Capability ---------------------------------------------------------------


@dataclass(frozen=True)
class Capability:
    """Why a feature is or is not available.

    ``status`` is the authoritative answer; ``reason_code`` is a stable
    machine-readable string; ``message`` is short user-facing detail;
    ``resources`` lists detected paths/devices when relevant.
    """

    status: CapabilityStatus
    reason_code: str = ""
    message: str = ""
    resources: tuple[str, ...] = field(default_factory=tuple)

    @property
    def writable(self) -> bool:
        """True only when the feature is fully supported for writes."""
        return self.status == CapabilityStatus.SUPPORTED


# -- Operation result ---------------------------------------------------------


@dataclass(frozen=True)
class OperationResult:
    """Structured result of every mutation.

    ``changed`` / ``persisted`` / ``applied`` are independent booleans so
    callers can distinguish "saved to config" from "verified on hardware".
    """

    status: OperationStatus
    code: str = ""
    message: str = ""
    changed: bool = False
    persisted: bool = False
    applied: bool = False
    sequence: int = 0
    details: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def success(
        cls,
        *,
        message: str = "",
        changed: bool = True,
        persisted: bool = False,
        applied: bool = False,
        sequence: int = 0,
        details: dict[str, Any] | None = None,
    ) -> OperationResult:
        return cls(
            status=OperationStatus.SUCCESS,
            message=message,
            changed=changed,
            persisted=persisted,
            applied=applied,
            sequence=sequence,
            details=details or {},
        )

    @classmethod
    def rejected(
        cls,
        *,
        code: str,
        message: str,
        sequence: int = 0,
        details: dict[str, Any] | None = None,
    ) -> OperationResult:
        return cls(
            status=OperationStatus.REJECTED,
            code=code,
            message=message,
            sequence=sequence,
            details=details or {},
        )

    @classmethod
    def partial(
        cls,
        *,
        code: str,
        message: str,
        persisted: bool = False,
        applied: bool = False,
        sequence: int = 0,
        details: dict[str, Any] | None = None,
    ) -> OperationResult:
        return cls(
            status=OperationStatus.PARTIAL,
            code=code,
            message=message,
            changed=True,
            persisted=persisted,
            applied=applied,
            sequence=sequence,
            details=details or {},
        )

    @classmethod
    def unavailable(
        cls,
        *,
        code: str,
        message: str,
        sequence: int = 0,
    ) -> OperationResult:
        return cls(
            status=OperationStatus.UNAVAILABLE,
            code=code,
            message=message,
            sequence=sequence,
        )

    @classmethod
    def failed(
        cls,
        *,
        code: str,
        message: str,
        sequence: int = 0,
        details: dict[str, Any] | None = None,
    ) -> OperationResult:
        return cls(
            status=OperationStatus.FAILED,
            code=code,
            message=message,
            sequence=sequence,
            details=details or {},
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a plain dict for wire/JSON transport."""
        return {
            "status": str(self.status),
            "code": self.code,
            "message": self.message,
            "changed": self.changed,
            "persisted": self.persisted,
            "applied": self.applied,
            "sequence": self.sequence,
            "details": self.details,
        }


# -- Feature snapshots --------------------------------------------------------


@dataclass(frozen=True)
class BatterySnapshot:
    """Observed and desired battery state."""

    available: bool = False
    capacity_percent: int | None = None
    status: BatteryStatusKind | None = None
    ac_online: bool | None = None
    observed_end: int | None = None
    observed_start: int | None = None
    desired_end: int | None = None
    desired_start: int | None = None
    mode: ChargeMode = ChargeMode.CUSTOM
    last_error: str = ""


@dataclass(frozen=True)
class PowerProfileEntry:
    """A profile definition from the registry."""

    name: str
    label: str
    description: str = ""
    pl1_uw: int = 25_000_000
    pl2_uw: int = 35_000_000
    governor: str = "powersave"
    epp: str = "balance_power"
    ppd_profile: str = "balanced"
    turbo_enabled: bool = True
    max_perf_pct: int = 100
    built_in: bool = False


POWER_PROFILES: tuple[PowerProfileEntry, ...] = (
    PowerProfileEntry(
        name="silent",
        label="Silent",
        description="Lowest power and noise",
        pl1_uw=12_000_000,
        pl2_uw=18_000_000,
        governor="powersave",
        epp="power",
        ppd_profile="power-saver",
        built_in=True,
    ),
    PowerProfileEntry(
        name="balanced",
        label="Balanced",
        description="Everyday power and battery use",
        pl1_uw=25_000_000,
        pl2_uw=35_000_000,
        governor="powersave",
        epp="balance_power",
        ppd_profile="balanced",
        built_in=True,
    ),
    PowerProfileEntry(
        name="performance",
        label="Performance",
        description="Maximum sustained power",
        pl1_uw=35_000_000,
        pl2_uw=55_000_000,
        # With active Intel P-State/HWP, the performance governor owns EPP and
        # rejects the independently writable EPP policy.  Keep the PPD-
        # compatible powersave selector and express performance through EPP.
        governor="powersave",
        epp="performance",
        ppd_profile="performance",
        built_in=True,
    ),
)
POWER_PROFILE_NAMES = frozenset(entry.name for entry in POWER_PROFILES)


@dataclass(frozen=True)
class PowerSnapshot:
    """Observed and desired power-profile state."""

    available: bool = False
    desired_profile: str = ""
    applied_profile: str = ""
    observed_summary: dict[str, Any] = field(default_factory=dict)
    ac_online: bool | None = None
    auto_switch_enabled: bool = False
    auto_switch_on_ac: str = ""
    auto_switch_on_battery: str = ""
    auto_switch_on_ac_script: str = ""
    auto_switch_on_battery_script: str = ""
    auto_switch_last_script_status: str = ""
    profiles: tuple[PowerProfileEntry, ...] = field(default_factory=tuple)
    last_error: str = ""


@dataclass(frozen=True)
class FanCurvePoint:
    """A single point on a fan curve (temperature in m°C, speed 0-100)."""

    temp_mc: int
    speed: int


@dataclass(frozen=True)
class FanSnapshot:
    """Observed and desired fan state."""

    available: bool = False
    mode: FanMode = FanMode.STOCK
    desired_mode: FanMode = FanMode.STOCK
    temp_mc: int | None = None
    target_speed: int | None = None
    measured_rpm: int | None = None
    curves: dict[str, str] = field(default_factory=dict)
    manual_expires_at: datetime | None = None
    last_error: str = ""


@dataclass(frozen=True)
class GestureEntry:
    """A single gesture's configuration."""

    id: str
    label: str
    enabled: bool
    mapping: str
    default_mapping: str
    error: str = ""


@dataclass(frozen=True)
class GesturesSnapshot:
    """Observed gesture-daemon and mapping state."""

    available: bool = False
    daemon_enabled: bool = False
    daemon_running: bool = False
    device_found: bool = False
    permission_denied: bool = False
    device_path: str = ""
    reports_seen: int = 0
    gestures_emitted: int = 0
    mappings: tuple[GestureEntry, ...] = field(default_factory=tuple)
    wmi_transport_present: bool = False
    firmware_settings_supported: bool = False
    firmware_settings: dict[str, int] = field(default_factory=dict)
    last_error: str = ""


@dataclass(frozen=True)
class GpuSnapshot:
    """Observed GPU mitigation state."""

    available: bool = False
    mitigation_enabled: bool = False
    target_cpu: int | None = None
    irqs: tuple[str, ...] = field(default_factory=tuple)
    last_error: str = ""


@dataclass(frozen=True)
class DiagnosticCheck:
    """A single diagnostic check result."""

    id: str
    severity: DiagnosticSeverity
    message: str = ""
    detail: str = ""
    remediation: str = ""
    duration_ms: int = 0


@dataclass(frozen=True)
class ServiceHealth:
    """Service-level health summary."""

    version: str = ""
    uptime: int = 0
    overall: str = "healthy"
    controller_health: dict[str, str] = field(default_factory=dict)
    dependency_ok: bool = True
    config_valid: bool = True
    stale_domains: tuple[str, ...] = field(default_factory=tuple)
    last_fault: str = ""


@dataclass(frozen=True)
class PlatformInfo:
    """Detected platform identity and confidence."""

    vendor: str = ""
    product: str = ""
    model: str = ""
    cpu_model: str = ""
    matched: bool = False
    confidence: str = "none"


# -- System snapshot ----------------------------------------------------------


@dataclass(frozen=True)
class SystemSnapshot:
    """The complete observable state of the machine.

    Published by :class:`SnapshotStore` and returned by ``GetSnapshot``.
    ``sequence`` increments only on meaningful changes; ``stale_domains``
    lists domains whose last live read failed (last-known value retained).
    """

    api_version: int = API_VERSION
    schema_version: int = SCHEMA_VERSION
    sequence: int = 0
    observed_at: datetime = field(default_factory=utc_now)
    service: ServiceHealth = field(default_factory=ServiceHealth)
    platform: PlatformInfo = field(default_factory=PlatformInfo)
    capabilities: dict[str, Capability] = field(default_factory=dict)
    battery: BatterySnapshot = field(default_factory=BatterySnapshot)
    power: PowerSnapshot = field(default_factory=PowerSnapshot)
    fan: FanSnapshot = field(default_factory=FanSnapshot)
    gestures: GesturesSnapshot = field(default_factory=GesturesSnapshot)
    gpu: GpuSnapshot = field(default_factory=GpuSnapshot)
    stale_domains: tuple[str, ...] = field(default_factory=tuple)
    errors: tuple[str, ...] = field(default_factory=tuple)
