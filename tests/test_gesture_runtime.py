"""Tests for the service-owned hidraw-to-uinput gesture runtime."""

from __future__ import annotations

import asyncio
import os
import pathlib
import struct

from honor_control.backend.config_store import GestureMappingState
from honor_control.backend.gesture_runtime import (
    EV_KEY,
    UI_DEV_CREATE,
    UI_DEV_SETUP,
    GestureRuntime,
    create_uinput_device,
    discover_touchpad,
    emit_key_combo,
    probe_gesture_environment,
)
from honor_control.core.gestures import gesture_keys_from_report


def _make_hidraw_tree(tmp_path: pathlib.Path) -> tuple[pathlib.Path, pathlib.Path]:
    sysfs = tmp_path / "sys/class/hidraw"
    dev = tmp_path / "dev"
    (sysfs / "hidraw7/device").mkdir(parents=True)
    dev.mkdir()
    (sysfs / "hidraw7/device/uevent").write_text(
        "DRIVER=hid-multitouch\n"
        "HID_ID=0018:000035CC:00000104\n"
        "HID_NAME=TOPS0102:00 35CC:0104\n",
        encoding="utf-8",
    )
    (dev / "hidraw7").touch()
    (dev / "uinput").touch()
    return sysfs, dev


def test_discover_touchpad_uses_sysfs_identity(tmp_path: pathlib.Path) -> None:
    sysfs, dev = _make_hidraw_tree(tmp_path)
    assert discover_touchpad(sysfs_root=sysfs, dev_root=dev) == dev / "hidraw7"


def test_probe_reports_complete_environment(tmp_path: pathlib.Path) -> None:
    sysfs, dev = _make_hidraw_tree(tmp_path)
    result = probe_gesture_environment(sysfs_root=sysfs, dev_root=dev)
    assert result.available is True
    assert result.device_found is True
    assert result.uinput_found is True


def test_report_parser_rejects_non_vendor_reports() -> None:
    assert gesture_keys_from_report(b"\x0e\x03\x01") == ("3:1", "3")
    assert gesture_keys_from_report(b"\x01\x03\x01") is None
    assert gesture_keys_from_report(b"\x0e\x03") is None


def test_create_uinput_uses_modern_setup_ioctl(monkeypatch) -> None:
    calls: list[tuple[int, object]] = []
    monkeypatch.setattr(os, "open", lambda *_args: 41)
    monkeypatch.setattr(os, "close", lambda _fd: None)

    def fake_ioctl(_fd: int, request: int, arg: object = 0):
        calls.append((request, arg))
        return 0

    monkeypatch.setattr("fcntl.ioctl", fake_ioctl)
    assert create_uinput_device() == 41
    setup = next(arg for request, arg in calls if request == UI_DEV_SETUP)
    assert isinstance(setup, bytes)
    assert len(setup) == 92
    bustype, vendor, product, version = struct.unpack("=HHHH", setup[:8])
    assert (bustype, vendor, product, version) == (0x18, 0x35CC, 0x0104, 1)
    assert calls[-1][0] == UI_DEV_CREATE


def test_emit_key_combo_writes_native_input_events() -> None:
    read_fd, write_fd = os.pipe()
    try:
        emit_key_combo(write_fd, [125, 47])
        payload = os.read(read_fd, 4096)
    finally:
        os.close(read_fd)
        os.close(write_fd)
    event_size = struct.calcsize("@llHHi")
    assert event_size in {16, 24}
    assert len(payload) == event_size * 6
    first = struct.unpack("@llHHi", payload[:event_size])
    assert first[2:] == (EV_KEY, 125, 1)


def test_runtime_dispatches_current_mapping() -> None:
    async def exercise() -> None:
        runtime = GestureRuntime(
            lambda: {"3:1": GestureMappingState(enabled=True, mapping="leftmeta,x")}
        )
        hid_read, hid_write = os.pipe2(os.O_NONBLOCK)
        event_read, event_write = os.pipe2(os.O_NONBLOCK)
        runtime._hid_fd = hid_read  # noqa: SLF001
        runtime._uinput_fd = event_write  # noqa: SLF001
        runtime._session_done = asyncio.get_running_loop().create_future()  # noqa: SLF001
        try:
            os.write(hid_write, b"\x0e\x03\x01\x00\x00\x00\x00\x00\x00")
            runtime._read_ready()  # noqa: SLF001
            payload = os.read(event_read, 4096)
            assert payload
            assert runtime.status.reports_seen == 1
            assert runtime.status.gestures_emitted == 1
        finally:
            runtime._hid_fd = None  # noqa: SLF001
            runtime._uinput_fd = None  # noqa: SLF001
            for fd in (hid_read, hid_write, event_read, event_write):
                os.close(fd)

    asyncio.run(exercise())
