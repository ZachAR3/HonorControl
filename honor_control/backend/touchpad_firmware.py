"""Linux hidraw transport for the confirmed Honor touchpad output protocol."""

from __future__ import annotations

import errno
import os
import pathlib
import select
import time
from dataclasses import dataclass
from typing import Mapping

from honor_control.core.touchpad import (
    TOUCHPAD_PRODUCT_ID,
    TOUCHPAD_REPORT_BYTES,
    TOUCHPAD_REPORT_ID,
    TOUCHPAD_VENDOR_ID,
    TOUCHPAD_VENDOR_USAGE,
    TOUCHPAD_VENDOR_USAGE_PAGE,
    TouchpadSetting,
    decode_support_report,
    encode_touchpad_setting,
    make_support_query_report,
    make_timestamp_report,
    parse_touchpad_setting,
    parse_touchpad_value,
)


@dataclass(frozen=True)
class VendorCollectionCaps:
    """Report sizes for the exact vendor application collection."""

    found: bool = False
    report_id: int | None = None
    input_report_bytes: int = 0
    output_report_bytes: int = 0

    @property
    def writable(self) -> bool:
        return (
            self.found
            and self.report_id == TOUCHPAD_REPORT_ID
            and self.input_report_bytes == TOUCHPAD_REPORT_BYTES
            and self.output_report_bytes == TOUCHPAD_REPORT_BYTES
        )


@dataclass(frozen=True)
class TouchpadFirmwareProbe:
    """Read-only discovery result for the firmware control endpoint."""

    available: bool = False
    platform_verified: bool = False
    dmi_vendor: str = ""
    dmi_product: str = ""
    device_found: bool = False
    permission_denied: bool = False
    descriptor_verified: bool = False
    device_path: str = ""
    report_id: int | None = None
    input_report_bytes: int = 0
    output_report_bytes: int = 0
    error: str = ""


@dataclass(frozen=True)
class TouchpadApplyResult:
    """Result of one typed firmware transaction."""

    setting: TouchpadSetting
    value: int
    device_path: str
    reports: tuple[bytes, ...]
    reports_applied: int
    clock_synchronized: bool

    @property
    def applied(self) -> bool:
        return self.reports_applied == len(self.reports)


@dataclass(frozen=True)
class TouchpadBatchApplyResult:
    """Result of one validated, single-open profile transaction."""

    device_path: str
    settings: tuple[TouchpadApplyResult, ...]
    reports_applied: int
    total_reports: int
    clock_synchronized: bool

    @property
    def applied(self) -> bool:
        return self.reports_applied == self.total_reports


class TouchpadFirmwareError(OSError):
    """A discovery or write failure with partial-application context."""

    def __init__(
        self,
        message: str,
        *,
        device_path: str = "",
        reports_applied: int = 0,
        total_reports: int = 0,
    ) -> None:
        super().__init__(message)
        self.device_path = device_path
        self.reports_applied = reports_applied
        self.total_reports = total_reports


def _unsigned_item_value(payload: bytes) -> int:
    return int.from_bytes(payload, "little", signed=False) if payload else 0


