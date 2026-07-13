"""Low-level, typed Linux CLI for the reverse-engineered touchpad controls."""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
import tomllib

from honor_control.backend.touchpad_firmware import (
    TouchpadFirmwareError,
    TouchpadFirmwareTransport,
)
from honor_control.core.touchpad import (
    SUPPORTED_GESTURE_BITS,
    TOUCHPAD_SETTING_SPECS,
    TouchpadSetting,
    encode_touchpad_setting,
    parse_touchpad_setting,
    parse_touchpad_value,
)

WMI_GUID = "ABBC0F5B-8EA1-11D1-A000-C90629100000"
MASTER_DISABLE_COMMAND = 0x00001002
MASTER_ENABLE_COMMAND = 0x00011002


def _emit(payload: object, *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    elif isinstance(payload, str):
        print(payload)
    else:
        print(json.dumps(payload, indent=2, sort_keys=True))


def _settings_payload() -> list[dict[str, object]]:
    result: list[dict[str, object]] = []
    for setting in TouchpadSetting:
        spec = TOUCHPAD_SETTING_SPECS[setting]
        result.append(
            {
                "setting": setting.value,
                "range": [spec.minimum, spec.maximum],
                "labels": list(spec.labels),
                "command": spec.command,
                "companion_commands": list(spec.companion_commands),
                "registry_name": spec.registry_name,
                "helper_message_id": spec.helper_message_id,
            }
        )
    return result


def _master_attribute(root: pathlib.Path) -> pathlib.Path | None:
    try:
        candidates = sorted(
            entry / "touchpad_enabled"
            for entry in root.iterdir()
            if entry.name.upper().startswith(f"{WMI_GUID}-")
            and (entry / "touchpad_enabled").is_file()
        )
    except OSError:
        return None
    return candidates[0] if candidates else None


def _read_master(attribute: pathlib.Path) -> bool:
    value = attribute.read_text(encoding="ascii").strip()
    if value not in {"0", "1"}:
        raise RuntimeError(f"invalid touchpad_enabled readback {value!r}")
    return value == "1"


def _set_master(attribute: pathlib.Path, enabled: bool) -> bool:
    attribute.write_text(f"{int(enabled)}\n", encoding="ascii")
    observed = _read_master(attribute)
    if observed != enabled:
        raise RuntimeError(
            "touchpad master-switch readback did not match the requested value"
        )
    return observed


def _load_profile(
    path: pathlib.Path,
) -> tuple[dict[TouchpadSetting, int], bool | None]:
    """Load and fully validate the narrow touchpad TOML schema."""
    with path.open("rb") as stream:
        document = tomllib.load(stream)
    unknown_tables = sorted(set(document) - {"settings", "master"})
    if unknown_tables:
        raise ValueError(f"unknown profile table(s): {', '.join(unknown_tables)}")

    raw_settings = document.get("settings", {})
    if not isinstance(raw_settings, dict):
        raise ValueError("[settings] must be a TOML table")
    settings: dict[TouchpadSetting, int] = {}
    for name, raw_value in raw_settings.items():
        canonical = parse_touchpad_setting(name)
        if canonical in settings:
            raise ValueError(f"duplicate touchpad setting {canonical.value!r}")
        if not isinstance(raw_value, (str, int, bool)):
            raise ValueError(f"{name} must be a string, integer, or boolean")
        settings[canonical] = parse_touchpad_value(canonical, raw_value)

    master: bool | None = None
    if "master" in document:
        raw_master = document["master"]
        if not isinstance(raw_master, dict):
            raise ValueError("[master] must be a TOML table")
        unknown_master = sorted(set(raw_master) - {"enabled"})
        if unknown_master:
            raise ValueError(
                f"unknown [master] key(s): {', '.join(unknown_master)}"
            )
        if "enabled" not in raw_master or not isinstance(raw_master["enabled"], bool):
            raise ValueError("[master].enabled must be true or false")
        master = raw_master["enabled"]

    if not settings and master is None:
        raise ValueError("profile contains neither [settings] values nor [master]")
    return settings, master


def _encoded_profile_payload(
    settings: dict[TouchpadSetting, int],
    master: bool | None,
) -> dict[str, object]:
    return {
        "master_enabled": master,
        "master_command": (
            None
            if master is None
            else f"0x{(MASTER_ENABLE_COMMAND if master else MASTER_DISABLE_COMMAND):08x}"
        ),
        "settings": [
            {
                "setting": setting.value,
                "value": settings[setting],
                "reports": [
                    report.hex(" ")
                    for report in encode_touchpad_setting(setting, settings[setting])
                ],
            }
            for setting in TouchpadSetting
            if setting in settings
        ],
    }


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="honor-touchpadctl",
        description="Typed Honor 35cc:0104 HID firmware controls",
    )
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument(
        "--sysfs-root",
        type=pathlib.Path,
        default=pathlib.Path("/sys/class/hidraw"),
    )
    parser.add_argument(
        "--dev-root",
        type=pathlib.Path,
        default=pathlib.Path("/dev"),
    )
    parser.add_argument(
        "--dmi-root",
        type=pathlib.Path,
        default=None,
        help="override /sys/class/dmi/id (offline tests only)",
    )
    parser.add_argument(
        "--wmi-root",
        type=pathlib.Path,
        default=pathlib.Path("/sys/bus/wmi/devices"),
    )
    parser.add_argument("--timeout", type=float, default=2.0)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("probe", help="read-only endpoint and descriptor check")
    sub.add_parser("list", help="list every proven public firmware setting")

    encode = sub.add_parser("encode", help="print reports without touching hardware")
    encode.add_argument("setting")
    encode.add_argument("value")

    set_parser = sub.add_parser("set", help="write one validated firmware setting")
    set_parser.add_argument("setting")
    set_parser.add_argument("value")
    set_parser.add_argument(
        "--no-clock-sync",
        action="store_true",
        help="omit the startup/resume epoch handshake",
    )

    apply_parser = sub.add_parser(
        "apply",
        help="validate and apply a complete TOML profile in one HID session",
    )
    apply_parser.add_argument("profile", type=pathlib.Path)
    apply_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="validate and print every setting report without touching hardware",
    )
    apply_parser.add_argument(
        "--no-clock-sync",
        action="store_true",
        help="omit the startup/resume epoch handshake",
    )
    apply_parser.add_argument(
        "--wait",
        type=float,
        default=0.0,
        metavar="SECONDS",
        help="wait for the validated hidraw endpoint after boot or resume",
    )

    support = sub.add_parser("support", help="query the gesture support bitmap")
    support.add_argument(
        "--no-clock-sync",
        action="store_true",
        help="omit the startup/resume epoch handshake",
    )

    master = sub.add_parser(
        "master",
        help="read or change the WMI-backed master switch (kernel module required)",
    )
    master.add_argument("value", nargs="?", choices=("0", "1", "off", "on"))
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        transport = TouchpadFirmwareTransport(
            sysfs_root=args.sysfs_root,
            dev_root=args.dev_root,
            dmi_root=args.dmi_root,
            timeout_seconds=args.timeout,
        )
        if args.command == "probe":
            probe = transport.probe()
            payload = {
                "available": probe.available,
                "platform_verified": probe.platform_verified,
                "dmi_vendor": probe.dmi_vendor,
                "dmi_product": probe.dmi_product,
                "device_found": probe.device_found,
                "permission_denied": probe.permission_denied,
                "descriptor_verified": probe.descriptor_verified,
                "device_path": probe.device_path,
                "report_id": probe.report_id,
                "input_report_bytes": probe.input_report_bytes,
                "output_report_bytes": probe.output_report_bytes,
                "error": probe.error,
                "master_attribute": str(_master_attribute(args.wmi_root) or ""),
            }
            _emit(payload, as_json=args.json)
            return 0 if probe.available else 2

        if args.command == "list":
            _emit(_settings_payload(), as_json=args.json)
            return 0

        if args.command == "encode":
            setting = parse_touchpad_setting(args.setting)
            value = parse_touchpad_value(setting, args.value)
            payload = {
                "setting": setting.value,
                "value": value,
                "reports": [
                    report.hex(" ") for report in encode_touchpad_setting(setting, value)
                ],
            }
            _emit(payload, as_json=args.json)
            return 0

        if args.command == "set":
            result = transport.apply_setting(
                args.setting,
                args.value,
                synchronize_clock=not args.no_clock_sync,
            )
            payload = {
                "setting": result.setting.value,
                "value": result.value,
                "applied": result.applied,
                "reports_applied": result.reports_applied,
                "reports": [report.hex(" ") for report in result.reports],
                "clock_synchronized": result.clock_synchronized,
                "device_path": result.device_path,
                "readback": "unavailable",
            }
            _emit(payload, as_json=args.json)
            return 0

        if args.command == "apply":
            if args.wait < 0:
                raise ValueError("--wait cannot be negative")
            settings, master = _load_profile(args.profile)
            if args.dry_run:
                payload = _encoded_profile_payload(settings, master)
                payload.update({"dry_run": True, "applied": False})
                _emit(payload, as_json=args.json)
                return 0

            attribute = _master_attribute(args.wmi_root)
            master_observed: bool | None = None
            # Enabling first makes the HID endpoint available on firmware that
            # removes it while globally disabled.  Disabling is deliberately
            # last so every requested HID setting is sent before the endpoint
            # can disappear.
            if master is True:
                if attribute is None:
                    raise RuntimeError(
                        "profile requests [master], but honor-touchpad-wmi is not bound"
                    )
                master_observed = _set_master(attribute, True)

            batch = None
            if settings:
                if args.wait:
                    transport.wait_until_available(args.wait)
                batch = transport.apply_settings(
                    settings,
                    synchronize_clock=not args.no_clock_sync,
                )

            if master is False:
                if attribute is None:
                    raise RuntimeError(
                        "profile requests [master], but honor-touchpad-wmi is not bound"
                    )
                master_observed = _set_master(attribute, False)

            payload = _encoded_profile_payload(settings, master)
            payload.update(
                {
                    "applied": True,
                    "master_observed": master_observed,
                    "device_path": batch.device_path if batch else "",
                    "clock_synchronized": (
                        batch.clock_synchronized if batch else False
                    ),
                    "reports_applied": batch.reports_applied if batch else 0,
                    "total_reports": batch.total_reports if batch else 0,
                    "readback": {
                        "master": "verified" if master is not None else "not_requested",
                        "hid_settings": "unavailable",
                    },
                }
            )
            _emit(payload, as_json=args.json)
            return 0

        if args.command == "support":
            bits = transport.query_supported_gestures(
                synchronize_clock=not args.no_clock_sync,
            )
            payload = {
                "supported_bits": sorted(bits),
                "known": {
                    str(bit): SUPPORTED_GESTURE_BITS[bit]
                    for bit in sorted(bits & SUPPORTED_GESTURE_BITS.keys())
                },
                "unknown_bits": sorted(bits - SUPPORTED_GESTURE_BITS.keys()),
            }
            _emit(payload, as_json=args.json)
            return 0

        if args.command == "master":
            attribute = _master_attribute(args.wmi_root)
            if attribute is None:
                raise RuntimeError(
                    "honor-touchpad-wmi is not bound; build and load the supplied module"
                )
            if args.value is not None:
                enabled = "1" if args.value in {"1", "on"} else "0"
                _set_master(attribute, enabled == "1")
            current = _read_master(attribute)
            _emit(
                {"touchpad_enabled": current, "attribute": str(attribute)},
                as_json=args.json,
            )
            return 0
    except (ValueError, OSError, RuntimeError, TouchpadFirmwareError) as exc:
        payload = {"error": str(exc)}
        if isinstance(exc, TouchpadFirmwareError):
            payload.update(
                {
                    "device_path": exc.device_path,
                    "reports_applied": exc.reports_applied,
                    "total_reports": exc.total_reports,
                }
            )
        if args.json:
            _emit(payload, as_json=True)
        else:
            print(f"honor-touchpadctl: {exc}", file=sys.stderr)
        return 1
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
