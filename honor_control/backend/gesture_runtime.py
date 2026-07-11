"""Async hidraw-to-uinput runtime for Honor touchpad gesture reports.

The runtime consumes the confirmed vendor *input* report (report ID ``0x0e``)
and emits configured Linux key chords. It never sends HID output reports or
unknown WMI/EC commands.
"""

from __future__ import annotations

import asyncio
import errno
import fcntl
import logging
import os
import pathlib
import struct
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from typing import Any

from honor_control.core.gestures import (
    DEFAULT_GESTURE_MAPPINGS,
    KEY_CODES,
    TOUCHPAD_PRODUCT_ID,
    TOUCHPAD_VENDOR_ID,
    gesture_keys_from_report,
)
from honor_control.core.validation import validate_key_combo

log = logging.getLogger("honor_control.backend.gesture_runtime")

UI_SET_EVBIT = 0x40045564
UI_SET_KEYBIT = 0x40045565
UI_DEV_SETUP = 0x405C5503
UI_DEV_CREATE = 0x5501
UI_DEV_DESTROY = 0x5502

EV_SYN = 0x00
EV_KEY = 0x01
SYN_REPORT = 0x00
BUS_I2C = 0x18

_INPUT_EVENT = struct.Struct("@llHHi")
_UINPUT_SETUP = struct.Struct("=HHHH80sI")


@dataclass(frozen=True)
class GestureProbe:
    """Current device/access probe result."""

    available: bool = False
    device_found: bool = False
    permission_denied: bool = False
    device_path: str = ""
    uinput_found: bool = False
    error: str = ""


@dataclass(frozen=True)
class GestureRuntimeStatus:
    """Observable state of the gesture dispatch loop."""

    running: bool = False
    device_found: bool = False
    permission_denied: bool = False
    uinput_ready: bool = False
    device_path: str = ""
    reports_seen: int = 0
    gestures_emitted: int = 0
    last_error: str = ""


def discover_touchpad(
    *,
    sysfs_root: pathlib.Path = pathlib.Path("/sys/class/hidraw"),
    dev_root: pathlib.Path = pathlib.Path("/dev"),
    vendor_id: int = TOUCHPAD_VENDOR_ID,
    product_id: int = TOUCHPAD_PRODUCT_ID,
) -> pathlib.Path | None:
    """Find the matching hidraw node from sysfs HID identity metadata."""
    if not sysfs_root.is_dir():
        return None
    for entry in sorted(sysfs_root.glob("hidraw*")):
        try:
            lines = (entry / "device/uevent").read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        hid_id = next((line[7:] for line in lines if line.startswith("HID_ID=")), "")
        fields = hid_id.split(":")
        if len(fields) != 3:
            continue
        try:
            vendor = int(fields[1], 16)
            product = int(fields[2], 16)
        except ValueError:
            continue
        if vendor == vendor_id and product == product_id:
            return dev_root / entry.name
    return None


def probe_gesture_environment(
    *,
    sysfs_root: pathlib.Path = pathlib.Path("/sys/class/hidraw"),
    dev_root: pathlib.Path = pathlib.Path("/dev"),
) -> GestureProbe:
    """Probe the confirmed HID input path and uinput access without writing."""
    device = discover_touchpad(sysfs_root=sysfs_root, dev_root=dev_root)
    if device is None or not device.exists():
        return GestureProbe(error="Honor touchpad hidraw device not found")
    uinput = dev_root / "uinput"
    device_readable = os.access(device, os.R_OK)
    uinput_writable = uinput.exists() and os.access(uinput, os.W_OK)
    denied = not device_readable or (uinput.exists() and not uinput_writable)
    if not uinput.exists():
        error = "/dev/uinput not found; load the uinput kernel module"
    elif denied:
        error = "Gesture service cannot access hidraw or uinput"
    else:
        error = ""
    return GestureProbe(
        available=device_readable and uinput_writable,
        device_found=True,
        permission_denied=denied,
        device_path=str(device),
        uinput_found=uinput.exists(),
        error=error,
    )