def parse_vendor_collection_descriptor(descriptor: bytes) -> VendorCollectionCaps:
    """Parse the HID descriptor and verify the ``ff00:0001`` collection.

    This intentionally implements the HID short-item state machine instead of
    byte-pattern matching.  Report IDs, counts, and sizes can move without
    changing the meaning of the descriptor.
    """

    globals_: dict[str, int] = {
        "usage_page": 0,
        "report_size": 0,
        "report_id": 0,
        "report_count": 0,
    }
    global_stack: list[dict[str, int]] = []
    collections: list[tuple[int, int, int]] = []
    local_usages: list[int] = []
    input_bits: dict[int, int] = {}
    output_bits: dict[int, int] = {}
    found = False
    position = 0

    while position < len(descriptor):
        prefix = descriptor[position]
        position += 1
        if prefix == 0xFE:
            if position + 2 > len(descriptor):
                raise ValueError("truncated HID long item")
            size = descriptor[position]
            position += 2  # size and long-item tag
            if position + size > len(descriptor):
                raise ValueError("truncated HID long-item payload")
            position += size
            continue

        encoded_size = prefix & 0x03
        size = 4 if encoded_size == 3 else encoded_size
        if position + size > len(descriptor):
            raise ValueError("truncated HID short-item payload")
        payload = descriptor[position : position + size]
        position += size
        item_type = (prefix >> 2) & 0x03
        tag = (prefix >> 4) & 0x0F
        value = _unsigned_item_value(payload)

        if item_type == 1:  # Global items
            if tag == 0x0:
                globals_["usage_page"] = value
            elif tag == 0x7:
                globals_["report_size"] = value
            elif tag == 0x8:
                globals_["report_id"] = value
            elif tag == 0x9:
                globals_["report_count"] = value
            elif tag == 0xA:
                global_stack.append(globals_.copy())
            elif tag == 0xB:
                if not global_stack:
                    raise ValueError("HID global POP without PUSH")
                globals_ = global_stack.pop()
            continue

        if item_type == 2:  # Local items
            if tag == 0x0:
                local_usages.append(value)
            continue

        if item_type != 0:  # Reserved
            continue

        if tag == 0xA:  # Collection
            usage = local_usages[-1] if local_usages else 0
            collections.append((globals_["usage_page"], usage, value))
            if collections[-1] == (
                TOUCHPAD_VENDOR_USAGE_PAGE,
                TOUCHPAD_VENDOR_USAGE,
                0x01,  # Application collection
            ):
                found = True
        elif tag == 0xC:  # End Collection
            if collections:
                collections.pop()
        elif tag in (0x8, 0x9):  # Input / Output
            in_vendor_collection = (
                TOUCHPAD_VENDOR_USAGE_PAGE,
                TOUCHPAD_VENDOR_USAGE,
                0x01,
            ) in collections
            if in_vendor_collection:
                report_id = globals_["report_id"]
                bits = globals_["report_size"] * globals_["report_count"]
                target = input_bits if tag == 0x8 else output_bits
                target[report_id] = target.get(report_id, 0) + bits

        # Local state is cleared after every Main item.
        local_usages.clear()

    report_id: int | None
    if TOUCHPAD_REPORT_ID in input_bits or TOUCHPAD_REPORT_ID in output_bits:
        report_id = TOUCHPAD_REPORT_ID
    else:
        report_ids = sorted(set(input_bits) | set(output_bits))
        report_id = report_ids[0] if len(report_ids) == 1 else None

    def report_bytes(bits: Mapping[int, int], selected: int | None) -> int:
        if selected is None or selected not in bits:
            return 0
        payload_bytes = (bits[selected] + 7) // 8
        return payload_bytes + (1 if selected else 0)

    return VendorCollectionCaps(
        found=found,
        report_id=report_id,
        input_report_bytes=report_bytes(input_bits, report_id),
        output_report_bytes=report_bytes(output_bits, report_id),
    )


def _hid_identity(entry: pathlib.Path) -> tuple[int, int] | None:
    try:
        lines = (entry / "device/uevent").read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    hid_id = next((line[7:] for line in lines if line.startswith("HID_ID=")), "")
    fields = hid_id.split(":")
    if len(fields) != 3:
        return None
    try:
        return int(fields[1], 16), int(fields[2], 16)
    except ValueError:
        return None


