"""Windows backend (Win32 display settings via ``ctypes``).

Refresh rate is switched with ``ChangeDisplaySettingsEx`` on the built-in
panel, found through ``QueryDisplayConfig`` (output technology); the primary
display is used only when no internal panel can be identified.  Windows does
not let applications choose a colour depth per power source, so only the
refresh rate is offered.

All ``ctypes``/``windll`` access happens inside :class:`Win32Api`, which is
constructed lazily, so this module imports cleanly on every platform and the
backend logic can be tested with a fake API.
"""

from __future__ import annotations

import functools
import logging
import sys
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Protocol

from honor_control.frontend.display_backends.types import (
    ApplyResult,
    DisplayCapabilities,
    DisplayPreference,
    Mode,
    available_refresh_rates,
    current_mode,
    pick_refresh_mode,
)

log = logging.getLogger("honor_control.frontend.display_backends.windows")

#: DISPLAYCONFIG_VIDEO_OUTPUT_TECHNOLOGY values that denote a built-in panel.
_INTERNAL_TECHNOLOGIES = frozenset(
    {
        0x80000000,  # INTERNAL
        6,  # LVDS
        11,  # DISPLAYPORT_EMBEDDED
        13,  # UDI_EMBEDDED
    }
)

# DEVMODE / ChangeDisplaySettingsEx constants.
_ENUM_CURRENT_SETTINGS = 0xFFFFFFFF
_DM_BITSPERPEL = 0x00040000
_DM_PELSWIDTH = 0x00080000
_DM_PELSHEIGHT = 0x00100000
_DM_DISPLAYFREQUENCY = 0x00400000
_CDS_UPDATEREGISTRY = 0x00000001
_CDS_TEST = 0x00000002
_DISP_CHANGE_SUCCESSFUL = 0
_QDC_ONLY_ACTIVE_PATHS = 0x00000002
_ERROR_SUCCESS = 0
_DISPLAYCONFIG_DEVICE_INFO_GET_SOURCE_NAME = 1


@dataclass(frozen=True)
class WinMode:
    width: int
    height: int
    bits_per_pixel: int
    frequency: int
    current: bool = False


class WindowsApi(Protocol):
    def internal_device(self) -> str | None:
        """GDI device name (``\\\\.\\DISPLAY1``) of the built-in or primary display."""

    def modes(self, device: str) -> list[WinMode]: ...

    def set_frequency(self, device: str, mode: WinMode) -> bool: ...


