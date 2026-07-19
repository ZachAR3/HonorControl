"""``honorctl`` — deterministic D-Bus client CLI.

Thin D-Bus client; no hardware/config fallback.  Parser functions return
an exit code; command handlers never call ``sys.exit()`` internally.

Exit codes:
  0  success
  2  usage/validation
  3  unavailable/unsupported
  4  operation partial/failed
  13 not authorized
  69 service unavailable
  70 protocol/internal error
"""

from __future__ import annotations

import argparse
import asyncio
import json
import pathlib
import sys
from typing import Any

from honor_control import __version__
from honor_control.client.errors import ClientError
from honor_control.client.sdbus_client import SdbusClient
from honor_control.core.errors import DomainError, DomainException, TransportError
from honor_control.core.models import OperationStatus
from honor_control.core.touchpad import (
    TOUCHPAD_SETTING_SPECS,
    TouchpadSetting,
    parse_touchpad_setting,
    parse_touchpad_value,
)

#: Exit codes (stable, scriptable).
EXIT_OK = 0
EXIT_USAGE = 2
EXIT_UNAVAILABLE = 3
EXIT_OPERATION_FAILED = 4
EXIT_NOT_AUTHORIZED = 13
EXIT_SERVICE_UNAVAILABLE = 69
EXIT_PROTOCOL = 70


def _exit_for_client_error(exc: ClientError) -> int:
    """Map a :class:`ClientError` to an exit code."""
    return {
        TransportError.SERVICE_UNAVAILABLE: EXIT_SERVICE_UNAVAILABLE,
        TransportError.TIMEOUT: EXIT_SERVICE_UNAVAILABLE,
        TransportError.BUSY: EXIT_SERVICE_UNAVAILABLE,
        TransportError.NOT_AUTHORIZED: EXIT_NOT_AUTHORIZED,
        TransportError.INVALID_REQUEST: EXIT_USAGE,
        TransportError.FEATURE_UNAVAILABLE: EXIT_UNAVAILABLE,
        TransportError.API_MISMATCH: EXIT_PROTOCOL,
        TransportError.INTERNAL: EXIT_PROTOCOL,
    }.get(exc.code, EXIT_PROTOCOL)


def _emit(args: argparse.Namespace, payload: Any, human: str) -> None:
    if args.json:
        print(json.dumps(payload, indent=2, default=str, sort_keys=False))
    else:
        print(human.rstrip())


def _fmt_dict(d: dict[str, Any], indent: int = 0) -> str:
    if not d:
        return "(empty)"
    pad = " " * indent
    lines = []
    for k, v in d.items():
        if isinstance(v, dict):
            lines.append(f"{pad}{k}:")
            lines.append(_fmt_dict(v, indent + 2))
        elif isinstance(v, (list, tuple)):
            lines.append(f"{pad}{k}: {', '.join(str(x) for x in v)}")
        else:
            lines.append(f"{pad}{k}: {v}")
    return "\n".join(lines)


def _milli_c(v: Any) -> str:
    try:
        return f"{int(v) // 1000} °C"
    except (TypeError, ValueError):
        return str(v)


async def _get_client(args: argparse.Namespace):
    """Return a connected client.  Never falls back to a backend."""
    if args.bus == "session":
        # Session bus dev mode: connect to a FakeClient-backed service.
        # In production dev mode, the service itself uses FakeHardware.
        client = SdbusClient(bus_kind="session", timeout=args.timeout)
    else:
        client = SdbusClient(bus_kind="system", timeout=args.timeout)
    try:
        await client.connect()
    except ClientError as exc:
        print(
            f"honorctl: service unavailable: {exc.message}\n"
            "Start the service with: sudo systemctl start honor-control.service",
            file=sys.stderr,
        )
        return None, exc
    return client, None


# -- Command handlers --


async def cmd_status(args: argparse.Namespace, client) -> int:
    snap = await client.get_snapshot()
    payload = {
        "sequence": snap.sequence,
        "platform": {"model": snap.platform.model, "matched": snap.platform.matched},
        "battery": {"available": snap.battery.available},
        "power": {"available": snap.power.available},
        "fan": {"available": snap.fan.available},
        "stale_domains": list(snap.stale_domains),
    }
    human = "\n".join(
        [
            f"Model: {snap.platform.model or '—'} (matched: {snap.platform.matched})",
            f"Sequence: {snap.sequence}",
            f"Stale domains: {', '.join(snap.stale_domains) or 'none'}",
        ]
    )
    _emit(args, payload, human)
    return EXIT_OK


