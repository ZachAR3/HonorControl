"""Confirmed Honor MagicTouchPad firmware protocol.

The current Honor service opens the vendor HID collection for ``35cc:0104``
(usage page ``0xff00``, usage ``0x0001``) and writes nine-byte output reports.
The first three bytes are ``0x0e, command, value`` and the remaining bytes are
zero.  The only composite public operation is three-finger drag, which writes
both the drag and light-touch bits in that order.

The master touchpad switch is deliberately absent from this encoder.  Honor
implements that switch through its OEM ACPI-WMI method, not through HID.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Final, Mapping

TOUCHPAD_VENDOR_ID: Final = 0x35CC
TOUCHPAD_PRODUCT_ID: Final = 0x0104
TOUCHPAD_RELEASE: Final = 0x0101
TOUCHPAD_VENDOR_USAGE_PAGE: Final = 0xFF00
TOUCHPAD_VENDOR_USAGE: Final = 0x0001
TOUCHPAD_REPORT_ID: Final = 0x0E
TOUCHPAD_REPORT_BYTES: Final = 9


class TouchpadSetting(StrEnum):
    """Public settings exposed by the updated Honor touchpad UI."""

    SENSITIVITY = "sensitivity"
    VIBRATION_INTENSITY = "vibration_intensity"
    PRESS_TEXT = "press_text"
    PRESS_PICTURE = "press_picture"
    THREE_FINGER_DRAG = "three_finger_drag"
    MOUSE_LIKE_MODE = "mouse_like_mode"
    EDGE_BRIGHTNESS = "edge_brightness"
    EDGE_VOLUME = "edge_volume"
    EDGE_CONTROL_CENTER = "edge_control_center"
    EDGE_CLOSE_OR_MINIMIZE = "edge_close_or_minimize"
    KNUCKLE_SCREENSHOT = "knuckle_screenshot"
    KNUCKLE_SCREEN_RECORD = "knuckle_screen_record"


@dataclass(frozen=True)
class TouchpadSettingSpec:
    """A validated public setting and its firmware command sequence."""

    command: int
    minimum: int
    maximum: int
    registry_name: str
    helper_message_id: int
    companion_commands: tuple[int, ...] = ()
    labels: tuple[str, ...] = ()

    @property
    def boolean(self) -> bool:
        return self.minimum == 0 and self.maximum == 1 and not self.labels


_SETTING_SPECS = {
    TouchpadSetting.SENSITIVITY: TouchpadSettingSpec(
        command=0x01,
        minimum=0,
        maximum=1,
        registry_name="sensitivity",
        helper_message_id=0x02,
        labels=("low", "high"),
    ),
    TouchpadSetting.VIBRATION_INTENSITY: TouchpadSettingSpec(
        command=0x02,
        minimum=0,
        maximum=2,
        registry_name="shock",
        helper_message_id=0x03,
        labels=("low", "medium", "high"),
    ),
    TouchpadSetting.PRESS_TEXT: TouchpadSettingSpec(
        command=0x03,
        minimum=0,
        maximum=1,
        registry_name="SensitivityPressText",
        helper_message_id=0x04,
    ),
    TouchpadSetting.PRESS_PICTURE: TouchpadSettingSpec(
        command=0x04,
        minimum=0,
        maximum=1,
        registry_name="SensitivityPressPicture",
        helper_message_id=0x05,
    ),
    TouchpadSetting.THREE_FINGER_DRAG: TouchpadSettingSpec(
        command=0x05,
        minimum=0,
        maximum=1,
        registry_name="ThreeFingerDrag",
        helper_message_id=0x06,
        companion_commands=(0x11,),
    ),
    TouchpadSetting.MOUSE_LIKE_MODE: TouchpadSettingSpec(
        command=0x06,
        minimum=0,
        maximum=1,
        registry_name="MouseLikeMode",
        helper_message_id=0x07,
    ),
    TouchpadSetting.EDGE_BRIGHTNESS: TouchpadSettingSpec(
        command=0x07,
        minimum=0,
        maximum=1,
        registry_name="EdgeGestureAdjusBrightness",
        helper_message_id=0x08,
    ),
    TouchpadSetting.EDGE_VOLUME: TouchpadSettingSpec(
        command=0x08,
        minimum=0,
        maximum=1,
        registry_name="EdgeGestureAdjusVolume",
        helper_message_id=0x09,
    ),
    TouchpadSetting.EDGE_CONTROL_CENTER: TouchpadSettingSpec(
        command=0x09,
        minimum=0,
        maximum=1,
        registry_name="EdgeGestureOpenHiCenter",
        helper_message_id=0x0A,
    ),
    TouchpadSetting.EDGE_CLOSE_OR_MINIMIZE: TouchpadSettingSpec(
        command=0x0A,
        minimum=0,
        maximum=1,
        registry_name="EdgeGestureCloesOrMinWnd",
        helper_message_id=0x0B,
    ),
    TouchpadSetting.KNUCKLE_SCREENSHOT: TouchpadSettingSpec(
        command=0x0B,
        minimum=0,
        maximum=1,
        registry_name="KnuckleScreenShot",
        helper_message_id=0x0C,
    ),
    TouchpadSetting.KNUCKLE_SCREEN_RECORD: TouchpadSettingSpec(
        command=0x0C,
        minimum=0,
        maximum=1,
        registry_name="KnuckleRecordScreen",
        helper_message_id=0x0D,
    ),
}
TOUCHPAD_SETTING_SPECS: Mapping[TouchpadSetting, TouchpadSettingSpec] = (
    MappingProxyType(_SETTING_SPECS)
)

# Accept the registry names and terms used by the old capture notes without
# making those spellings part of the stable public API.
_ALIASES = {
    "shock": TouchpadSetting.VIBRATION_INTENSITY,
    "sensitivity_press_text": TouchpadSetting.PRESS_TEXT,
    "sensitivity_press_picture": TouchpadSetting.PRESS_PICTURE,
    "edge_gesture_adjus_brightness": TouchpadSetting.EDGE_BRIGHTNESS,
    "edge_gesture_adjus_volume": TouchpadSetting.EDGE_VOLUME,
    "edge_gesture_open_hi_center": TouchpadSetting.EDGE_CONTROL_CENTER,
    "edge_gesture_cloes_or_min_wnd": TouchpadSetting.EDGE_CLOSE_OR_MINIMIZE,
    "knuckle_screen_shot": TouchpadSetting.KNUCKLE_SCREENSHOT,
    "knuckle_record_screen": TouchpadSetting.KNUCKLE_SCREEN_RECORD,
}


def parse_touchpad_setting(value: str | TouchpadSetting) -> TouchpadSetting:
    """Return a canonical setting name or raise :class:`ValueError`."""
    if isinstance(value, TouchpadSetting):
        return value
    normalized = str(value).strip().lower().replace("-", "_")
    try:
        return TouchpadSetting(normalized)
    except ValueError:
        try:
            return _ALIASES[normalized]
        except KeyError as exc:
            names = ", ".join(setting.value for setting in TouchpadSetting)
            raise ValueError(f"unknown touchpad setting {value!r}; choose {names}") from exc


def parse_touchpad_value(
    setting: str | TouchpadSetting,
    value: str | int | bool,
) -> int:
    """Validate and normalize a setting value to the firmware byte."""
    canonical = parse_touchpad_setting(setting)
    spec = TOUCHPAD_SETTING_SPECS[canonical]
    if isinstance(value, bool):
        if spec.maximum > 1:
            labels = ", ".join(spec.labels)
            raise ValueError(
                f"{canonical.value} does not accept a boolean; choose {labels}"
            )
        parsed = int(value)
    elif isinstance(value, int):
        parsed = value
    else:
        normalized = value.strip().lower()
        named: dict[str, int] = {}
        if spec.maximum == 1:
            named.update(
                {
                    "off": 0,
                    "false": 0,
                    "disabled": 0,
                    "on": 1,
                    "true": 1,
                    "enabled": 1,
                }
            )
        if spec.labels:
            named.update({label: index for index, label in enumerate(spec.labels)})
        if normalized in named:
            parsed = named[normalized]
        else:
            try:
                parsed = int(normalized, 0)
            except ValueError as exc:
                raise ValueError(
                    f"invalid value {value!r} for {canonical.value}"
                ) from exc
    if not spec.minimum <= parsed <= spec.maximum:
        raise ValueError(
            f"{canonical.value} must be {spec.minimum}..{spec.maximum}, got {parsed}"
        )
    return parsed


def make_touchpad_report(command: int, value: int = 0) -> bytes:
    """Build one exact nine-byte vendor output report."""
    if not 0 <= command <= 0xFF or not 0 <= value <= 0xFF:
        raise ValueError("touchpad command and value must fit in one byte")
    return bytes((TOUCHPAD_REPORT_ID, command, value, 0, 0, 0, 0, 0, 0))


def encode_touchpad_setting(
    setting: str | TouchpadSetting,
    value: str | int | bool,
) -> tuple[bytes, ...]:
    """Encode one public operation, including required companion reports."""
    canonical = parse_touchpad_setting(setting)
    parsed = parse_touchpad_value(canonical, value)
    spec = TOUCHPAD_SETTING_SPECS[canonical]
    return tuple(
        make_touchpad_report(command, parsed)
        for command in (spec.command, *spec.companion_commands)
    )


def make_timestamp_report(epoch_seconds: int | None = None) -> bytes:
    """Build the service's startup/resume time synchronization report.

    Honor calls ``time(NULL)`` and copies the low five bytes little-endian.
    """
    seconds = int(time.time()) if epoch_seconds is None else int(epoch_seconds)
    if seconds < 0 or seconds >= 1 << 40:
        raise ValueError("epoch timestamp must fit in five bytes")
    return bytes((TOUCHPAD_REPORT_ID, 0x00)) + seconds.to_bytes(5, "little") + b"\0\0"


def make_support_query_report() -> bytes:
    """Build the firmware gesture-capability query."""
    return make_touchpad_report(0xF0, 0)


# The updated plugin tests these bit indices in the response payload beginning
# at byte two.  Names come from its support table and the updated managed UI.
SUPPORTED_GESTURE_BITS: Mapping[int, str] = MappingProxyType(
    {
        1: "one_knuckle_double_tap",
        2: "two_knuckle_double_tap",
        3: "left_edge_up",
        4: "left_edge_down",
        5: "right_edge_up",
        6: "right_edge_down",
        15: "top_left_corner_press",
        16: "top_right_corner_press",
        17: "single_finger_heavy_pressure",
        18: "right_to_left_slide",
        20: "three_finger_drag",
        26: "double_finger_pressure",
        27: "three_finger_light_touch",
    }
)


def decode_support_report(report: bytes) -> frozenset[int]:
    """Decode supported gesture bit indices from a nine-byte response."""
    if len(report) != TOUCHPAD_REPORT_BYTES:
        raise ValueError(f"support report must be {TOUCHPAD_REPORT_BYTES} bytes")
    if report[0] != TOUCHPAD_REPORT_ID or report[1] != 0xF0:
        raise ValueError("not an Honor touchpad support response")
    result: set[int] = set()
    for bit in range((len(report) - 2) * 8):
        if report[2 + bit // 8] & (1 << (bit % 8)):
            result.add(bit)
    return frozenset(result)
