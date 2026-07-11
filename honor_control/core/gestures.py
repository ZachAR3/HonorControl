"""Honor touchpad gesture protocol and virtual-key registry.

This module contains only confirmed HID input semantics and Linux input key
codes. Firmware-setting writes are intentionally absent: the Windows IPC
setting IDs are not a hardware protocol.
"""

from __future__ import annotations

TOUCHPAD_VENDOR_ID = 0x35CC
TOUCHPAD_PRODUCT_ID = 0x0104
GESTURE_REPORT_ID = 0x0E

DEFAULT_GESTURE_MAPPINGS: dict[str, str] = {
    "3:1": "brightnessup",
    "3:2": "brightnessdown",
    "4:1": "volumeup",
    "4:2": "volumedown",
    "10:3": "leftmeta,v",
    "6": "print",
    "7": "leftshift,print",
    "8": "leftmeta,h",
    "9": "leftalt,f4",
}

GESTURE_NAMES: dict[str, str] = {
    "3:1": "Left edge swipe up (brightness up)",
    "3:2": "Left edge swipe down (brightness down)",
    "4:1": "Right edge swipe up (volume up)",
    "4:2": "Right edge swipe down (volume down)",
    "10:3": "Two-finger swipe left (notification panel)",
    "6": "One-knuckle double knock (screenshot)",
    "7": "Two-knuckle double knock (selective screenshot)",
    "8": "Top-left corner click (minimize)",
    "9": "Top-right corner click (close window)",
}

# Stable values from linux/input-event-codes.h. This is deliberately bounded:
# accepting an unknown token and silently dropping it can turn a key chord into
# a different, potentially destructive chord.
KEY_CODES: dict[str, int] = {
    "esc": 1,
    "1": 2,
    "2": 3,
    "3": 4,
    "4": 5,
    "5": 6,
    "6": 7,
    "7": 8,
    "8": 9,
    "9": 10,
    "0": 11,
    "backspace": 14,
    "tab": 15,
    "q": 16,
    "w": 17,
    "e": 18,
    "r": 19,
    "t": 20,
    "y": 21,
    "u": 22,
    "i": 23,
    "o": 24,
    "p": 25,
    "enter": 28,
    "leftctrl": 29,
    "a": 30,
    "s": 31,
    "d": 32,
    "f": 33,
    "g": 34,
    "h": 35,
    "j": 36,
    "k": 37,
    "l": 38,
    "leftshift": 42,
    "z": 44,
    "x": 45,
    "c": 46,
    "v": 47,
    "b": 48,
    "n": 49,
    "m": 50,
    "rightshift": 54,
    "leftalt": 56,
    "space": 57,
    "capslock": 58,
    "f1": 59,
    "f2": 60,
    "f3": 61,
    "f4": 62,
    "f5": 63,
    "f6": 64,
    "f7": 65,
    "f8": 66,
    "f9": 67,
    "f10": 68,
    "f11": 87,
    "f12": 88,
    "sysrq": 99,
    "rightctrl": 97,
    "rightalt": 100,
    "home": 102,
    "up": 103,
    "pageup": 104,
    "left": 105,
    "right": 106,
    "end": 107,
    "down": 108,
    "pagedown": 109,
    "insert": 110,
    "delete": 111,
    "mute": 113,
    "volumedown": 114,
    "volumeup": 115,
    "leftmeta": 125,
    "rightmeta": 126,
    "menu": 139,
    "back": 158,
    "forward": 159,
    "nextsong": 163,
    "playpause": 164,
    "previoussong": 165,
    "stop": 166,
    "homepage": 172,
    "refresh": 173,
    "print": 210,
    "brightnessdown": 224,
    "brightnessup": 225,
}


def gesture_keys_from_report(report: bytes) -> tuple[str, str] | None:
    """Return ``(specific, fallback)`` mapping keys for a vendor report."""
    if len(report) < 3 or report[0] != GESTURE_REPORT_ID:
        return None
    gesture_type = report[1]
    return f"{gesture_type}:{report[2]}", str(gesture_type)


def key_codes_for_tokens(tokens: list[str]) -> list[int]:
    """Translate already-validated key tokens into Linux key codes."""
    return [KEY_CODES[token] for token in tokens]
