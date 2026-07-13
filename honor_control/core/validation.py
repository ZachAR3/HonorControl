"""Domain validation: thresholds, profiles, curves, and gestures.

Every parser returns a typed model or raises :class:`DomainException` with
a stable ``code``.  No silent coercion to ``0``, ``False``, or ``""``.
"""

from __future__ import annotations

import re

from honor_control.core.errors import DomainError, DomainException
from honor_control.core.gestures import KEY_CODES
from honor_control.core.models import (
    CHARGE_PRESETS,
    POWER_PROFILE_NAMES,
    ChargeMode,
    FanCurvePoint,
    FanMode,
)

# -- Battery thresholds -------------------------------------------------------

MIN_THRESHOLD = 40
MAX_THRESHOLD = 100
#: Minimum gap between start and end (kernel driver hysteresis).
MIN_HYSTERESIS = 5


def validate_thresholds(end: int, start: int) -> tuple[int, int]:
    """Validate and return a ``(end, start)`` threshold pair.

    Rules: ``40 <= start <= end <= 100`` and ``start <= end - MIN_HYSTERESIS``.
    """
    if (
        isinstance(end, bool)
        or isinstance(start, bool)
        or not isinstance(end, int)
        or not isinstance(start, int)
    ):
        raise DomainException(
            DomainError.INVALID_ARGUMENT,
            "Thresholds must be integers",
        )
    if not (MIN_THRESHOLD <= end <= MAX_THRESHOLD):
        raise DomainException(
            DomainError.INVALID_ARGUMENT,
            f"End threshold {end} out of range [{MIN_THRESHOLD}-{MAX_THRESHOLD}]",
        )
    if not (MIN_THRESHOLD <= start <= MAX_THRESHOLD):
        raise DomainException(
            DomainError.INVALID_ARGUMENT,
            f"Start threshold {start} out of range [{MIN_THRESHOLD}-{MAX_THRESHOLD}]",
        )
    if start > end:
        raise DomainException(
            DomainError.INVALID_ARGUMENT,
            f"Start threshold {start} cannot exceed end threshold {end}",
        )
    if end - start < MIN_HYSTERESIS:
        raise DomainException(
            DomainError.INVALID_ARGUMENT,
            f"Gap between end ({end}) and start ({start}) must be at least"
            f" {MIN_HYSTERESIS}%",
        )
    return end, start


def validate_charge_mode(mode: str) -> ChargeMode:
    """Validate a charge-mode preset name (``custom`` is rejected)."""
    if mode == ChargeMode.CUSTOM.value:
        raise DomainException(
            DomainError.INVALID_ARGUMENT,
            "'custom' is a display state; use SetThresholds instead",
        )
    try:
        return ChargeMode(mode)
    except ValueError:
        raise DomainException(
            DomainError.INVALID_ARGUMENT,
            f"Unknown charge mode '{mode}'",
        ) from None


def thresholds_for_mode(mode: ChargeMode) -> tuple[int, int]:
    """Return the ``(end, start)`` preset for a charge mode."""
    if mode == ChargeMode.CUSTOM:
        raise DomainException(
            DomainError.INVALID_ARGUMENT,
            "No preset thresholds for 'custom' mode",
        )
    return CHARGE_PRESETS[mode.value]


# -- Fan curves ---------------------------------------------------------------

#: Temperature range in millidegrees Celsius (platform-safe default).
FAN_TEMP_MIN_MC = 40_000
FAN_TEMP_MAX_MC = 100_000
FAN_SPEED_MIN = 0
FAN_SPEED_MAX = 100
FAN_MIN_POINTS = 2
FAN_MAX_POINTS = 12
#: At/above this temperature, speed must be 100%.
FAN_HIGH_TEMP_MC = 95_000
FAN_HIGH_TEMP_MIN_SPEED = 100

_CURVE_POINT_RE = re.compile(r"^\s*(\d+)\s*:\s*(\d+)\s*$")


def parse_curve(curve: str) -> list[FanCurvePoint]:
    """Parse a ``"temp:speed,..."`` string into validated curve points.

    Rules: 2-12 points, temps within platform range, speeds 0-100,
    strictly increasing temperatures, non-decreasing speeds, and
    at/above 95°C the speed must be 100%.
    """
    if not isinstance(curve, str):
        raise DomainException(
            DomainError.INVALID_ARGUMENT,
            "Fan curve must be a string",
        )
    raw_points: list[tuple[int, int]] = []
    for segment in curve.split(","):
        segment = segment.strip()
        if not segment:
            continue
        match = _CURVE_POINT_RE.match(segment)
        if not match:
            raise DomainException(
                DomainError.INVALID_ARGUMENT,
                f"Invalid curve point '{segment}' (expected temp:speed)",
            )
        temp_mc = int(match.group(1))
        speed = int(match.group(2))
        raw_points.append((temp_mc, speed))

    return validate_curve_points(raw_points)


