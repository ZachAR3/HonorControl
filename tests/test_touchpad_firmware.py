"""Golden vectors and offline transport tests for Honor touchpad firmware."""

from __future__ import annotations

import os
import pathlib

import pytest

from honor_control.backend.touchpad_firmware import (
    TouchpadFirmwareError,
    TouchpadFirmwareTransport,
    parse_vendor_collection_descriptor,
    probe_touchpad_firmware,
)
from honor_control.cli.touchpadctl import _load_profile, main as touchpadctl_main
from honor_control.core.touchpad import (
    SUPPORTED_GESTURE_BITS,
    TOUCHPAD_SETTING_SPECS,
    TouchpadSetting,
    decode_support_report,
    encode_touchpad_setting,
    make_support_query_report,
    make_timestamp_report,
    parse_touchpad_setting,
    parse_touchpad_value,
)


VENDOR_DESCRIPTOR = bytes.fromhex(
    # Usage Page (Vendor 0xff00), Usage 1, Application collection
    "06 00 ff 09 01 a1 01 "
    # Report ID 0x0e, eight one-byte input fields, eight output fields
    "85 0e 75 08 95 08 81 02 91 02 c0"
)


def _fake_device(tmp_path: pathlib.Path) -> tuple[pathlib.Path, pathlib.Path]:
    sysfs = tmp_path / "sys/class/hidraw"
    dmi = tmp_path / "sys/class/dmi/id"
    dev = tmp_path / "dev"
    device = sysfs / "hidraw7/device"
    device.mkdir(parents=True)
    dmi.mkdir(parents=True)
    dev.mkdir()
    (dmi / "sys_vendor").write_text("HONOR\n", encoding="utf-8")
    (dmi / "product_name").write_text("MRA-XXX\n", encoding="utf-8")
    (device / "uevent").write_text(
        "HID_ID=0018:000035CC:00000104\n",
        encoding="utf-8",
    )
    (device / "report_descriptor").write_bytes(VENDOR_DESCRIPTOR)
    (dev / "hidraw7").write_bytes(b"")
    return sysfs, dev


def test_every_public_setting_has_an_exact_golden_vector() -> None:
    expected = {
        TouchpadSetting.SENSITIVITY: ("0e0101000000000000",),
        TouchpadSetting.VIBRATION_INTENSITY: ("0e0201000000000000",),
        TouchpadSetting.PRESS_TEXT: ("0e0301000000000000",),
        TouchpadSetting.PRESS_PICTURE: ("0e0401000000000000",),
        TouchpadSetting.THREE_FINGER_DRAG: (
            "0e0501000000000000",
            "0e1101000000000000",
        ),
        TouchpadSetting.MOUSE_LIKE_MODE: ("0e0601000000000000",),
        TouchpadSetting.EDGE_BRIGHTNESS: ("0e0701000000000000",),
        TouchpadSetting.EDGE_VOLUME: ("0e0801000000000000",),
        TouchpadSetting.EDGE_CONTROL_CENTER: ("0e0901000000000000",),
        TouchpadSetting.EDGE_CLOSE_OR_MINIMIZE: ("0e0a01000000000000",),
        TouchpadSetting.KNUCKLE_SCREENSHOT: ("0e0b01000000000000",),
        TouchpadSetting.KNUCKLE_SCREEN_RECORD: ("0e0c01000000000000",),
    }
    assert set(expected) == set(TOUCHPAD_SETTING_SPECS)
    for setting, vectors in expected.items():
        assert (
            tuple(report.hex() for report in encode_touchpad_setting(setting, 1))
            == vectors
        )


def test_value_names_and_legacy_aliases_are_bounded() -> None:
    assert parse_touchpad_setting("shock") is TouchpadSetting.VIBRATION_INTENSITY
    assert parse_touchpad_value("sensitivity", "low") == 0
    assert parse_touchpad_value("sensitivity", "high") == 1
    assert parse_touchpad_value("shock", "medium") == 1
    assert parse_touchpad_value("shock", "high") == 2
    assert parse_touchpad_value("edge-volume", "on") == 1
    with pytest.raises(ValueError):
        parse_touchpad_value("sensitivity", 2)
    with pytest.raises(ValueError):
        parse_touchpad_value("shock", 3)
    with pytest.raises(ValueError):
        parse_touchpad_value("shock", True)
    with pytest.raises(ValueError):
        parse_touchpad_value("shock", "on")
    with pytest.raises(ValueError):
        parse_touchpad_setting("raw_command")


def test_lifecycle_and_query_vectors() -> None:
    assert make_timestamp_report(0x0102030405).hex() == "0e0005040302010000"
    assert make_support_query_report().hex() == "0ef000000000000000"


