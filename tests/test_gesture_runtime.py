"""Tests for the service-owned hidraw-to-uinput gesture runtime."""

from __future__ import annotations

import asyncio
import errno
import fcntl
import os
import pathlib
import struct

import pytest

from honor_control.backend.config_store import GestureMappingState
from honor_control.backend.gesture_runtime import (
    EV_KEY,
    UI_DEV_CREATE,
    UI_DEV_DESTROY,
    UI_DEV_SETUP,
    GestureRuntime,
    create_uinput_device,
    discover_touchpad,
    emit_key_combo,
    probe_gesture_environment,
)
from honor_control.core.gestures import gesture_keys_from_report
from honor_control.core.touchpad import TouchpadLockBusy, touchpad_firmware_lock

VENDOR_DESCRIPTOR = bytes.fromhex(
    "06 00 ff 09 01 a1 01 85 0e 75 08 95 08 81 02 91 02 c0"
)


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
    (sysfs / "hidraw7/device/report_descriptor").write_bytes(VENDOR_DESCRIPTOR)
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


def test_probe_marks_unreadable_hidraw_as_permission_denied(
    tmp_path: pathlib.Path, monkeypatch
) -> None:
    sysfs, dev = _make_hidraw_tree(tmp_path)
    monkeypatch.setattr(os, "access", lambda path, mode: path != dev / "hidraw7")

    result = probe_gesture_environment(sysfs_root=sysfs, dev_root=dev)

    assert result.available is False
    assert result.permission_denied is True


def test_run_session_closes_fds_when_reader_registration_fails(
    tmp_path: pathlib.Path, monkeypatch
) -> None:
    sysfs, dev = _make_hidraw_tree(tmp_path)
    runtime = GestureRuntime(lambda: {}, sysfs_root=sysfs, dev_root=dev)
    closed: list[int] = []

    monkeypatch.setattr(
        "honor_control.backend.gesture_runtime.os.open",
        lambda path, *_args: 41 if str(path).endswith("hidraw7") else 42,
    )
    monkeypatch.setattr(
        "honor_control.backend.gesture_runtime.os.close", closed.append
    )
    monkeypatch.setattr(
        "honor_control.backend.gesture_runtime.create_uinput_device",
        lambda *_args, **_kwargs: 42,
    )

    async def exercise() -> None:
        loop = asyncio.get_running_loop()
        monkeypatch.setattr(
            loop, "add_reader", lambda *_args: (_ for _ in ()).throw(OSError("no fd"))
        )
        monkeypatch.setattr(loop, "remove_reader", lambda *_args: None)
        with pytest.raises(OSError, match="no fd"):
            await runtime._run_session()  # noqa: SLF001

    asyncio.run(exercise())
    # asyncio.run closes its own selector pipe after the coroutine returns;
    # the final two closes are the runtime descriptors.
    assert closed[-2:] == [42, 41]
    assert runtime._hid_fd is None  # noqa: SLF001
    assert runtime._uinput_fd is None  # noqa: SLF001
    assert runtime._session_done is None  # noqa: SLF001


def test_report_parser_rejects_non_vendor_reports() -> None:
    valid = b"\x0e\x03\x01\x00\x00\x00\x00\x00\x00"
    assert gesture_keys_from_report(valid) == ("3:1", "3")
    assert gesture_keys_from_report(b"\x01" + valid[1:]) is None
    assert gesture_keys_from_report(valid + b"\x00") is None
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