async def cmd_snapshot(args: argparse.Namespace, client) -> int:
    snap = await client.get_snapshot()
    _emit(args, {"sequence": snap.sequence, "api_version": snap.api_version}, "")
    return EXIT_OK


async def cmd_battery_thresholds(args: argparse.Namespace, client) -> int:
    result = await client.set_thresholds(int(args.end), int(args.start))
    _emit(
        args,
        result.to_dict(),
        f"Thresholds {'set' if result.applied else 'NOT set'}: {result.message}",
    )
    return EXIT_OK if result.applied else EXIT_OPERATION_FAILED


async def cmd_battery_mode(args: argparse.Namespace, client) -> int:
    result = await client.set_mode(args.mode)
    _emit(
        args,
        result.to_dict(),
        f"Charge mode {'set' if result.applied else 'NOT set'}: {result.message}",
    )
    return EXIT_OK if result.applied else EXIT_OPERATION_FAILED


async def cmd_power_profile(args: argparse.Namespace, client) -> int:
    if args.name:
        result = await client.set_profile(args.name)
        _emit(
            args,
            result.to_dict(),
            f"Profile {'applied' if result.applied else 'NOT applied'}: {result.message}",
        )
        return EXIT_OK if result.applied else EXIT_OPERATION_FAILED
    snap = await client.get_snapshot()
    profiles = [
        {
            "name": profile.name,
            "label": profile.label,
            "pl1_w": profile.pl1_uw / 1_000_000,
            "pl2_w": profile.pl2_uw / 1_000_000,
            "governor": profile.governor,
            "epp": profile.epp,
            "ppd": profile.ppd_profile,
            "turbo": profile.turbo_enabled,
            "max_perf_pct": profile.max_perf_pct,
        }
        for profile in snap.power.profiles
    ]
    _emit(
        args,
        {"profile": snap.power.applied_profile, "profiles": profiles},
        "\n".join(
            [f"Current profile: {snap.power.applied_profile}"]
            + [
                f"  {item['name']}: PL1 {item['pl1_w']:g} W, "
                f"PL2 {item['pl2_w']:g} W, {item['governor']}, {item['epp']}"
                for item in profiles
            ]
        ),
    )
    return EXIT_OK


async def cmd_power_auto_switch(args: argparse.Namespace, client) -> int:
    if any(
        value is not None
        for value in (
            args.on_ac,
            args.on_battery,
            args.ac_script,
            args.battery_script,
        )
    ):
        snap = await client.get_snapshot()
        result = await client.configure_auto_switch(
            args.enabled == "on"
            if args.enabled is not None
            else snap.power.auto_switch_enabled,
            args.on_ac or snap.power.auto_switch_on_ac,
            args.on_battery or snap.power.auto_switch_on_battery,
            args.ac_script
            if args.ac_script is not None
            else snap.power.auto_switch_on_ac_script,
            args.battery_script
            if args.battery_script is not None
            else snap.power.auto_switch_on_battery_script,
        )
        _emit(args, result.to_dict(), f"Auto-switch: {result.message}")
        return EXIT_OK if result.persisted else EXIT_OPERATION_FAILED
    if args.enabled is not None:
        enabled = args.enabled == "on"
        result = await client.set_auto_switch(enabled)
        _emit(args, result.to_dict(), f"Auto-switch: {result.message}")
        return EXIT_OK if result.persisted else EXIT_OPERATION_FAILED
    snap = await client.get_snapshot()
    _emit(
        args,
        {
            "auto_switch": snap.power.auto_switch_enabled,
            "on_ac": snap.power.auto_switch_on_ac,
            "on_battery": snap.power.auto_switch_on_battery,
            "ac_script": snap.power.auto_switch_on_ac_script,
            "battery_script": snap.power.auto_switch_on_battery_script,
            "last_script_status": snap.power.auto_switch_last_script_status,
        },
        f"Auto-switch: {'on' if snap.power.auto_switch_enabled else 'off'}; "
        f"AC={snap.power.auto_switch_on_ac}, "
        f"battery={snap.power.auto_switch_on_battery}",
    )
    return EXIT_OK


