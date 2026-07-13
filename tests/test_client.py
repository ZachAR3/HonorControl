"""Tests for the D-Bus codec and client error mapping (WP-05, WP-06)."""

from __future__ import annotations

import pytest

from honor_control.backend.dbus.codec import (
    from_variant,
    operation_result_to_vardict,
    snapshot_to_vardict,
    to_vardict,
    to_variant,
)
from honor_control.client.errors import ClientError, classify_dbus_error
from honor_control.client.sdbus_client import (
    FakeClient,
    _decode_result,
    _decode_snapshot,
)
from honor_control.core.errors import TransportError
from honor_control.core.models import (
    BatterySnapshot,
    BatteryStatusKind,
    OperationResult,
    OperationStatus,
    PowerProfileEntry,
    PowerSnapshot,
    SystemSnapshot,
)


class TestCodec:
    """Verify D-Bus variant conversion."""

    def test_to_variant_bool(self) -> None:
        assert to_variant(True) == ("b", True)
        assert to_variant(False) == ("b", False)

    def test_to_variant_int(self) -> None:
        assert to_variant(42) == ("i", 42)

    def test_to_variant_str(self) -> None:
        assert to_variant("hello") == ("s", "hello")

    def test_to_variant_dict(self) -> None:
        result = to_variant({"key": "value"})
        sig, val = result
        assert sig == "a{sv}"
        assert "key" in val

    def test_to_variant_list_of_strings(self) -> None:
        result = to_variant(["a", "b"])
        assert result == ("as", ["a", "b"])

    def test_to_vardict_empty(self) -> None:
        assert to_vardict({}) == {}
        assert to_vardict(None) == {}

    def test_from_variant_unwraps_tuple(self) -> None:
        assert from_variant(("s", "hello")) == "hello"
        assert from_variant(("i", 42)) == 42

    def test_from_variant_dict(self) -> None:
        d = {"key": ("s", "value")}
        assert from_variant(d) == {"key": "value"}

    def test_snapshot_round_trip(self) -> None:
        snap = SystemSnapshot()
        vd = snapshot_to_vardict(snap)
        assert "api_version" in vd
        # Decode the variant.
        decoded = from_variant(vd)
        assert decoded["api_version"] == 1

    def test_touchpad_desired_settings_round_trip(self) -> None:
        from honor_control.core.models import GesturesSnapshot

        snap = SystemSnapshot(
            gestures=GesturesSnapshot(
                firmware_settings_supported=True,
                firmware_settings={"edge_volume": 1},
            )
        )
        decoded = _decode_snapshot(from_variant(snapshot_to_vardict(snap)))
        assert decoded.gestures.firmware_settings == {"edge_volume": 1}

    def test_operation_result_round_trip(self) -> None:
        result = OperationResult.success(message="ok", applied=True)
        vd = operation_result_to_vardict(result)
        decoded = from_variant(vd)
        assert decoded["status"] == "success"
        assert decoded["applied"] is True


class TestClientErrorMapping:
    """Verify D-Bus error name classification."""

    def test_not_authorized(self) -> None:
        err = classify_dbus_error(
            "org.honorlinux.Control1.Error.NotAuthorized", "denied"
        )
        assert err.code == TransportError.NOT_AUTHORIZED

    def test_invalid_argument(self) -> None:
        err = classify_dbus_error(
            "org.honorlinux.Control1.Error.InvalidArgument", "bad input"
        )
        assert err.code == TransportError.INVALID_REQUEST

    def test_unsupported(self) -> None:
        err = classify_dbus_error("org.honorlinux.Control1.Error.Unsupported", "no EC")
        assert err.code == TransportError.FEATURE_UNAVAILABLE

    def test_service_unknown(self) -> None:
        err = classify_dbus_error("org.freedesktop.DBus.Error.ServiceUnknown", "gone")
        assert err.code == TransportError.SERVICE_UNAVAILABLE

    def test_timeout(self) -> None:
        err = classify_dbus_error("org.freedesktop.DBus.Error.Timeout", "slow")
        assert err.code == TransportError.TIMEOUT

    def test_unknown_falls_back_to_internal(self) -> None:
        err = classify_dbus_error("org.freedesktop.DBus.Error.Failed", "oops")
        assert err.code == TransportError.INTERNAL


class TestDecodeResult:
    """Verify OperationResult wire decoding."""

    def test_decode_success(self) -> None:
        data = {
            "status": ("s", "success"),
            "code": ("s", ""),
            "message": ("s", "ok"),
            "changed": ("b", True),
            "persisted": ("b", False),
            "applied": ("b", True),
            "sequence": ("i", 5),
            "details": ("a{sv}", {}),
        }
        result = _decode_result(data)
        assert result.status == OperationStatus.SUCCESS
        assert result.applied is True
        assert result.sequence == 5

    def test_decode_rejected(self) -> None:
        data = {
            "status": ("s", "rejected"),
            "code": ("s", "bad_input"),
            "message": ("s", "nope"),
        }
        result = _decode_result(data)
        assert result.status == OperationStatus.REJECTED
        assert result.code == "bad_input"

    def test_decode_invalid_status_is_protocol_error(self) -> None:
        data = {"status": ("s", "bogus")}
        with pytest.raises(ClientError):
            _decode_result(data)


class TestDecodeSnapshot:
    """Verify SystemSnapshot wire decoding."""

    def test_decode_minimal(self) -> None:
        data = {
            "api_version": ("i", 1),
            "schema_version": ("i", 1),
            "sequence": ("i", 42),
        }
        snap = _decode_snapshot(data)
        assert snap.api_version == 1
        assert snap.sequence == 42

    def test_decode_non_dict_raises(self) -> None:
        with pytest.raises(Exception):
            _decode_snapshot("not a dict")  # type: ignore[arg-type]

    def test_decode_complete_snapshot(self) -> None:
        data = snapshot_to_vardict(
            SystemSnapshot(
                sequence=9,
                battery=BatterySnapshot(
                    available=True,
                    capacity_percent=72,
                    status=BatteryStatusKind.DISCHARGING,
                ),
                power=PowerSnapshot(available=True, applied_profile="balanced"),
            )
        )
        snap = _decode_snapshot(data)
        assert snap.sequence == 9
        assert snap.battery.capacity_percent == 72
        assert str(snap.battery.status) == "discharging"
        assert snap.power.applied_profile == "balanced"

    def test_decode_editable_power_profile(self) -> None:
        profile = PowerProfileEntry(
            name="compile",
            label="Compile",
            pl1_uw=30_000_000,
            pl2_uw=45_000_000,
            epp="balance_performance",
            turbo_enabled=False,
            max_perf_pct=85,
        )
        data = snapshot_to_vardict(
            SystemSnapshot(power=PowerSnapshot(profiles=(profile,)))
        )
        decoded = _decode_snapshot(data).power.profiles[0]
        assert decoded.name == "compile"
        assert decoded.pl2_uw == 45_000_000
        assert decoded.turbo_enabled is False
        assert decoded.max_perf_pct == 85


class TestFakeClient:
    """Verify the FakeClient delegates to the application service."""

    def test_fake_client_get_snapshot(self) -> None:
        from honor_control.backend.application import ApplicationService
        from honor_control.backend.hardware import FakeHardware

        app = ApplicationService(hardware=FakeHardware())
        asyncio_run(app.initialize())
        client = FakeClient(app)
        snap = asyncio_run(client.get_snapshot())
        assert snap.sequence > 0


def asyncio_run(coro):
    """Helper to run a coroutine in a test."""
    import asyncio

    return asyncio.run(coro)
