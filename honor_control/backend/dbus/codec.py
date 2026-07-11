"""DTO ↔ D-Bus conversion: the single wire boundary.

Converts domain models to ``a{sv}``-shaped return values and back.
No other module performs this conversion, so the wire representation
is materialized in exactly one place.

Wire format note: this build of sdbus has no ``Variant`` wrapper class;
a D-Bus variant is a ``(signature, value)`` tuple.
"""

from __future__ import annotations

from typing import Any

from honor_control.core.models import (
    OperationResult,
    SystemSnapshot,
)


def to_variant(value: Any) -> tuple[str, Any]:
    """Wrap a Python scalar/container as a ``(signature, value)`` variant."""
    if isinstance(value, bool):
        return ("b", value)
    if isinstance(value, int):
        return ("i", value) if -(2**31) <= value < 2**31 else ("x", value)
    if isinstance(value, float):
        return ("d", value)
    if isinstance(value, str):
        return ("s", value)
    if isinstance(value, dict):
        return ("a{sv}", to_vardict(value))
    if isinstance(value, (list, tuple)):
        if value and all(isinstance(x, dict) for x in value):
            return ("aa{sv}", [to_vardict(x) for x in value])
        return ("as", [str(x) for x in value])
    if value is None:
        # Use an empty string variant for None; callers must check.
        return ("s", "")
    return ("s", str(value))


def to_vardict(d: dict | None) -> dict:
    """Convert a plain dict into an ``a{sv}``-shaped return value."""
    if not d:
        return {}
    return {str(k): to_variant(v) for k, v in d.items() if v is not None}


def to_varlist(items: list[dict] | None) -> list:
    """Convert a list of dicts into an ``aa{sv}``-shaped return value."""
    if not items:
        return []
    return [to_vardict(x) for x in items]