def test_runtime_debounces_duplicate_reports() -> None:
    async def exercise() -> None:
        runtime = GestureRuntime(
            lambda: {"3:1": GestureMappingState(enabled=True, mapping="leftmeta,x")}
        )
        hid_read, hid_write = os.pipe2(os.O_NONBLOCK)
        event_read, event_write = os.pipe2(os.O_NONBLOCK)
        runtime._hid_fd = hid_read  # noqa: SLF001
        runtime._uinput_fd = event_write  # noqa: SLF001
        runtime._session_done = asyncio.get_running_loop().create_future()  # noqa: SLF001
        report = b"\x0e\x03\x01\x00\x00\x00\x00\x00\x00"
        try:
            os.write(hid_write, report)
            runtime._read_ready()  # noqa: SLF001
            os.write(hid_write, report)
            runtime._read_ready()  # noqa: SLF001
            assert os.read(event_read, 4096)
            assert runtime.status.gestures_emitted == 1
        finally:
            runtime._hid_fd = None  # noqa: SLF001
            runtime._uinput_fd = None  # noqa: SLF001
            for fd in (hid_read, hid_write, event_read, event_write):
                os.close(fd)

    asyncio.run(exercise())


def test_runtime_skips_bad_mapping_without_tearing_down_session(monkeypatch) -> None:
    destroyed: list[int] = []
    real_ioctl = fcntl.ioctl

    def spy_ioctl(fd: int, request: int, *args: object) -> int:
        if request == UI_DEV_DESTROY:
            destroyed.append(fd)
        return real_ioctl(fd, request, *args)

    monkeypatch.setattr("fcntl.ioctl", spy_ioctl)

    async def exercise() -> None:
        runtime = GestureRuntime(
            lambda: {
                "3:1": GestureMappingState(
                    enabled=True, mapping="definitely_not_a_key"
                ),
                "3:2": GestureMappingState(enabled=True, mapping="leftmeta,x"),
            }
        )
        hid_read, hid_write = os.pipe2(os.O_NONBLOCK)
        event_read, event_write = os.pipe2(os.O_NONBLOCK)
        runtime._hid_fd = hid_read  # noqa: SLF001
        runtime._uinput_fd = event_write  # noqa: SLF001
        session_done = asyncio.get_running_loop().create_future()
        runtime._session_done = session_done  # noqa: SLF001
        try:
            # Report 3:1 carries a permanently invalid chord: it must be
            # skipped per-report without ending the session.
            os.write(hid_write, b"\x0e\x03\x01\x00\x00\x00\x00\x00\x00")
            runtime._read_ready()  # noqa: SLF001
            assert not session_done.done()
            assert runtime.status.skipped == 1
            assert runtime.status.gestures_emitted == 0
            assert "definitely_not_a_key" in runtime.status.last_error
            assert runtime._uinput_fd == event_write  # noqa: SLF001
            assert destroyed == []

            # A subsequent valid report still emits: the session survived and
            # no uinput destroy/recreate happened for the bad report.
            os.write(hid_write, b"\x0e\x03\x02\x00\x00\x00\x00\x00\x00")
            runtime._read_ready()  # noqa: SLF001
            assert not session_done.done()
            assert os.read(event_read, 4096)
            assert runtime.status.reports_seen == 2
            assert runtime.status.gestures_emitted == 1
            assert runtime.status.skipped == 1
            assert destroyed == []
        finally:
            runtime._hid_fd = None  # noqa: SLF001
            runtime._uinput_fd = None  # noqa: SLF001
            for fd in (hid_read, hid_write, event_read, event_write):
                os.close(fd)

    asyncio.run(exercise())


def test_runtime_eof_still_fails_session() -> None:
    async def exercise() -> None:
        runtime = GestureRuntime(lambda: {})
        hid_read, hid_write = os.pipe2(os.O_NONBLOCK)
        runtime._hid_fd = hid_read  # noqa: SLF001
        runtime._session_done = asyncio.get_running_loop().create_future()  # noqa: SLF001
        os.close(hid_write)  # EOF on the next read: simulates device unplug
        try:
            runtime._read_ready()  # noqa: SLF001
            assert runtime._session_done.done()  # noqa: SLF001
            exc = runtime._session_done.exception()  # noqa: SLF001
            assert isinstance(exc, OSError)
            assert exc.errno == errno.ENODEV
        finally:
            runtime._hid_fd = None  # noqa: SLF001
            os.close(hid_read)

    asyncio.run(exercise())