@functools.lru_cache(maxsize=1)
def win_structs() -> SimpleNamespace:
    """Return the Win32 structures, built from fixed-width ctypes types.

    Fixed widths (not ``c_long``/``wintypes.DWORD``, whose sizes depend on the
    host C library) keep the layouts identical everywhere, so their sizes can
    be asserted by tests that run on any platform.
    """
    import ctypes as c

    u32, i32, u16 = c.c_uint32, c.c_int32, c.c_uint16
    # Windows' wchar_t is 16 bits; on other hosts c_wchar is 32 bits, so use a
    # 16-bit stand-in there to keep the layout (and its tests) identical.
    wchar = c.c_wchar if sys.platform == "win32" else u16

    class DEVMODEW(c.Structure):
        _fields_ = [
            ("dmDeviceName", wchar * 32),
            ("dmSpecVersion", u16),
            ("dmDriverVersion", u16),
            ("dmSize", u16),
            ("dmDriverExtra", u16),
            ("dmFields", u32),
            ("dmPositionX", i32),
            ("dmPositionY", i32),
            ("dmDisplayOrientation", u32),
            ("dmDisplayFixedOutput", u32),
            ("dmColor", c.c_int16),
            ("dmDuplex", c.c_int16),
            ("dmYResolution", c.c_int16),
            ("dmTTOption", c.c_int16),
            ("dmCollate", c.c_int16),
            ("dmFormName", wchar * 32),
            ("dmLogPixels", u16),
            ("dmBitsPerPel", u32),
            ("dmPelsWidth", u32),
            ("dmPelsHeight", u32),
            ("dmDisplayFlags", u32),
            ("dmDisplayFrequency", u32),
            ("dmICMMethod", u32),
            ("dmICMIntent", u32),
            ("dmMediaType", u32),
            ("dmDitherType", u32),
            ("dmReserved1", u32),
            ("dmReserved2", u32),
            ("dmPanningWidth", u32),
            ("dmPanningHeight", u32),
        ]

    class DISPLAY_DEVICEW(c.Structure):
        _fields_ = [
            ("cb", u32),
            ("DeviceName", wchar * 32),
            ("DeviceString", wchar * 128),
            ("StateFlags", u32),
            ("DeviceID", wchar * 128),
            ("DeviceKey", wchar * 128),
        ]

    class LUID(c.Structure):
        _fields_ = [("LowPart", u32), ("HighPart", i32)]

    class SOURCE(c.Structure):
        _fields_ = [
            ("adapterId", LUID),
            ("id", u32),
            ("modeInfoIdx", u32),
            ("statusFlags", u32),
        ]

    class RATIONAL(c.Structure):
        _fields_ = [("Numerator", u32), ("Denominator", u32)]

    class TARGET(c.Structure):
        _fields_ = [
            ("adapterId", LUID),
            ("id", u32),
            ("modeInfoIdx", u32),
            ("outputTechnology", u32),
            ("rotation", u32),
            ("scaling", u32),
            ("refreshRate", RATIONAL),
            ("scanLineOrdering", u32),
            ("targetAvailable", i32),
            ("statusFlags", u32),
        ]

    class PATH(c.Structure):
        _fields_ = [("sourceInfo", SOURCE), ("targetInfo", TARGET), ("flags", u32)]

    class MODE(c.Structure):  # opaque element: only its size matters here
        _fields_ = [("raw", c.c_ubyte * 64)]

    class HEADER(c.Structure):
        _fields_ = [("type", u32), ("size", u32), ("adapterId", LUID), ("id", u32)]

    class SOURCE_NAME(c.Structure):
        _fields_ = [("header", HEADER), ("viewGdiDeviceName", wchar * 32)]

    class SYSTEM_POWER_STATUS(c.Structure):
        _fields_ = [
            ("ACLineStatus", c.c_ubyte),
            ("BatteryFlag", c.c_ubyte),
            ("BatteryLifePercent", c.c_ubyte),
            ("SystemStatusFlag", c.c_ubyte),
            ("BatteryLifeTime", u32),
            ("BatteryFullLifeTime", u32),
        ]

    return SimpleNamespace(
        **{k: v for k, v in locals().items() if k.isupper() or k[0].isupper()}
    )