def from_variant(value: Any) -> Any:
    """Unwrap a ``(signature, value)`` variant to a plain Python value."""
    if isinstance(value, tuple) and len(value) == 2:
        return from_variant(value[1])
    if isinstance(value, dict):
        return {str(k): from_variant(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [from_variant(v) for v in value]
    return value


def snapshot_to_vardict(snap: SystemSnapshot) -> dict:
    """Convert a :class:`SystemSnapshot` to an ``a{sv}`` wire dict."""
    result = to_vardict(
        {
            "api_version": snap.api_version,
            "schema_version": snap.schema_version,
            "sequence": snap.sequence,
            "observed_at": snap.observed_at.isoformat(),
            "service": _service_to_dict(snap),
            "platform": _platform_to_dict(snap),
            "capabilities": _capabilities_to_dict(snap),
            "battery": _battery_to_dict(snap),
            "power": _power_to_dict(snap),
            "fan": _fan_to_dict(snap),
            "gestures": _gestures_to_dict(snap),
            "gpu": _gpu_to_dict(snap),
            "stale_domains": list(snap.stale_domains),
            "errors": list(snap.errors),
        }
    )
    result["api_version"] = ("u", snap.api_version)
    result["schema_version"] = ("u", snap.schema_version)
    result["sequence"] = ("t", snap.sequence)
    return result


def operation_result_to_vardict(result: OperationResult) -> dict:
    """Convert an :class:`OperationResult` to an ``a{sv}`` wire dict."""
    wire = to_vardict(result.to_dict())
    wire["sequence"] = ("t", result.sequence)
    return wire


def _service_to_dict(snap: SystemSnapshot) -> dict[str, Any]:
    return {
        "version": snap.service.version,
        "uptime": snap.service.uptime,
        "overall": snap.service.overall,
        "controller_health": dict(snap.service.controller_health),
        "dependency_ok": snap.service.dependency_ok,
        "config_valid": snap.service.config_valid,
        "stale_domains": list(snap.service.stale_domains),
        "last_fault": snap.service.last_fault,
    }


def _platform_to_dict(snap: SystemSnapshot) -> dict[str, Any]:
    return {
        "vendor": snap.platform.vendor,
        "product": snap.platform.product,
        "model": snap.platform.model,
        "cpu_model": snap.platform.cpu_model,
        "matched": snap.platform.matched,
        "confidence": snap.platform.confidence,
    }


def _capabilities_to_dict(snap: SystemSnapshot) -> dict[str, Any]:
    return {
        name: {
            "status": str(cap.status),
            "reason_code": cap.reason_code,
            "message": cap.message,
            "resources": list(cap.resources),
        }
        for name, cap in snap.capabilities.items()
    }


def _battery_to_dict(snap: SystemSnapshot) -> dict[str, Any]:
    bat = snap.battery
    return {
        "available": bat.available,
        "capacity_percent": bat.capacity_percent,
        "status": str(bat.status) if bat.status else "",
        "ac_online": bat.ac_online,
        "observed_end": bat.observed_end,
        "observed_start": bat.observed_start,
        "desired_end": bat.desired_end,
        "desired_start": bat.desired_start,
        "mode": str(bat.mode),
        "last_error": bat.last_error,
    }


def _power_to_dict(snap: SystemSnapshot) -> dict[str, Any]:
    pw = snap.power
    return {
        "available": pw.available,
        "desired_profile": pw.desired_profile,
        "applied_profile": pw.applied_profile,
        "observed_summary": pw.observed_summary,
        "ac_online": pw.ac_online,
        "auto_switch_enabled": pw.auto_switch_enabled,
        "auto_switch_on_ac": pw.auto_switch_on_ac,
        "auto_switch_on_battery": pw.auto_switch_on_battery,
        "auto_switch_on_ac_script": pw.auto_switch_on_ac_script,
        "auto_switch_on_battery_script": pw.auto_switch_on_battery_script,
        "auto_switch_last_script_status": pw.auto_switch_last_script_status,
        "profiles": [
            {
                "name": profile.name,
                "label": profile.label,
                "description": profile.description,
                "pl1_uw": profile.pl1_uw,
                "pl2_uw": profile.pl2_uw,
                "governor": profile.governor,
                "epp": profile.epp,
                "ppd_profile": profile.ppd_profile,
                "turbo_enabled": profile.turbo_enabled,
                "max_perf_pct": profile.max_perf_pct,
                "built_in": profile.built_in,
            }
            for profile in pw.profiles
        ],
        "last_error": pw.last_error,
    }


def _fan_to_dict(snap: SystemSnapshot) -> dict[str, Any]:
    fan = snap.fan
    return {
        "available": fan.available,
        "mode": str(fan.mode),
        "desired_mode": str(fan.desired_mode),
        "temp_mc": fan.temp_mc,
        "target_speed": fan.target_speed,
        "measured_rpm": fan.measured_rpm,
        "curves": dict(fan.curves),
        "manual_expires_at": fan.manual_expires_at.isoformat()
        if fan.manual_expires_at
        else "",
        "last_error": fan.last_error,
    }


def _gestures_to_dict(snap: SystemSnapshot) -> dict[str, Any]:
    ges = snap.gestures
    return {
        "available": ges.available,
        "daemon_enabled": ges.daemon_enabled,
        "daemon_running": ges.daemon_running,
        "device_found": ges.device_found,
        "permission_denied": ges.permission_denied,
        "device_path": ges.device_path,
        "reports_seen": ges.reports_seen,
        "gestures_emitted": ges.gestures_emitted,
        "mappings": [
            {
                "id": m.id,
                "label": m.label,
                "enabled": m.enabled,
                "mapping": m.mapping,
                "default_mapping": m.default_mapping,
                "error": m.error,
            }
            for m in ges.mappings
        ],
        "wmi_transport_present": ges.wmi_transport_present,
        "firmware_settings_supported": ges.firmware_settings_supported,
        "last_error": ges.last_error,
    }


def _gpu_to_dict(snap: SystemSnapshot) -> dict[str, Any]:
    gpu = snap.gpu
    return {
        "available": gpu.available,
        "mitigation_enabled": gpu.mitigation_enabled,
        "target_cpu": gpu.target_cpu,
        "irqs": list(gpu.irqs),
        "last_error": gpu.last_error,
    }