def test_support_bitmap_uses_little_endian_bit_indices() -> None:
    report = bytearray.fromhex("0ef000000000000000")
    for bit in (1, 17, 27):
        report[2 + bit // 8] |= 1 << (bit % 8)
    assert decode_support_report(bytes(report)) == frozenset({1, 17, 27})
    assert {SUPPORTED_GESTURE_BITS[bit] for bit in (1, 17, 27)} == {
        "one_knuckle_double_tap",
        "single_finger_heavy_pressure",
        "three_finger_light_touch",
    }


def test_descriptor_parser_verifies_report_id_and_both_directions() -> None:
    caps = parse_vendor_collection_descriptor(VENDOR_DESCRIPTOR)
    assert caps.found is True
    assert caps.report_id == 0x0E
    assert caps.input_report_bytes == 9
    assert caps.output_report_bytes == 9
    assert caps.writable is True

    wrong_size = VENDOR_DESCRIPTOR.replace(
        bytes.fromhex("95 08"), bytes.fromhex("95 07")
    )
    assert parse_vendor_collection_descriptor(wrong_size).writable is False

    not_application = VENDOR_DESCRIPTOR.replace(
        bytes.fromhex("a1 01"),
        bytes.fromhex("a1 00"),
    )
    assert parse_vendor_collection_descriptor(not_application).writable is False


def test_probe_requires_descriptor_not_just_vid_pid(tmp_path: pathlib.Path) -> None:
    sysfs, dev = _fake_device(tmp_path)
    probe = probe_touchpad_firmware(sysfs_root=sysfs, dev_root=dev)
    assert probe.available is True
    assert probe.descriptor_verified is True
    assert probe.device_path == str(dev / "hidraw7")

    (sysfs / "hidraw7/device/report_descriptor").write_bytes(b"\x05\x01")
    rejected = probe_touchpad_firmware(sysfs_root=sysfs, dev_root=dev)
    assert rejected.available is False
    assert "descriptor verification failed" in rejected.error


def test_probe_fails_closed_on_wrong_dmi(tmp_path: pathlib.Path) -> None:
    sysfs, dev = _fake_device(tmp_path)
    (tmp_path / "sys/class/dmi/id/product_name").write_text(
        "UnknownBook\n",
        encoding="utf-8",
    )
    probe = probe_touchpad_firmware(sysfs_root=sysfs, dev_root=dev)
    assert probe.available is False
    assert probe.platform_verified is False
    assert "expected exact DMI HONOR/MRA-XXX" in probe.error


def test_probe_fails_closed_on_wrong_vid_pid(tmp_path: pathlib.Path) -> None:
    sysfs, dev = _fake_device(tmp_path)
    (sysfs / "hidraw7/device/uevent").write_text(
        "HID_ID=0018:000035CC:00009999\n",
        encoding="utf-8",
    )
    probe = probe_touchpad_firmware(sysfs_root=sysfs, dev_root=dev)
    assert probe.available is False
    assert probe.device_found is False


def test_apply_three_finger_drag_is_one_handshake_plus_two_reports(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Linux defines these flags.  Supplying zero-valued stand-ins lets this
    # pure file-I/O test also run on the Windows analysis host.
    monkeypatch.setattr(os, "O_NONBLOCK", getattr(os, "O_NONBLOCK", 0), raising=False)
    monkeypatch.setattr(os, "O_CLOEXEC", getattr(os, "O_CLOEXEC", 0), raising=False)
    sysfs, dev = _fake_device(tmp_path)
    transport = TouchpadFirmwareTransport(sysfs_root=sysfs, dev_root=dev)

    result = transport.apply_setting(
        TouchpadSetting.THREE_FINGER_DRAG,
        True,
        epoch_seconds=0x0102030405,
    )

    assert result.applied is True
    assert result.reports_applied == 2
    assert (dev / "hidraw7").read_bytes() == bytes.fromhex(
        "0e00050403020100000e05010000000000000e1101000000000000"
    )


def test_batch_validates_then_uses_stable_order_and_one_handshake(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(os, "O_NONBLOCK", getattr(os, "O_NONBLOCK", 0), raising=False)
    monkeypatch.setattr(os, "O_CLOEXEC", getattr(os, "O_CLOEXEC", 0), raising=False)
    sysfs, dev = _fake_device(tmp_path)
    transport = TouchpadFirmwareTransport(sysfs_root=sysfs, dev_root=dev)

    result = transport.apply_settings(
        {
            TouchpadSetting.EDGE_VOLUME: False,
            TouchpadSetting.SENSITIVITY: "high",
            TouchpadSetting.THREE_FINGER_DRAG: True,
        },
        epoch_seconds=0x0102030405,
    )

    assert result.applied is True
    assert [item.setting for item in result.settings] == [
        TouchpadSetting.SENSITIVITY,
        TouchpadSetting.THREE_FINGER_DRAG,
        TouchpadSetting.EDGE_VOLUME,
    ]
    assert result.reports_applied == result.total_reports == 4
    assert (dev / "hidraw7").read_bytes() == bytes.fromhex(
        "0e0005040302010000"
        "0e0101000000000000"
        "0e0501000000000000"
        "0e1101000000000000"
        "0e0800000000000000"
    )


def test_profile_loader_rejects_raw_or_unknown_fields(tmp_path: pathlib.Path) -> None:
    valid = tmp_path / "valid.toml"
    valid.write_text(
        "[settings]\nshock = 'high'\nedge_volume = false\n[master]\nenabled = true\n",
        encoding="utf-8",
    )
    settings, master = _load_profile(valid)
    assert settings == {
        TouchpadSetting.VIBRATION_INTENSITY: 2,
        TouchpadSetting.EDGE_VOLUME: 0,
    }
    assert master is True

    invalid = tmp_path / "invalid.toml"
    invalid.write_text("[settings]\nraw_command = '0e ff'\n", encoding="utf-8")
    with pytest.raises(ValueError):
        _load_profile(invalid)

    invalid.write_text("[transport]\ncommand = 1\n", encoding="utf-8")
    with pytest.raises(ValueError):
        _load_profile(invalid)


def test_apply_profile_cli_writes_hid_and_verifies_master(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(os, "O_NONBLOCK", getattr(os, "O_NONBLOCK", 0), raising=False)
    monkeypatch.setattr(os, "O_CLOEXEC", getattr(os, "O_CLOEXEC", 0), raising=False)
    sysfs, dev = _fake_device(tmp_path)
    wmi_root = tmp_path / "sys/bus/wmi/devices"
    attribute = wmi_root / ("ABBC0F5B-8EA1-11D1-A000-C90629100000-0/touchpad_enabled")
    attribute.parent.mkdir(parents=True)
    attribute.write_text("0\n", encoding="ascii")
    profile = tmp_path / "profile.toml"
    profile.write_text(
        "[settings]\nsensitivity = 'high'\nthree_finger_drag = true\n"
        "[master]\nenabled = true\n",
        encoding="utf-8",
    )

    result = touchpadctl_main(
        [
            "--sysfs-root",
            str(sysfs),
            "--dev-root",
            str(dev),
            "--wmi-root",
            str(wmi_root),
            "apply",
            str(profile),
        ]
    )

    assert result == 0
    assert attribute.read_text(encoding="ascii") == "1\n"
    assert '"reports_applied": 3' in capsys.readouterr().out


def test_apply_profile_restores_master_when_hid_write_fails(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sysfs, dev = _fake_device(tmp_path)
    wmi_root = tmp_path / "sys/bus/wmi/devices"
    attribute = wmi_root / ("ABBC0F5B-8EA1-11D1-A000-C90629100000-0/touchpad_enabled")
    attribute.parent.mkdir(parents=True)
    attribute.write_text("0\n", encoding="ascii")
    profile = tmp_path / "profile.toml"
    profile.write_text(
        "[settings]\nsensitivity = 'high'\n[master]\nenabled = true\n",
        encoding="utf-8",
    )

    def fail_apply(*_args, **_kwargs):
        raise TouchpadFirmwareError("write failed")

    monkeypatch.setattr(TouchpadFirmwareTransport, "apply_settings", fail_apply)
    result = touchpadctl_main(
        [
            "--sysfs-root",
            str(sysfs),
            "--dev-root",
            str(dev),
            "--wmi-root",
            str(wmi_root),
            "apply",
            str(profile),
        ]
    )

    assert result == 3
    assert attribute.read_text(encoding="ascii").strip() == "0"


def test_touchpad_cli_uses_shared_exit_code_categories(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sysfs, dev = _fake_device(tmp_path)
    (tmp_path / "sys/class/dmi/id/product_name").write_text(
        "UnknownBook\n", encoding="utf-8"
    )
    assert (
        touchpadctl_main(["--sysfs-root", str(sysfs), "--dev-root", str(dev), "probe"])
        == 3
    )
    assert touchpadctl_main(["encode", "not-a-setting", "on"]) == 2

    def partial_failure(*_args, **_kwargs):
        raise TouchpadFirmwareError(
            "partial write",
            reports_applied=1,
            total_reports=2,
        )

    monkeypatch.setattr(TouchpadFirmwareTransport, "apply_setting", partial_failure)
    assert (
        touchpadctl_main(
            [
                "--sysfs-root",
                str(sysfs),
                "--dev-root",
                str(dev),
                    "set",
                "edge_volume",
                "on",
            ]
        )
        == 4
    )


def test_touchpadctl_writes_verified_device_without_legacy_qualification(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("HONOR_TOUCHPAD_FIRMWARE_QUALIFIED", raising=False)
    sysfs, dev = _fake_device(tmp_path)
    base = ["--sysfs-root", str(sysfs), "--dev-root", str(dev)]
    assert touchpadctl_main([*base, "set", "sensitivity", "high"]) == 0
    assert (dev / "hidraw7").read_bytes()[9:] == encode_touchpad_setting("sensitivity", 1)[0]


@pytest.mark.parametrize("value", [None, 1.5, [], {}, b"on"])
def test_setting_rejects_invalid_types(value) -> None:
    with pytest.raises(ValueError, match="invalid value type"):
        parse_touchpad_value("edge_volume", value)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf"), 0, -1])
def test_transport_rejects_unbounded_timeouts(value) -> None:
    with pytest.raises(ValueError, match="finite and positive"):
        TouchpadFirmwareTransport(timeout_seconds=value)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf"), -1])
def test_device_wait_rejects_unbounded_timeouts(value) -> None:
    with pytest.raises(ValueError, match="finite and non-negative"):
        TouchpadFirmwareTransport().wait_until_available(value)


def test_interrupted_writes_respect_deadline(monkeypatch) -> None:
    from honor_control.backend import touchpad_firmware

    ticks = iter([0.0, 0.1, 0.5, 1.0])
    monkeypatch.setattr(touchpad_firmware.time, "monotonic", lambda: next(ticks))
    calls = []

    def interrupted_write(fd, report):
        calls.append(report)
        raise InterruptedError

    monkeypatch.setattr(touchpad_firmware.os, "write", interrupted_write)
    with pytest.raises(TimeoutError):
        TouchpadFirmwareTransport(timeout_seconds=1)._write_report(
            -1, encode_touchpad_setting("sensitivity", 1)[0]
        )
    assert len(calls) == 2


def test_protocol_imports_without_posix_fcntl() -> None:
    import subprocess
    import sys

    result = subprocess.run(
        [sys.executable, "-c", """
import sys
sys.modules['fcntl'] = None
from honor_control.core.touchpad import encode_touchpad_setting, touchpad_firmware_lock
assert encode_touchpad_setting('sensitivity', 1)[0].hex() == '0e0101000000000000'
try:
    with touchpad_firmware_lock():
        raise AssertionError('must reject unsupported locking')
except RuntimeError as exc:
    assert 'POSIX flock' in str(exc)
"""],
        capture_output=True, text=True, timeout=10, check=False,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    "descriptor",
    [VENDOR_DESCRIPTOR[:-1], VENDOR_DESCRIPTOR + b"\xc0", b"\xa4" + VENDOR_DESCRIPTOR],
)
def test_descriptor_rejects_unbalanced_state(descriptor) -> None:
    with pytest.raises(ValueError):
        parse_vendor_collection_descriptor(descriptor)


def test_open_rejects_replaced_node_and_closes_handle(tmp_path, monkeypatch) -> None:
    sysfs, dev = _fake_device(tmp_path)
    transport = TouchpadFirmwareTransport(sysfs_root=sysfs, dev_root=dev)
    original_open = os.open
    opened = []

    def replace_after_open(path, flags):
        fd = original_open(path, flags)
        opened.append(fd)
        replacement = dev / "replacement"
        replacement.write_bytes(b"")
        replacement.replace(path)
        return fd

    monkeypatch.setattr(os, "open", replace_after_open)
    with pytest.raises(TouchpadFirmwareError, match="identity changed"):
        transport.apply_setting("sensitivity", 1)
    assert (dev / "hidraw7").read_bytes() == b""
    with pytest.raises(OSError):
        os.fstat(opened[0])


def test_descriptor_counts_shared_report_id_across_collections() -> None:
    # A second application collection extends the SAME input/output reports.
    extra_collection = bytes.fromhex("05 01 09 02 a1 01 75 08 95 01 81 02 91 02 c0")
    caps = parse_vendor_collection_descriptor(VENDOR_DESCRIPTOR + extra_collection)
    assert caps.input_report_bytes == caps.output_report_bytes == 10
    assert caps.writable is False