def probe_touchpad_firmware(
    *,
    sysfs_root: pathlib.Path = pathlib.Path("/sys/class/hidraw"),
    dev_root: pathlib.Path = pathlib.Path("/dev"),
    dmi_root: pathlib.Path | None = None,
) -> TouchpadFirmwareProbe:
    """Locate and validate the exact hidraw output endpoint without writing."""
    if dmi_root is None:
        dmi_root = sysfs_root.parent / "dmi/id"
    try:
        vendor = (dmi_root / "sys_vendor").read_text(encoding="utf-8").strip()
        product = (dmi_root / "product_name").read_text(encoding="utf-8").strip()
    except OSError as exc:
        return TouchpadFirmwareProbe(error=f"cannot verify DMI platform: {exc}")
    if (vendor, product) != ("HONOR", "MRA-XXX"):
        return TouchpadFirmwareProbe(
            dmi_vendor=vendor,
            dmi_product=product,
            error=(
                "unsupported platform; expected exact DMI HONOR/MRA-XXX, "
                f"found {vendor or '<empty>'}/{product or '<empty>'}"
            ),
        )

    if not sysfs_root.is_dir():
        return TouchpadFirmwareProbe(
            platform_verified=True,
            dmi_vendor=vendor,
            dmi_product=product,
            error="hidraw sysfs class not found",
        )

    identity_found = False
    descriptor_errors: list[str] = []
    for entry in sorted(sysfs_root.glob("hidraw*")):
        if _hid_identity(entry) != (TOUCHPAD_VENDOR_ID, TOUCHPAD_PRODUCT_ID):
            continue
        identity_found = True
        device = dev_root / entry.name
        descriptor_path = entry / "device/report_descriptor"
        try:
            descriptor = descriptor_path.read_bytes()
            caps = parse_vendor_collection_descriptor(descriptor)
        except (OSError, ValueError) as exc:
            descriptor_errors.append(f"{entry.name}: {exc}")
            continue
        if not caps.writable:
            descriptor_errors.append(
                f"{entry.name}: vendor reports are "
                f"in={caps.input_report_bytes}, out={caps.output_report_bytes}, "
                f"id={caps.report_id!r}"
            )
            continue
        exists = device.exists()
        denied = exists and not os.access(device, os.R_OK | os.W_OK)
        error = ""
        if not exists:
            error = f"{device} is missing"
        elif denied:
            error = f"read/write access denied for {device}"
        return TouchpadFirmwareProbe(
            available=exists and not denied,
            platform_verified=True,
            dmi_vendor=vendor,
            dmi_product=product,
            device_found=exists,
            permission_denied=denied,
            descriptor_verified=True,
            device_path=str(device),
            report_id=caps.report_id,
            input_report_bytes=caps.input_report_bytes,
            output_report_bytes=caps.output_report_bytes,
            error=error,
        )

    if identity_found:
        detail = "; ".join(descriptor_errors) or "no writable vendor collection"
        return TouchpadFirmwareProbe(
            platform_verified=True,
            dmi_vendor=vendor,
            dmi_product=product,
            device_found=True,
            error=f"Honor touchpad found, but descriptor verification failed: {detail}",
        )
    return TouchpadFirmwareProbe(
        platform_verified=True,
        dmi_vendor=vendor,
        dmi_product=product,
        error="Honor touchpad 35cc:0104 not found",
    )