async def cmd_power_profile_save(args: argparse.Namespace, client) -> int:
    result = await client.save_power_profile(
        args.name,
        args.label or args.name.replace("_", " ").replace("-", " ").title(),
        args.description,
        round(args.pl1 * 1_000_000),
        round(args.pl2 * 1_000_000),
        args.governor,
        args.epp,
        args.ppd,
        args.turbo == "on",
        args.max_perf,
    )
    _emit(args, result.to_dict(), f"Power profile: {result.message}")
    return EXIT_OK if result.persisted else EXIT_OPERATION_FAILED


async def cmd_power_profile_delete(args: argparse.Namespace, client) -> int:
    result = await client.delete_power_profile(args.name)
    _emit(args, result.to_dict(), f"Power profile: {result.message}")
    return EXIT_OK if result.persisted else EXIT_OPERATION_FAILED


async def cmd_fan_stock(args: argparse.Namespace, client) -> int:
    result = await client.set_stock_auto()
    _emit(args, result.to_dict(), f"Fan: {result.message}")
    return EXIT_OK if result.applied else EXIT_OPERATION_FAILED


async def cmd_fan_curve(args: argparse.Namespace, client) -> int:
    from honor_control.core.validation import parse_curve

    points = parse_curve(args.curve)
    result = await client.set_curve(
        args.profile, [(p.temp_mc, p.speed) for p in points]
    )
    _emit(args, result.to_dict(), f"Fan curve: {result.message}")
    return EXIT_OK if result.persisted else EXIT_OPERATION_FAILED


async def cmd_fan_manual(args: argparse.Namespace, client) -> int:
    result = await client.set_manual(int(args.speed), int(args.ttl))
    _emit(args, result.to_dict(), f"Fan manual: {result.message}")
    return EXIT_OK if result.applied else EXIT_OPERATION_FAILED


async def cmd_gesture_map(args: argparse.Namespace, client) -> int:
    result = await client.set_mapping(args.id, args.keys)
    _emit(args, result.to_dict(), f"Gesture mapping: {result.message}")
    return EXIT_OK if result.persisted else EXIT_OPERATION_FAILED


async def cmd_gesture_enable(args: argparse.Namespace, client) -> int:
    result = await client.set_enabled(args.id, True)
    _emit(args, result.to_dict(), f"Gesture: {result.message}")
    return EXIT_OK if result.persisted else EXIT_OPERATION_FAILED


async def cmd_gesture_disable(args: argparse.Namespace, client) -> int:
    result = await client.set_enabled(args.id, False)
    _emit(args, result.to_dict(), f"Gesture: {result.message}")
    return EXIT_OK if result.persisted else EXIT_OPERATION_FAILED


async def cmd_gesture_batch(args: argparse.Namespace, client) -> int:
    result = await client.set_all_enabled(args.enabled == "on")
    _emit(args, result.to_dict(), f"Gestures: {result.message}")
    return EXIT_OK if result.persisted else EXIT_OPERATION_FAILED


async def cmd_gesture_daemon(args: argparse.Namespace, client) -> int:
    enabled = args.enabled == "on"
    result = await client.set_daemon_enabled(enabled)
    _emit(args, result.to_dict(), f"Gesture daemon: {result.message}")
    return EXIT_OK if result.applied else EXIT_OPERATION_FAILED


async def cmd_touchpad_probe(args: argparse.Namespace, client) -> int:
    probe = await client.probe_touchpad_firmware()
    _emit(args, probe, _fmt_dict(probe))
    return EXIT_OK if probe.get("available") else EXIT_UNAVAILABLE


async def cmd_touchpad_list(args: argparse.Namespace, client) -> int:
    settings = [
        {
            "setting": setting.value,
            "minimum": TOUCHPAD_SETTING_SPECS[setting].minimum,
            "maximum": TOUCHPAD_SETTING_SPECS[setting].maximum,
            "labels": list(TOUCHPAD_SETTING_SPECS[setting].labels),
        }
        for setting in TouchpadSetting
    ]
    human = "\n".join(
        f"{item['setting']}: {item['minimum']}..{item['maximum']}"
        + (f" ({', '.join(item['labels'])})" if item["labels"] else "")
        for item in settings
    )
    _emit(args, {"settings": settings}, human)
    return EXIT_OK