def validate_curve_points(points: list[tuple[int, int]]) -> list[FanCurvePoint]:
    """Validate a list of ``(temp_mc, speed)`` tuples and return typed points."""
    if not FAN_MIN_POINTS <= len(points) <= FAN_MAX_POINTS:
        raise DomainException(
            DomainError.INVALID_ARGUMENT,
            f"Curve must have {FAN_MIN_POINTS}-{FAN_MAX_POINTS} points"
            f" (got {len(points)})",
        )
    validated: list[FanCurvePoint] = []
    for temp_mc, speed in points:
        if (
            isinstance(temp_mc, bool)
            or isinstance(speed, bool)
            or not isinstance(temp_mc, int)
            or not isinstance(speed, int)
        ):
            raise DomainException(
                DomainError.INVALID_ARGUMENT,
                "Fan curve temperatures and speeds must be integers",
            )
        if not (FAN_TEMP_MIN_MC <= temp_mc <= FAN_TEMP_MAX_MC):
            raise DomainException(
                DomainError.INVALID_ARGUMENT,
                f"Temperature {temp_mc} out of range"
                f" [{FAN_TEMP_MIN_MC}-{FAN_TEMP_MAX_MC}] m°C",
            )
        if not (FAN_SPEED_MIN <= speed <= FAN_SPEED_MAX):
            raise DomainException(
                DomainError.INVALID_ARGUMENT,
                f"Speed {speed} out of range [{FAN_SPEED_MIN}-{FAN_SPEED_MAX}]",
            )
        validated.append(FanCurvePoint(temp_mc=temp_mc, speed=speed))

    # Strictly increasing temperatures.
    for i in range(1, len(validated)):
        if validated[i].temp_mc <= validated[i - 1].temp_mc:
            raise DomainException(
                DomainError.INVALID_ARGUMENT,
                f"Temperatures must be strictly increasing"
                f" (point {i}: {validated[i].temp_mc}"
                f" <= {validated[i - 1].temp_mc})",
            )
    # Non-decreasing speeds.
    for i in range(1, len(validated)):
        if validated[i].speed < validated[i - 1].speed:
            raise DomainException(
                DomainError.INVALID_ARGUMENT,
                f"Speeds must be non-decreasing"
                f" (point {i}: {validated[i].speed}"
                f" < {validated[i - 1].speed})",
            )
    # High-temperature safety rule.
    for point in validated:
        if point.temp_mc >= FAN_HIGH_TEMP_MC and point.speed < FAN_HIGH_TEMP_MIN_SPEED:
            raise DomainException(
                DomainError.INVALID_ARGUMENT,
                f"At {point.temp_mc // 1000}°C speed must be"
                f" {FAN_HIGH_TEMP_MIN_SPEED}% (got {point.speed}%)",
            )
    return validated


def format_curve(points: list[FanCurvePoint]) -> str:
    """Serialize curve points back to the ``temp:speed,...`` string format."""
    return ",".join(f"{p.temp_mc}:{p.speed}" for p in points)


def interpolate_curve(points: list[FanCurvePoint], temp_mc: int) -> int:
    """Linear-interpolate the target speed for ``temp_mc``.

    Below the first point: return the first speed.  Above the last
    point: return the last speed (which must be 100% at high temp).
    """
    if not points:
        return 0
    if temp_mc <= points[0].temp_mc:
        return points[0].speed
    if temp_mc >= points[-1].temp_mc:
        return points[-1].speed
    for i in range(1, len(points)):
        if temp_mc <= points[i].temp_mc:
            p0, p1 = points[i - 1], points[i]
            if p1.temp_mc == p0.temp_mc:
                return p1.speed
            ratio = (temp_mc - p0.temp_mc) / (p1.temp_mc - p0.temp_mc)
            return int(round(p0.speed + ratio * (p1.speed - p0.speed)))
    return points[-1].speed


# -- Fan mode / manual speed --------------------------------------------------

MANUAL_TTL_DEFAULT_SECONDS = 300  # 5 minutes
MANUAL_TTL_MAX_SECONDS = 900  # 15 minutes
MANUAL_EMERGENCY_MIN_SPEED = 80
MANUAL_EMERGENCY_TEMP_MC = 90_000


def validate_fan_mode(mode: str) -> FanMode:
    """Validate a fan mode name."""
    try:
        return FanMode(mode)
    except ValueError:
        raise DomainException(
            DomainError.INVALID_ARGUMENT,
            f"Unknown fan mode '{mode}'",
        ) from None