class TouchpadFirmwareTransport:
    """Serialized, typed hidraw writer for firmware settings."""

    def __init__(
        self,
        *,
        sysfs_root: pathlib.Path = pathlib.Path("/sys/class/hidraw"),
        dev_root: pathlib.Path = pathlib.Path("/dev"),
        dmi_root: pathlib.Path | None = None,
        timeout_seconds: float = 2.0,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self._sysfs_root = sysfs_root
        self._dev_root = dev_root
        self._dmi_root = dmi_root
        self._timeout_seconds = timeout_seconds

    def probe(self) -> TouchpadFirmwareProbe:
        return probe_touchpad_firmware(
            sysfs_root=self._sysfs_root,
            dev_root=self._dev_root,
            dmi_root=self._dmi_root,
        )

    def wait_until_available(
        self,
        timeout_seconds: float,
        *,
        poll_interval: float = 0.1,
    ) -> TouchpadFirmwareProbe:
        """Wait for the validated hidraw endpoint after boot or resume."""
        if timeout_seconds < 0:
            raise ValueError("device wait timeout cannot be negative")
        if poll_interval <= 0:
            raise ValueError("poll_interval must be positive")
        deadline = time.monotonic() + timeout_seconds
        while True:
            probe = self.probe()
            if probe.available:
                return probe
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TouchpadFirmwareError(
                    f"touchpad did not become available: {probe.error}"
                )
            time.sleep(min(poll_interval, remaining))

    def _open(self) -> tuple[int, str]:
        probe = self.probe()
        if not probe.available:
            raise TouchpadFirmwareError(probe.error or "touchpad is unavailable")
        flags = os.O_RDWR | os.O_NONBLOCK | os.O_CLOEXEC
        try:
            return os.open(probe.device_path, flags), probe.device_path
        except OSError as exc:
            raise TouchpadFirmwareError(
                f"cannot open {probe.device_path}: {exc}",
                device_path=probe.device_path,
            ) from exc

    def _write_report(self, fd: int, report: bytes) -> None:
        if len(report) != TOUCHPAD_REPORT_BYTES or report[0] != TOUCHPAD_REPORT_ID:
            raise ValueError("refusing a non-Honor or non-nine-byte HID report")
        deadline = time.monotonic() + self._timeout_seconds
        while True:
            try:
                written = os.write(fd, report)
            except InterruptedError:
                continue
            except BlockingIOError:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("timed out waiting to write touchpad report")
                try:
                    select.select([], [fd], [], remaining)
                except InterruptedError:
                    continue
                continue
            if written != len(report):
                raise OSError(
                    errno.EIO,
                    f"hidraw accepted {written} of {len(report)} report bytes",
                )
            return

    def apply_setting(
        self,
        setting: str | TouchpadSetting,
        value: str | int | bool,
        *,
        synchronize_clock: bool = True,
        epoch_seconds: int | None = None,
    ) -> TouchpadApplyResult:
        """Apply one validated setting and report partial composite writes."""
        result = self.apply_settings(
            {setting: value},
            synchronize_clock=synchronize_clock,
            epoch_seconds=epoch_seconds,
        )
        return result.settings[0]

    def apply_settings(
        self,
        settings: Mapping[str | TouchpadSetting, str | int | bool],
        *,
        synchronize_clock: bool = True,
        epoch_seconds: int | None = None,
    ) -> TouchpadBatchApplyResult:
        """Validate a complete profile, then apply it through one HID handle.

        Every name and value is validated before the device is opened.  The
        public enum order determines write order, so TOML key order cannot
        change the transaction.  A failure reports the number of setting
        reports accepted across the entire batch; the clock report is tracked
        separately and is not included in that count.
        """
        normalized: dict[TouchpadSetting, tuple[int, tuple[bytes, ...]]] = {}
        for raw_setting, raw_value in settings.items():
            canonical = parse_touchpad_setting(raw_setting)
            if canonical in normalized:
                raise ValueError(f"duplicate touchpad setting {canonical.value!r}")
            parsed = parse_touchpad_value(canonical, raw_value)
            normalized[canonical] = (
                parsed,
                encode_touchpad_setting(canonical, parsed),
            )
        if not normalized:
            raise ValueError("touchpad settings profile is empty")

        ordered = [
            (setting, *normalized[setting])
            for setting in TouchpadSetting
            if setting in normalized
        ]
        total_reports = sum(len(reports) for _, _, reports in ordered)
        fd, device_path = self._open()
        reports_applied = 0
        synchronized = False
        completed: list[TouchpadApplyResult] = []
        current = ordered[0][0]
        try:
            if synchronize_clock:
                self._write_report(fd, make_timestamp_report(epoch_seconds))
                synchronized = True
            for current, value, reports in ordered:
                setting_reports_applied = 0
                for report in reports:
                    self._write_report(fd, report)
                    reports_applied += 1
                    setting_reports_applied += 1
                completed.append(
                    TouchpadApplyResult(
                        setting=current,
                        value=value,
                        device_path=device_path,
                        reports=reports,
                        reports_applied=setting_reports_applied,
                        clock_synchronized=synchronized,
                    )
                )
        except Exception as exc:
            raise TouchpadFirmwareError(
                f"failed applying {current.value}: {exc}",
                device_path=device_path,
                reports_applied=reports_applied,
                total_reports=total_reports,
            ) from exc
        finally:
            os.close(fd)
        return TouchpadBatchApplyResult(
            device_path=device_path,
            settings=tuple(completed),
            reports_applied=reports_applied,
            total_reports=total_reports,
            clock_synchronized=synchronized,
        )

    def query_supported_gestures(
        self,
        *,
        synchronize_clock: bool = True,
        epoch_seconds: int | None = None,
    ) -> frozenset[int]:
        """Request the support bitmap and wait for the asynchronous response.

        Run this diagnostic while the gesture daemon is stopped so two readers
        cannot race for the same response report.
        """
        fd, device_path = self._open()
        try:
            if synchronize_clock:
                self._write_report(fd, make_timestamp_report(epoch_seconds))
            self._write_report(fd, make_support_query_report())
            deadline = time.monotonic() + self._timeout_seconds
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("timed out waiting for support response")
                try:
                    readable, _, _ = select.select([fd], [], [], remaining)
                except InterruptedError:
                    continue
                if not readable:
                    raise TimeoutError("timed out waiting for support response")
                try:
                    report = os.read(fd, 64)
                except InterruptedError:
                    continue
                except BlockingIOError:
                    continue
                if not report:
                    raise OSError(errno.ENODEV, "touchpad disconnected")
                if (
                    len(report) == TOUCHPAD_REPORT_BYTES
                    and report[0] == TOUCHPAD_REPORT_ID
                    and report[1] == 0xF0
                ):
                    return decode_support_report(report)
        except Exception as exc:
            raise TouchpadFirmwareError(
                f"support query failed on {device_path}: {exc}",
                device_path=device_path,
            ) from exc
        finally:
            os.close(fd)