async def cmd_touchpad_set(args: argparse.Namespace, client) -> int:
    setting = parse_touchpad_setting(args.setting)
    value = parse_touchpad_value(setting, args.value)
    result = await client.set_touchpad_setting(setting.value, value)
    _emit(args, result.to_dict(), f"Touchpad setting: {result.message}")
    return EXIT_OK if result.applied else EXIT_OPERATION_FAILED


async def cmd_touchpad_apply(args: argparse.Namespace, client) -> int:
    from honor_control.cli.touchpadctl import _load_profile

    settings, master = _load_profile(args.profile)
    if master is not None:
        raise DomainException(
            code=DomainError.INVALID_ARGUMENT,
            message=(
                "honorctl touchpad apply does not manage [master]; use "
                "honor-touchpadctl with the optional WMI module"
            ),
        )
    result = await client.apply_touchpad_settings(
        {setting.value: value for setting, value in settings.items()}
    )
    _emit(args, result.to_dict(), f"Touchpad profile: {result.message}")
    return EXIT_OK if result.applied else EXIT_OPERATION_FAILED


async def cmd_touchpad_support(args: argparse.Namespace, client) -> int:
    support = await client.query_touchpad_support()
    _emit(args, support, _fmt_dict(support))
    return EXIT_OK


async def cmd_gpu_enable(args: argparse.Namespace, client) -> int:
    result = await client.set_mitigation_enabled(True)
    _emit(args, result.to_dict(), f"GPU: {result.message}")
    return EXIT_OK if result.applied else EXIT_OPERATION_FAILED


async def cmd_gpu_disable(args: argparse.Namespace, client) -> int:
    result = await client.set_mitigation_enabled(False)
    _emit(args, result.to_dict(), f"GPU: {result.message}")
    return EXIT_OK if result.applied else EXIT_OPERATION_FAILED


async def cmd_diag_checks(args: argparse.Namespace, client) -> int:
    result = await client.run_checks()
    _emit(args, result, _fmt_dict(result))
    overall = result.get("overall", "fail")
    return EXIT_OK if overall == "pass" else EXIT_OPERATION_FAILED


async def cmd_diag_bundle(args: argparse.Namespace, client) -> int:
    bundle = await client.get_debug_bundle()
    if args.output:
        import pathlib

        pathlib.Path(args.output).write_text(
            json.dumps(bundle, indent=2, default=str), encoding="utf-8"
        )
        print(f"Debug bundle written to {args.output}")
    else:
        _emit(args, bundle, _fmt_dict(bundle))
    return EXIT_OK


async def cmd_diag_logs(args: argparse.Namespace, client) -> int:
    lines = await client.get_recent_logs(int(args.lines))
    if args.json:
        print(json.dumps({"lines": lines}, indent=2))
    else:
        for line in lines:
            print(line)
    return EXIT_OK


async def cmd_reload(args: argparse.Namespace, client) -> int:
    result = await client.reload()
    _emit(args, result.to_dict(), f"Reload: {result.message}")
    return (
        EXIT_OK if result.status == OperationStatus.SUCCESS else EXIT_OPERATION_FAILED
    )


async def cmd_version(args: argparse.Namespace, client) -> int:
    api_version = await client.get_api_version()
    _emit(
        args,
        {"api_version": api_version, "client_version": __version__},
        f"API v{api_version}",
    )
    return EXIT_OK