class Win32Api:
    """Real implementation on top of ``user32`` (Windows only)."""

    def __init__(self) -> None:
        import ctypes

        self._ctypes = ctypes
        self._s = win_structs()
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        p = ctypes.c_void_p
        u32 = ctypes.c_uint32
        user32.EnumDisplaySettingsExW.argtypes = [ctypes.c_wchar_p, u32, p, u32]
        user32.EnumDisplaySettingsExW.restype = ctypes.c_int
        user32.ChangeDisplaySettingsExW.argtypes = [ctypes.c_wchar_p, p, p, u32, p]
        user32.ChangeDisplaySettingsExW.restype = ctypes.c_int32
        user32.EnumDisplayDevicesW.argtypes = [ctypes.c_wchar_p, u32, p, u32]
        user32.EnumDisplayDevicesW.restype = ctypes.c_int
        user32.GetDisplayConfigBufferSizes.argtypes = [u32, p, p]
        user32.GetDisplayConfigBufferSizes.restype = ctypes.c_int32
        user32.QueryDisplayConfig.argtypes = [u32, p, p, p, p, p]
        user32.QueryDisplayConfig.restype = ctypes.c_int32
        user32.DisplayConfigGetDeviceInfo.argtypes = [p]
        user32.DisplayConfigGetDeviceInfo.restype = ctypes.c_int32
        self._user32 = user32

    def _new_devmode(self):
        devmode = self._s.DEVMODEW()
        devmode.dmSize = self._ctypes.sizeof(self._s.DEVMODEW)
        return devmode

    # -- Display discovery --

    def internal_device(self) -> str | None:
        name = self._internal_via_displayconfig()
        if name:
            return name
        return self._primary_device()

    def _primary_device(self) -> str | None:
        ctypes = self._ctypes
        primary_flag = 0x4  # DISPLAY_DEVICE_PRIMARY_DEVICE
        index = 0
        while True:
            device = self._s.DISPLAY_DEVICEW()
            device.cb = ctypes.sizeof(device)
            if not self._user32.EnumDisplayDevicesW(
                None, index, ctypes.byref(device), 0
            ):
                return None
            if device.StateFlags & primary_flag:
                return device.DeviceName
            index += 1

    def _internal_via_displayconfig(self) -> str | None:
        ctypes, s = self._ctypes, self._s
        paths = ctypes.c_uint32()
        modes = ctypes.c_uint32()
        if (
            self._user32.GetDisplayConfigBufferSizes(
                _QDC_ONLY_ACTIVE_PATHS, ctypes.byref(paths), ctypes.byref(modes)
            )
            != _ERROR_SUCCESS
        ):
            return None
        path_array = (s.PATH * paths.value)()
        mode_array = (s.MODE * modes.value)()
        if (
            self._user32.QueryDisplayConfig(
                _QDC_ONLY_ACTIVE_PATHS,
                ctypes.byref(paths),
                path_array,
                ctypes.byref(modes),
                mode_array,
                None,
            )
            != _ERROR_SUCCESS
        ):
            return None
        for index in range(paths.value):
            path = path_array[index]
            if path.targetInfo.outputTechnology not in _INTERNAL_TECHNOLOGIES:
                continue
            name = s.SOURCE_NAME()
            name.header.type = _DISPLAYCONFIG_DEVICE_INFO_GET_SOURCE_NAME
            name.header.size = ctypes.sizeof(name)
            name.header.adapterId = path.sourceInfo.adapterId
            name.header.id = path.sourceInfo.id
            if (
                self._user32.DisplayConfigGetDeviceInfo(ctypes.byref(name))
                == _ERROR_SUCCESS
            ):
                return name.viewGdiDeviceName
        return None

    # -- Modes --

    def modes(self, device: str) -> list[WinMode]:
        ctypes = self._ctypes
        current = self._new_devmode()
        if not self._user32.EnumDisplaySettingsExW(
            device, _ENUM_CURRENT_SETTINGS, ctypes.byref(current), 0
        ):
            return []
        found: list[WinMode] = []
        index = 0
        while True:
            devmode = self._new_devmode()
            if not self._user32.EnumDisplaySettingsExW(
                device, index, ctypes.byref(devmode), 0
            ):
                break
            index += 1
            if devmode.dmDisplayFrequency <= 1:  # 0/1 mean "hardware default"
                continue
            found.append(
                WinMode(
                    width=devmode.dmPelsWidth,
                    height=devmode.dmPelsHeight,
                    bits_per_pixel=devmode.dmBitsPerPel,
                    frequency=devmode.dmDisplayFrequency,
                    current=(
                        devmode.dmPelsWidth == current.dmPelsWidth
                        and devmode.dmPelsHeight == current.dmPelsHeight
                        and devmode.dmBitsPerPel == current.dmBitsPerPel
                        and devmode.dmDisplayFrequency == current.dmDisplayFrequency
                    ),
                )
            )
        return found

    def set_frequency(self, device: str, mode: WinMode) -> bool:
        ctypes = self._ctypes
        devmode = self._new_devmode()
        devmode.dmPelsWidth = mode.width
        devmode.dmPelsHeight = mode.height
        devmode.dmBitsPerPel = mode.bits_per_pixel
        devmode.dmDisplayFrequency = mode.frequency
        devmode.dmFields = (
            _DM_PELSWIDTH | _DM_PELSHEIGHT | _DM_BITSPERPEL | _DM_DISPLAYFREQUENCY
        )
        # Validate first so a rejected mode never blanks the panel.
        if (
            self._user32.ChangeDisplaySettingsExW(
                device, ctypes.byref(devmode), None, _CDS_TEST, None
            )
            != _DISP_CHANGE_SUCCESSFUL
        ):
            return False
        return (
            self._user32.ChangeDisplaySettingsExW(
                device, ctypes.byref(devmode), None, _CDS_UPDATEREGISTRY, None
            )
            == _DISP_CHANGE_SUCCESSFUL
        )


