"""Tests for the core domain layer: errors, models, and validation."""

from __future__ import annotations

import pytest

from honor_control.core.errors import (
    CapabilityStatus,
    DomainError,
    DomainException,
    OperationStatus,
    error_name_for,
)
from honor_control.core.models import (
    CHARGE_PRESETS,
    BatterySnapshot,
    Capability,
    ChargeMode,
    FanCurvePoint,
    FanMode,
    OperationResult,
    SystemSnapshot,
    derive_charge_mode,
)
from honor_control.core.validation import (
    FAN_HIGH_TEMP_MC,
    FAN_MAX_POINTS,
    FAN_TEMP_MAX_MC,
    FAN_TEMP_MIN_MC,
    LOG_MAX_LINES,
    MANUAL_TTL_MAX_SECONDS,
    format_curve,
    interpolate_curve,
    parse_curve,
    validate_charge_mode,
    validate_fan_mode,
    validate_gesture_id,
    validate_key_combo,
    validate_log_lines,
    validate_manual_speed,
    validate_manual_ttl,
    validate_profile_name,
    validate_thresholds,
)


class TestErrors:
    """Verify error codes and D-Bus name mapping are stable."""

    def test_domain_error_values_are_stable_strings(self) -> None:
        assert DomainError.NOT_AUTHORIZED == "not_authorized"
        assert DomainError.INVALID_ARGUMENT == "invalid_argument"
        assert DomainError.UNSUPPORTED == "unsupported"
        assert DomainError.UNAVAILABLE == "unavailable"
        assert DomainError.BUSY == "busy"
        assert DomainError.TIMEOUT == "timeout"
        assert DomainError.DEPENDENCY == "dependency"
        assert DomainError.INTERNAL == "internal"

    def test_operation_status_values_are_stable(self) -> None:
        assert OperationStatus.SUCCESS == "success"
        assert OperationStatus.PARTIAL == "partial"
        assert OperationStatus.REJECTED == "rejected"
        assert OperationStatus.UNAVAILABLE == "unavailable"
        assert OperationStatus.FAILED == "failed"

    def test_capability_status_values_are_stable(self) -> None:
        assert CapabilityStatus.SUPPORTED == "supported"
        assert CapabilityStatus.UNAVAILABLE == "unavailable"
        assert CapabilityStatus.DISABLED == "disabled"
        assert CapabilityStatus.UNSUPPORTED == "unsupported"
        assert CapabilityStatus.EXPERIMENTAL == "experimental"

    def test_error_name_for_every_code(self) -> None:
        for code in DomainError:
            name = error_name_for(code)
            assert name.startswith("org.honorlinux.Control1.Error.")

    def test_domain_exception_carries_code_and_message(self) -> None:
        exc = DomainException(DomainError.UNSUPPORTED, "no EC", detail="trace")
        assert exc.code == DomainError.UNSUPPORTED
        assert exc.message == "no EC"
        assert exc.detail == "trace"


class TestChargePresets:
    """Verify charge-mode preset values and derivation."""

    def test_preset_values(self) -> None:
        assert CHARGE_PRESETS["off"] == (100, 95)
        assert CHARGE_PRESETS["home"] == (90, 85)
        assert CHARGE_PRESETS["travel"] == (80, 75)
        assert CHARGE_PRESETS["storage"] == (60, 55)

    def test_all_presets_in_valid_range(self) -> None:
        for _mode, (end, start) in CHARGE_PRESETS.items():
            assert 40 <= end <= 100
            assert 40 <= start <= 100
            assert start <= end

    def test_derive_charge_mode_matches_preset(self) -> None:
        assert derive_charge_mode(90, 85) == ChargeMode.HOME
        assert derive_charge_mode(100, 95) == ChargeMode.OFF

    def test_derive_charge_mode_custom_for_unknown_pair(self) -> None:
        assert derive_charge_mode(85, 80) == ChargeMode.CUSTOM