# -- Parser --


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="honorctl",
        description="Command-line control for Honor MagicBook laptops "
        "(talks to the honor-control D-Bus service).",
    )
    p.add_argument("--version", action="version", version=f"honorctl {__version__}")
    p.add_argument(
        "--json", action="store_true", help="emit JSON instead of human text"
    )
    p.add_argument(
        "--bus",
        choices=["system", "session"],
        default="system",
        help="D-Bus to use (default: system)",
    )
    p.add_argument(
        "--timeout",
        type=float,
        default=10.0,
        help="per-call timeout in seconds (default: 10)",
    )
    sub = p.add_subparsers(dest="command", required=True, metavar="COMMAND")

    sub.add_parser("status", help="overall status summary").set_defaults(
        func=cmd_status
    )
    sub.add_parser("snapshot", help="fetch one system snapshot").set_defaults(
        func=cmd_snapshot
    )
    sub.add_parser("version", help="show API/client versions").set_defaults(
        func=cmd_version
    )

    bat = sub.add_parser("battery", help="battery / charge-limit controls")
    bat_sub = bat.add_subparsers(dest="subcommand", required=True, metavar="ACTION")
    bt = bat_sub.add_parser("thresholds", help="set charge thresholds (END START)")
    bt.add_argument("end", type=int, help="end threshold (40-100)")
    bt.add_argument("start", type=int, help="start threshold (40-100)")
    bt.set_defaults(func=cmd_battery_thresholds)
    bm = bat_sub.add_parser("mode", help="set charge mode preset")
    bm.add_argument("mode", choices=["off", "home", "travel", "storage"])
    bm.set_defaults(func=cmd_battery_mode)

    power = sub.add_parser("power", help="power profile controls")
    pw_sub = power.add_subparsers(dest="subcommand", required=True, metavar="ACTION")
    pp = pw_sub.add_parser("profile", help="show or apply power profile")
    pp.add_argument("name", nargs="?", help="profile to apply (omit to show current)")
    pp.set_defaults(func=cmd_power_profile)
    pw_as = pw_sub.add_parser("auto-switch", help="show or toggle auto-switching")
    pw_as.add_argument("enabled", nargs="?", choices=["on", "off"])
    pw_as.add_argument("--on-ac", help="profile to apply on AC")
    pw_as.add_argument("--on-battery", help="profile to apply on battery")
    pw_as.add_argument("--ac-script", help="absolute script command after AC switch")
    pw_as.add_argument(
        "--battery-script", help="absolute script command after battery switch"
    )
    pw_as.set_defaults(func=cmd_power_auto_switch)
    pw_save = pw_sub.add_parser("save", help="create or update a power profile")
    pw_save.add_argument("name")
    pw_save.add_argument("--label")
    pw_save.add_argument("--description", default="")
    pw_save.add_argument("--pl1", required=True, type=float, help="sustained watts")
    pw_save.add_argument("--pl2", required=True, type=float, help="burst watts")
    pw_save.add_argument(
        "--governor", choices=["powersave", "performance"], default="powersave"
    )
    pw_save.add_argument(
        "--epp",
        choices=[
            "power",
            "balance_power",
            "balance_performance",
            "performance",
            "default",
        ],
        default="balance_power",
    )
    pw_save.add_argument(
        "--ppd",
        choices=["power-saver", "balanced", "performance"],
        default="balanced",
    )
    pw_save.add_argument("--turbo", choices=["on", "off"], default="on")
    pw_save.add_argument("--max-perf", type=int, default=100, metavar="PERCENT")
    pw_save.set_defaults(func=cmd_power_profile_save)
    pw_delete = pw_sub.add_parser("delete", help="delete a custom power profile")
    pw_delete.add_argument("name")
    pw_delete.set_defaults(func=cmd_power_profile_delete)

    fan = sub.add_parser("fan", help="fan controls")
    fn_sub = fan.add_subparsers(dest="subcommand", required=True, metavar="ACTION")
    fn_sub.add_parser("stock", help="restore stock auto mode").set_defaults(
        func=cmd_fan_stock
    )
    fc = fn_sub.add_parser("curve", help="set fan curve")
    fc.add_argument("curve", help="temp:speed,... (temp in m°C)")
    fc.add_argument("-p", "--profile", default="default", help="profile name")
    fc.set_defaults(func=cmd_fan_curve)
    fm = fn_sub.add_parser("manual", help="set manual fan speed")
    fm.add_argument("speed", type=int, help="speed 0-100")
    fm.add_argument("--ttl", type=int, default=300, help="TTL in seconds (default 300)")
    fm.set_defaults(func=cmd_fan_manual)

    ges = sub.add_parser("gestures", help="touchpad gesture controls")
    ge_sub = ges.add_subparsers(dest="subcommand", required=True, metavar="ACTION")
    gm = ge_sub.add_parser("map", help="set a gesture's key combo")
    gm.add_argument("id", help="gesture id")
    gm.add_argument("keys", help="comma-separated key combo")
    gm.set_defaults(func=cmd_gesture_map)
    ge = ge_sub.add_parser("enable", help="enable a gesture")
    ge.add_argument("id")
    ge.set_defaults(func=cmd_gesture_enable)
    gd = ge_sub.add_parser("disable", help="disable a gesture")
    gd.add_argument("id")
    gd.set_defaults(func=cmd_gesture_disable)
    gb = ge_sub.add_parser("batch", help="enable/disable all gestures")
    gb.add_argument("enabled", choices=["on", "off"])
    gb.set_defaults(func=cmd_gesture_batch)
    daemon = ge_sub.add_parser("daemon", help="start/stop gesture dispatch")
    daemon.add_argument("enabled", choices=["on", "off"])
    daemon.set_defaults(func=cmd_gesture_daemon)

    touchpad = sub.add_parser("touchpad", help="touchpad firmware controls")
    tp_sub = touchpad.add_subparsers(dest="subcommand", required=True, metavar="ACTION")
    tp_sub.add_parser("probe", help="check DMI, descriptor, and access").set_defaults(
        func=cmd_touchpad_probe
    )
    tp_sub.add_parser("list", help="list typed firmware settings").set_defaults(
        func=cmd_touchpad_list
    )
    tp_set = tp_sub.add_parser("set", help="apply one firmware setting")
    tp_set.add_argument("setting")
    tp_set.add_argument("value")
    tp_set.set_defaults(func=cmd_touchpad_set)
    tp_apply = tp_sub.add_parser("apply", help="apply a TOML settings profile")
    tp_apply.add_argument("profile", type=pathlib.Path)
    tp_apply.set_defaults(func=cmd_touchpad_apply)
    tp_sub.add_parser("support", help="query firmware capability bitmap").set_defaults(
        func=cmd_touchpad_support
    )

    gpu = sub.add_parser("gpu", help="GPU mitigation controls")
    gpu_sub = gpu.add_subparsers(dest="subcommand", required=True, metavar="ACTION")
    gpu_sub.add_parser("enable", help="enable GPU mitigation").set_defaults(
        func=cmd_gpu_enable
    )
    gpu_sub.add_parser("disable", help="disable GPU mitigation").set_defaults(
        func=cmd_gpu_disable
    )

    diag = sub.add_parser("diagnostics", help="diagnostics")
    dg_sub = diag.add_subparsers(dest="subcommand", required=True, metavar="ACTION")
    dg_sub.add_parser("checks", help="run diagnostic checks").set_defaults(
        func=cmd_diag_checks
    )
    dg_export = dg_sub.add_parser("export", help="export debug bundle")
    dg_export.add_argument("--output", "-o", help="write to file instead of stdout")
    dg_export.set_defaults(func=cmd_diag_bundle)
    dg_logs = dg_sub.add_parser("logs", help="show recent logs")
    dg_logs.add_argument("lines", nargs="?", type=int, default=50)
    dg_logs.set_defaults(func=cmd_diag_logs)

    sub.add_parser("reload", help="reload config from disk").set_defaults(
        func=cmd_reload
    )
    return p


async def _async_main(args: argparse.Namespace) -> int:
    """Run one command with the client confined to one event loop."""
    client, exc = await _get_client(args)
    if client is None:
        assert exc is not None
        return _exit_for_client_error(exc)
    try:
        return await args.func(args, client)
    except DomainException as exc:
        print(f"honorctl: {exc.message}", file=sys.stderr)
        if exc.code.value == "not_authorized":
            return EXIT_NOT_AUTHORIZED
        if exc.code.value == "invalid_argument":
            return EXIT_USAGE
        return EXIT_OPERATION_FAILED
    except ClientError as exc:
        print(f"honorctl: {exc.message}", file=sys.stderr)
        return _exit_for_client_error(exc)
    finally:
        await client.close()


def main(argv: list[str] | None = None) -> int:
    """Console-script + ``python -m`` entry point for the CLI."""
    parser = build_parser()
    args = parser.parse_args(argv)
    return asyncio.run(_async_main(args))


if __name__ == "__main__":
    raise SystemExit(main())