def validate_manual_speed(speed: int) -> int:
    """Validate a manual fan speed (0-100)."""
    if isinstance(speed, bool) or not isinstance(speed, int):
        raise DomainException(
            DomainError.INVALID_ARGUMENT,
            "Fan speed must be an integer",
        )
    if not (FAN_SPEED_MIN <= speed <= FAN_SPEED_MAX):
        raise DomainException(
            DomainError.INVALID_ARGUMENT,
            f"Fan speed {speed} out of range [{FAN_SPEED_MIN}-{FAN_SPEED_MAX}]",
        )
    return speed


def validate_manual_ttl(ttl_seconds: int) -> int:
    """Validate a manual-override TTL (1-MAX seconds)."""
    if isinstance(ttl_seconds, bool) or not isinstance(ttl_seconds, int):
        raise DomainException(
            DomainError.INVALID_ARGUMENT,
            "TTL must be an integer",
        )
    if ttl_seconds < 1:
        raise DomainException(
            DomainError.INVALID_ARGUMENT,
            "TTL must be at least 1 second",
        )
    if ttl_seconds > MANUAL_TTL_MAX_SECONDS:
        raise DomainException(
            DomainError.INVALID_ARGUMENT,
            f"TTL must not exceed {MANUAL_TTL_MAX_SECONDS} seconds",
        )
    return ttl_seconds


# -- Gestures -----------------------------------------------------------------

#: Valid key token shape (lowercase alphanumeric + underscore).
_KEY_TOKEN_RE = re.compile(r"^[a-z0-9][a-z0-9_]*$")


def validate_key_combo(combo: str) -> list[str]:
    """Parse and validate a comma-separated key combo.

    Returns the list of valid key tokens.  Rejects the whole combo if
    any token is unknown.
    """
    if not isinstance(combo, str) or not combo.strip():
        raise DomainException(
            DomainError.INVALID_ARGUMENT,
            "Key combo is empty",
        )
    tokens = [t.strip().lower() for t in combo.split(",") if t.strip()]
    if not tokens:
        raise DomainException(
            DomainError.INVALID_ARGUMENT,
            "Key combo has no tokens",
        )
    for token in tokens:
        if not _KEY_TOKEN_RE.match(token):
            raise DomainException(
                DomainError.INVALID_ARGUMENT,
                f"Invalid key token '{token}' (use lowercase letters, digits,"
                " and underscores)",
            )
        if token not in KEY_CODES:
            raise DomainException(
                DomainError.INVALID_ARGUMENT,
                f"Unknown key token '{token}'",
            )
    return tokens


def validate_gesture_id(gesture_id: str) -> str:
    """Validate a gesture ID (non-empty, alphanumeric + colon)."""
    if not isinstance(gesture_id, str) or not gesture_id.strip():
        raise DomainException(
            DomainError.INVALID_ARGUMENT,
            "Gesture ID is empty",
        )
    gid = gesture_id.strip()
    if not re.match(r"^[a-zA-Z0-9:_-]+$", gid):
        raise DomainException(
            DomainError.INVALID_ARGUMENT,
            f"Invalid gesture ID '{gid}'",
        )
    return gid


# -- Profile name -------------------------------------------------------------

_PROFILE_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]*$")


def validate_profile_name(name: str) -> str:
    """Validate a power-profile identifier."""
    if not isinstance(name, str) or not name.strip():
        raise DomainException(
            DomainError.INVALID_ARGUMENT,
            "Profile name is empty",
        )
    cleaned = name.strip().lower()
    if not _PROFILE_NAME_RE.match(cleaned):
        raise DomainException(
            DomainError.INVALID_ARGUMENT,
            f"Invalid profile name '{cleaned}'",
        )
    return cleaned


def validate_power_profile(
    name: str, available_names: set[str] | frozenset[str] | None = None
) -> str:
    """Validate a profile against the profiles the hardware adapter implements."""
    cleaned = validate_profile_name(name)
    names = POWER_PROFILE_NAMES if available_names is None else available_names
    if cleaned not in names:
        raise DomainException(
            DomainError.INVALID_ARGUMENT,
            f"Unknown power profile '{cleaned}'",
        )
    return cleaned


# -- Log line limits ----------------------------------------------------------

LOG_MIN_LINES = 1
LOG_MAX_LINES = 500


def validate_log_lines(lines: int) -> int:
    """Validate a recent-logs line count (1-500)."""
    if not isinstance(lines, int):
        raise DomainException(
            DomainError.INVALID_ARGUMENT,
            "Lines must be an integer",
        )
    if not (LOG_MIN_LINES <= lines <= LOG_MAX_LINES):
        raise DomainException(
            DomainError.INVALID_ARGUMENT,
            f"Lines must be in [{LOG_MIN_LINES}-{LOG_MAX_LINES}]",
        )
    return lines