def wmi_transport_present(
    root: pathlib.Path = pathlib.Path("/sys/bus/wmi/devices"),
) -> bool:
    """Return whether the reverse-engineered Honor WMI block is enumerated."""
    try:
        return any(
            entry.name.upper().startswith("ABBC0F5B-8EA1-11D1-A000-C90629100000")
            for entry in root.iterdir()
        )
    except OSError:
        return False


def create_uinput_device(
    path: pathlib.Path = pathlib.Path("/dev/uinput"),
    *,
    vendor_id: int = TOUCHPAD_VENDOR_ID,
    product_id: int = TOUCHPAD_PRODUCT_ID,
) -> int:
    """Create a modern uinput virtual keyboard and return its descriptor."""
    fd = os.open(path, os.O_WRONLY | os.O_CLOEXEC)
    try:
        fcntl.ioctl(fd, UI_SET_EVBIT, EV_KEY)
        for code in sorted(set(KEY_CODES.values())):
            fcntl.ioctl(fd, UI_SET_KEYBIT, code)
        name = b"Honor Touchpad Gestures".ljust(80, b"\0")
        setup = _UINPUT_SETUP.pack(BUS_I2C, vendor_id, product_id, 1, name, 0)
        fcntl.ioctl(fd, UI_DEV_SETUP, setup)
        fcntl.ioctl(fd, UI_DEV_CREATE)
        return fd
    except BaseException:
        os.close(fd)
        raise


def _input_event(event_type: int, code: int, value: int) -> bytes:
    now_ns = time.time_ns()
    return _INPUT_EVENT.pack(
        now_ns // 1_000_000_000,
        (now_ns % 1_000_000_000) // 1_000,
        event_type,
        code,
        value,
    )


def _write_all(fd: int, payload: bytes) -> None:
    view = memoryview(payload)
    while view:
        try:
            written = os.write(fd, view)
        except InterruptedError:
            continue
        if written <= 0:
            raise OSError(errno.EIO, "uinput write returned no progress")
        view = view[written:]


def emit_key_combo(fd: int, key_codes: list[int]) -> None:
    """Emit one chord, releasing keys in reverse order even for modifiers."""
    payload = b"".join(
        [
            *(_input_event(EV_KEY, code, 1) for code in key_codes),
            _input_event(EV_SYN, SYN_REPORT, 0),
            *(_input_event(EV_KEY, code, 0) for code in reversed(key_codes)),
            _input_event(EV_SYN, SYN_REPORT, 0),
        ]
    )
    _write_all(fd, payload)