class TestThresholdValidation:
    """Verify battery threshold validation rules."""

    @pytest.mark.parametrize("end,start", [(45, 40), (100, 95), (80, 75), (60, 55)])
    def test_valid_thresholds(self, end: int, start: int) -> None:
        assert validate_thresholds(end, start) == (end, start)

    @pytest.mark.parametrize(
        "end,start",
        [
            (39, 39),  # below min
            (101, 100),  # above max
            (80, 90),  # start > end
            (50, 48),  # gap < MIN_HYSTERESIS
        ],
    )
    def test_invalid_thresholds_raise(self, end: int, start: int) -> None:
        with pytest.raises(DomainException) as exc_info:
            validate_thresholds(end, start)
        assert exc_info.value.code == DomainError.INVALID_ARGUMENT

    def test_validate_charge_mode_rejects_custom(self) -> None:
        with pytest.raises(DomainException):
            validate_charge_mode("custom")

    def test_validate_charge_mode_rejects_unknown(self) -> None:
        with pytest.raises(DomainException):
            validate_charge_mode("bogus")

    def test_validate_charge_mode_accepts_preset(self) -> None:
        assert validate_charge_mode("home") == ChargeMode.HOME


class TestFanCurveValidation:
    """Verify fan curve parsing and validation."""

    def test_parse_valid_curve(self) -> None:
        points = parse_curve("40000:0,60000:40,80000:80,95000:100")
        assert len(points) == 4
        assert points[0] == FanCurvePoint(40000, 0)
        assert points[-1] == FanCurvePoint(95000, 100)

    def test_parse_curve_with_spaces(self) -> None:
        points = parse_curve(" 40000 : 0 , 95000 : 100 ")
        assert len(points) == 2

    def test_parse_empty_curve_raises(self) -> None:
        with pytest.raises(DomainException):
            parse_curve("")

    def test_parse_single_point_raises(self) -> None:
        with pytest.raises(DomainException):
            parse_curve("40000:0")

    def test_parse_too_many_points_raises(self) -> None:
        curve = ",".join(f"{40000 + i * 5000}:50" for i in range(FAN_MAX_POINTS + 1))
        with pytest.raises(DomainException):
            parse_curve(curve)

    def test_parse_decreasing_temp_raises(self) -> None:
        with pytest.raises(DomainException):
            parse_curve("80000:40,60000:80")

    def test_parse_decreasing_speed_raises(self) -> None:
        with pytest.raises(DomainException):
            parse_curve("40000:80,60000:40")

    def test_parse_high_temp_below_100_raises(self) -> None:
        with pytest.raises(DomainException):
            parse_curve(f"{FAN_HIGH_TEMP_MC}:80,{FAN_TEMP_MAX_MC}:100")

    def test_parse_out_of_range_temp_raises(self) -> None:
        with pytest.raises(DomainException):
            parse_curve(f"{FAN_TEMP_MIN_MC - 1000}:0,{FAN_TEMP_MAX_MC}:100")

    def test_parse_out_of_range_speed_raises(self) -> None:
        with pytest.raises(DomainException):
            parse_curve("40000:-1,95000:100")
        with pytest.raises(DomainException):
            parse_curve("40000:0,95000:101")

    def test_format_round_trip(self) -> None:
        original = "40000:0,60000:40,80000:80,95000:100"
        points = parse_curve(original)
        assert format_curve(points) == original

    def test_interpolate_below_first_point(self) -> None:
        points = parse_curve("40000:0,60000:40,95000:100")
        assert interpolate_curve(points, 30000) == 0

    def test_interpolate_above_last_point(self) -> None:
        points = parse_curve("40000:0,60000:40,95000:100")
        assert interpolate_curve(points, 99000) == 100

    def test_interpolate_midpoint(self) -> None:
        points = parse_curve("40000:0,60000:40,95000:100")
        assert interpolate_curve(points, 50000) == 20

    def test_interpolate_exact_point(self) -> None:
        points = parse_curve("40000:0,60000:40,95000:100")
        assert interpolate_curve(points, 60000) == 40


class TestFanModeValidation:
    """Verify fan mode and manual speed validation."""

    def test_validate_fan_mode_accepts_known(self) -> None:
        assert validate_fan_mode("stock") == FanMode.STOCK
        assert validate_fan_mode("curve") == FanMode.CURVE
        assert validate_fan_mode("manual_override") == FanMode.MANUAL_OVERRIDE

    def test_validate_fan_mode_rejects_unknown(self) -> None:
        with pytest.raises(DomainException):
            validate_fan_mode("turbo")

    def test_validate_manual_speed_valid(self) -> None:
        assert validate_manual_speed(0) == 0
        assert validate_manual_speed(50) == 50
        assert validate_manual_speed(100) == 100

    def test_validate_manual_speed_out_of_range(self) -> None:
        with pytest.raises(DomainException):
            validate_manual_speed(-1)
        with pytest.raises(DomainException):
            validate_manual_speed(101)

    def test_validate_manual_ttl_valid(self) -> None:
        assert validate_manual_ttl(1) == 1
        assert validate_manual_ttl(300) == 300
        assert validate_manual_ttl(MANUAL_TTL_MAX_SECONDS) == MANUAL_TTL_MAX_SECONDS

    def test_validate_manual_ttl_out_of_range(self) -> None:
        with pytest.raises(DomainException):
            validate_manual_ttl(0)
        with pytest.raises(DomainException):
            validate_manual_ttl(MANUAL_TTL_MAX_SECONDS + 1)