def system_ac_online() -> bool | None:
    """Return the AC line state from ``GetSystemPowerStatus`` (Windows only)."""
    if sys.platform != "win32":
        return None
    import ctypes

    status = win_structs().SYSTEM_POWER_STATUS()
    if not ctypes.WinDLL("kernel32").GetSystemPowerStatus(ctypes.byref(status)):
        return None
    if status.ACLineStatus == 1:
        return True
    if status.ACLineStatus == 0:
        return False
    return None  # 255: unknown


def _same_size(mode: WinMode, other: WinMode) -> bool:
    return (mode.width, mode.height, mode.bits_per_pixel) == (
        other.width,
        other.height,
        other.bits_per_pixel,
    )


class WindowsBackend:
    name = "Windows (display settings)"

    def __init__(self, api: WindowsApi | None = None) -> None:
        self._api = api

    def is_available(self) -> bool:
        return self._api is not None or sys.platform == "win32"

    def _get_api(self) -> WindowsApi:
        if self._api is None:
            self._api = Win32Api()
        return self._api

    def _read(self) -> tuple[str, list[WinMode]] | None:
        try:
            api = self._get_api()
            device = api.internal_device()
            if not device:
                return None
            modes = api.modes(device)
        except Exception as exc:  # noqa: BLE001 - ctypes/OS failures mean unavailable
            log.warning("display: could not read Windows display modes: %s", exc)
            return None
        return (device, modes) if modes else None

    @staticmethod
    def _as_modes(modes: list[WinMode]) -> list[Mode]:
        return [
            Mode(
                id=str(index),
                width=mode.width,
                height=mode.height,
                refresh=float(mode.frequency),
                current=mode.current,
            )
            for index, mode in enumerate(modes)
        ]

    def query(self) -> DisplayCapabilities | None:
        state = self._read()
        if state is None:
            return None
        device, modes = state
        # Rates at the current resolution *and* bit depth, as Windows lists
        # one entry per combination.
        current = next((mode for mode in modes if mode.current), None)
        same = [mode for mode in modes if current is None or _same_size(mode, current)]
        return DisplayCapabilities(
            backend=self.name,
            output=device,
            refresh_rates=available_refresh_rates(self._as_modes(same)),
        )

    def apply(self, preference: DisplayPreference) -> ApplyResult:
        state = self._read()
        if state is None:
            return ApplyResult(False, message="could not read the display state")
        device, modes = state
        if preference.refresh_hz <= 0:
            return ApplyResult(True, changed=False)
        current = next((mode for mode in modes if mode.current), None)
        if current is None:
            return ApplyResult(False, message="could not read the current mode")
        same = [mode for mode in modes if _same_size(mode, current)]
        generic = self._as_modes(same)
        target_generic = pick_refresh_mode(generic, preference.refresh_hz)
        current_generic = current_mode(generic)
        if target_generic is None or (
            current_generic is not None and target_generic.id == current_generic.id
        ):
            return ApplyResult(True, changed=False)
        target = same[int(target_generic.id)]
        try:
            ok = self._get_api().set_frequency(device, target)
        except Exception as exc:  # noqa: BLE001
            log.warning("display: ChangeDisplaySettingsEx failed: %s", exc)
            ok = False
        if not ok:
            return ApplyResult(False, message="Windows rejected the new refresh rate")
        log.info("display: switched %s to %d Hz", device, target.frequency)
        return ApplyResult(True, changed=True)