class GestureRuntime:
    """Reconnectable asyncio reader that dispatches configured gestures."""

    def __init__(
        self,
        mappings_provider: Callable[[], Mapping[str, Any]],
        *,
        sysfs_root: pathlib.Path = pathlib.Path("/sys/class/hidraw"),
        dev_root: pathlib.Path = pathlib.Path("/dev"),
        retry_seconds: float = 2.0,
    ) -> None:
        self._mappings_provider = mappings_provider
        self._sysfs_root = sysfs_root
        self._dev_root = dev_root
        self._retry_seconds = retry_seconds
        self._status = GestureRuntimeStatus()
        self._hid_fd: int | None = None
        self._uinput_fd: int | None = None
        self._session_done: asyncio.Future[None] | None = None

    @property
    def status(self) -> GestureRuntimeStatus:
        return self._status

    def probe(self) -> GestureProbe:
        return probe_gesture_environment(
            sysfs_root=self._sysfs_root,
            dev_root=self._dev_root,
        )

    async def run(self) -> None:
        """Run until cancelled, reconnecting after removal or transient errors."""
        try:
            while True:
                try:
                    await self._run_session()
                except asyncio.CancelledError:
                    raise
                except PermissionError as exc:
                    self._status = replace(
                        self._status,
                        running=False,
                        permission_denied=True,
                        last_error=str(exc),
                    )
                except Exception as exc:  # noqa: BLE001
                    self._status = replace(
                        self._status,
                        running=False,
                        last_error=str(exc),
                    )
                await asyncio.sleep(self._retry_seconds)
        finally:
            self._close_session()
            self._status = replace(
                self._status,
                running=False,
                uinput_ready=False,
            )

    async def _run_session(self) -> None:
        device = discover_touchpad(
            sysfs_root=self._sysfs_root,
            dev_root=self._dev_root,
        )
        if device is None or not device.exists():
            self._status = replace(
                self._status,
                running=False,
                device_found=False,
                device_path="",
            )
            raise FileNotFoundError("Honor touchpad hidraw device not found")

        self._status = replace(
            self._status,
            device_found=True,
            device_path=str(device),
            permission_denied=False,
        )
        try:
            self._hid_fd = os.open(
                device,
                os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC,
            )
            self._uinput_fd = create_uinput_device(self._dev_root / "uinput")
        except BaseException:
            self._close_session()
            raise

        loop = asyncio.get_running_loop()
        self._session_done = loop.create_future()
        loop.add_reader(self._hid_fd, self._read_ready)
        self._status = replace(
            self._status,
            running=True,
            permission_denied=False,
            uinput_ready=True,
            last_error="",
        )
        log.info("gesture runtime reading %s", device)
        try:
            await self._session_done
        finally:
            self._close_session()

    def _read_ready(self) -> None:
        if self._hid_fd is None:
            return
        while True:
            try:
                report = os.read(self._hid_fd, 64)
            except BlockingIOError:
                return
            except InterruptedError:
                continue
            except OSError as exc:
                self._fail_session(exc)
                return
            if not report:
                self._fail_session(OSError(errno.ENODEV, "touchpad disconnected"))
                return
            keys = gesture_keys_from_report(report)
            if keys is None:
                continue
            self._status = replace(
                self._status,
                reports_seen=self._status.reports_seen + 1,
            )
            combo = self._mapping_for(keys)
            if not combo:
                continue
            try:
                tokens = validate_key_combo(combo)
                if self._uinput_fd is None:
                    raise OSError(errno.ENODEV, "uinput device is closed")
                emit_key_combo(self._uinput_fd, [KEY_CODES[token] for token in tokens])
                self._status = replace(
                    self._status,
                    gestures_emitted=self._status.gestures_emitted + 1,
                    last_error="",
                )
            except Exception as exc:  # noqa: BLE001
                self._fail_session(exc)
                return

    def _mapping_for(self, keys: tuple[str, str]) -> str:
        configured = self._mappings_provider()
        for key in keys:
            state = configured.get(key)
            if state is not None:
                if not bool(getattr(state, "enabled", False)):
                    return ""
                mapping = str(getattr(state, "mapping", ""))
                return mapping or DEFAULT_GESTURE_MAPPINGS.get(key, "")
            default = DEFAULT_GESTURE_MAPPINGS.get(key)
            if default:
                return default
        return ""

    def _fail_session(self, exc: BaseException) -> None:
        if self._session_done is not None and not self._session_done.done():
            self._session_done.set_exception(exc)

    def _close_session(self) -> None:
        if self._hid_fd is not None:
            try:
                asyncio.get_running_loop().remove_reader(self._hid_fd)
            except (RuntimeError, ValueError):
                pass
        if self._uinput_fd is not None:
            try:
                fcntl.ioctl(self._uinput_fd, UI_DEV_DESTROY)
            except OSError:
                pass
            try:
                os.close(self._uinput_fd)
            except OSError:
                pass
        if self._hid_fd is not None:
            try:
                os.close(self._hid_fd)
            except OSError:
                pass
        self._hid_fd = None
        self._uinput_fd = None
        self._session_done = None
        self._status = replace(
            self._status,
            running=False,
            uinput_ready=False,
        )