class TestGestureValidation:
    """Verify gesture ID and key-combo validation."""

    def test_validate_key_combo_valid(self) -> None:
        assert validate_key_combo("leftmeta,v") == ["leftmeta", "v"]
        assert validate_key_combo("brightnessup") == ["brightnessup"]

    def test_validate_key_combo_empty_raises(self) -> None:
        with pytest.raises(DomainException):
            validate_key_combo("")
        with pytest.raises(DomainException):
            validate_key_combo("   ")

    def test_validate_key_combo_invalid_token_raises(self) -> None:
        with pytest.raises(DomainException):
            validate_key_combo("leftmeta,1nv4l!d")

    def test_validate_key_combo_unknown_token_raises(self) -> None:
        with pytest.raises(DomainException):
            validate_key_combo("leftmeta,not_a_linux_key")

    def test_validate_gesture_id_valid(self) -> None:
        assert validate_gesture_id("3:1") == "3:1"
        assert validate_gesture_id("swipe_up") == "swipe_up"

    def test_validate_gesture_id_empty_raises(self) -> None:
        with pytest.raises(DomainException):
            validate_gesture_id("")


class TestProfileAndLogValidation:
    """Verify profile name and log line validation."""

    def test_validate_profile_name_valid(self) -> None:
        assert validate_profile_name("balanced") == "balanced"
        assert validate_profile_name("Performance") == "performance"

    def test_validate_profile_name_invalid(self) -> None:
        with pytest.raises(DomainException):
            validate_profile_name("")
        with pytest.raises(DomainException):
            validate_profile_name("1bad")

    def test_validate_log_lines_valid(self) -> None:
        assert validate_log_lines(1) == 1
        assert validate_log_lines(50) == 50
        assert validate_log_lines(LOG_MAX_LINES) == LOG_MAX_LINES

    def test_validate_log_lines_out_of_range(self) -> None:
        with pytest.raises(DomainException):
            validate_log_lines(0)
        with pytest.raises(DomainException):
            validate_log_lines(LOG_MAX_LINES + 1)


class TestOperationResult:
    """Verify OperationResult factory methods and serialization."""

    def test_success_factory(self) -> None:
        r = OperationResult.success(message="ok", applied=True)
        assert r.status == OperationStatus.SUCCESS
        assert r.applied is True
        assert r.changed is True

    def test_rejected_factory(self) -> None:
        r = OperationResult.rejected(code="bad_input", message="nope")
        assert r.status == OperationStatus.REJECTED
        assert r.code == "bad_input"

    def test_partial_factory(self) -> None:
        r = OperationResult.partial(code="half_done", message="partial")
        assert r.status == OperationStatus.PARTIAL
        assert r.changed is True

    def test_to_dict_round_trip(self) -> None:
        r = OperationResult.success(message="ok", details={"temp": 50})
        d = r.to_dict()
        assert d["status"] == "success"
        assert d["details"]["temp"] == 50


class TestCapability:
    """Verify capability writable property."""

    def test_supported_is_writable(self) -> None:
        cap = Capability(status=CapabilityStatus.SUPPORTED)
        assert cap.writable is True

    def test_unsupported_is_not_writable(self) -> None:
        for status in (
            CapabilityStatus.UNAVAILABLE,
            CapabilityStatus.DISABLED,
            CapabilityStatus.UNSUPPORTED,
            CapabilityStatus.EXPERIMENTAL,
        ):
            cap = Capability(status=status)
            assert cap.writable is False


class TestSystemSnapshot:
    """Verify snapshot immutability and defaults."""

    def test_snapshot_is_frozen(self) -> None:
        snap = SystemSnapshot()
        with pytest.raises(AttributeError):
            snap.sequence = 42  # type: ignore[misc]

    def test_snapshot_defaults(self) -> None:
        from honor_control.contract import SCHEMA_VERSION

        snap = SystemSnapshot()
        assert snap.api_version == 1
        assert snap.schema_version == SCHEMA_VERSION
        assert snap.sequence == 0
        assert snap.battery == BatterySnapshot()