def test_runtime_uinput_write_error_still_fails_session() -> None:
    async def exercise() -> None:
        runtime = GestureRuntime(
            lambda: {"3:1": GestureMappingState(enabled=True, mapping="leftmeta,x")}
        )
        hid_read, hid_write = os.pipe2(os.O_NONBLOCK)
        event_read, event_write = os.pipe2(os.O_NONBLOCK)
        os.close(event_read)  # uinput writes now fail with EPIPE
        runtime._hid_fd = hid_read  # noqa: SLF001
        runtime._uinput_fd = event_write  # noqa: SLF001
        runtime._session_done = asyncio.get_running_loop().create_future()  # noqa: SLF001
        try:
            os.write(hid_write, b"\x0e\x03\x01\x00\x00\x00\x00\x00\x00")
            runtime._read_ready()  # noqa: SLF001
            assert runtime._session_done.done()  # noqa: SLF001
            exc = runtime._session_done.exception()  # noqa: SLF001
            assert isinstance(exc, OSError)
        finally:
            runtime._hid_fd = None  # noqa: SLF001
            runtime._uinput_fd = None  # noqa: SLF001
            for fd in (hid_read, hid_write, event_write):
                os.close(fd)

    asyncio.run(exercise())


def test_reconnect_backoff_grows_and_is_capped() -> None:
    runtime = GestureRuntime(
        lambda: {},
        retry_seconds=2.0,
        max_retry_seconds=30.0,
        stable_session_seconds=5.0,
    )
    delays = [runtime._next_retry_delay(0.1) for _ in range(6)]  # noqa: SLF001
    assert delays == [2.0, 4.0, 8.0, 16.0, 30.0, 30.0]
    # A session that ran long enough resets the backoff...
    assert runtime._next_retry_delay(10.0) == 2.0  # noqa: SLF001
    assert runtime._next_retry_delay(0.1) == 2.0  # noqa: SLF001
    # ...while a short-lived session keeps growing it.
    assert runtime._next_retry_delay(4.0) == 4.0  # noqa: SLF001


def test_run_retries_failed_sessions_with_growing_backoff(monkeypatch) -> None:
    runtime = GestureRuntime(
        lambda: {},
        retry_seconds=2.0,
        max_retry_seconds=30.0,
    )
    attempts = 0

    async def failing_session() -> None:
        nonlocal attempts
        attempts += 1
        raise OSError(errno.EIO, "injected I/O failure")

    monkeypatch.setattr(runtime, "_run_session", failing_session)
    sleeps: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)
        if len(sleeps) >= 3:
            raise asyncio.CancelledError

    monkeypatch.setattr("asyncio.sleep", fake_sleep)

    async def exercise() -> None:
        try:
            await runtime.run()
        except asyncio.CancelledError:
            pass
        else:
            raise AssertionError("run() should propagate cancellation")

    asyncio.run(exercise())
    assert attempts == 3
    assert sleeps == [2.0, 4.0, 8.0]
    assert runtime.status.running is False
    assert "injected I/O failure" in runtime.status.last_error
    # A clean stop resets the backoff for the next run().
    assert runtime._retry_backoff == 2.0  # noqa: SLF001


def test_running_reader_holds_cross_process_firmware_lock(monkeypatch) -> None:
    runtime = GestureRuntime(lambda: {})

    async def exercise() -> None:
        started = asyncio.Event()

        async def active_session() -> None:
            started.set()
            await asyncio.Future()

        monkeypatch.setattr(runtime, "_run_session", active_session)
        task = asyncio.create_task(runtime.run())
        await started.wait()
        with pytest.raises(TouchpadLockBusy):
            with touchpad_firmware_lock():
                pass
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(exercise())
