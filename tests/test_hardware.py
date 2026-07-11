"""Tests for the hardware port and FakeHardware (WP-03)."""

from __future__ import annotations

import pytest

from honor_control.backend.hardware import (
    FakeHardware,
    HonorToolsAdapter,
    _discover_fan_inputs,
)
from honor_control.core.errors import DomainError, DomainException
from honor_control.core.models import (
    BatterySnapshot,
    CapabilityStatus,
    FanMode,
    FanSnapshot,
    GesturesSnapshot,
    GpuSnapshot,
    PlatformInfo,
)


class TestFakeHardwarePlatform:
    """Verify platform detection and capability probes."""

    def test_detect_platform_returns_matched_info(self) -> None:
        hw = FakeHardware()
        plat = hw.detect_platform()
        assert isinstance(plat, PlatformInfo)
        assert plat.matched is True
        assert plat.confidence == "high"
        assert "Honor" in plat.vendor

    def test_all_capabilities_supported(self) -> None:
        hw = FakeHardware()
        for cap in (
            hw.get_battery_capability(),
            hw.get_power_capability(),
            hw.get_fan_capability(),
            hw.get_gestures_capability(),
            hw.get_gpu_capability(),
        ):
            assert cap.status == CapabilityStatus.SUPPORTED
            assert cap.writable is True

    def test_check_dependency_ok(self) -> None:
        hw = FakeHardware()
        assert hw.check_dependency() is True


class TestFakeHardwareBattery:
    """Verify battery read/write and failure handling."""

    def test_read_battery_returns_typed_snapshot(self) -> None:
        hw = FakeHardware()
        bat = hw.read_battery()
        assert isinstance(bat, BatterySnapshot)
        assert bat.available is True
        assert bat.capacity_percent == 75
        assert bat.observed_end == 90

    def test_write_thresholds_updates_state(self) -> None:
        hw = FakeHardware()
        result = hw.write_battery_thresholds(80, 75)
        assert result["end_write_ok"] is True
        assert result["readback_ok"] is True
        bat = hw.read_battery()
        assert bat.observed_end == 80

    def test_queued_failure_raises(self) -> None:
        hw = FakeHardware()
        hw.fail_next("write_battery_thresholds")
        with pytest.raises(DomainException) as exc_info:
            hw.write_battery_thresholds(80, 75)
        assert exc_info.value.code == DomainError.UNAVAILABLE

    def test_call_log_records_operations(self) -> None:
        hw = FakeHardware()
        hw.read_battery()
        hw.write_battery_thresholds(70, 65)
        names = [entry[0] for entry in hw.call_log]
        assert "read_battery" in names
        assert "write_battery_thresholds" in names


class TestFakeHardwareFan:
    """Verify fan read/write."""

    def test_read_fan_returns_typed_snapshot(self) -> None:
        hw = FakeHardware()
        fan = hw.read_fan()
        assert isinstance(fan, FanSnapshot)
        assert fan.available is True
        assert fan.mode == FanMode.STOCK

    def test_set_fan_auto(self) -> None:
        hw = FakeHardware()
        assert hw.set_fan_auto() is True
        assert hw.read_fan().mode == FanMode.STOCK

    def test_set_fan_manual_and_speed(self) -> None:
        hw = FakeHardware()
        assert hw.set_fan_manual(50) is True
        assert hw.set_fan_speed(50) is True
        fan = hw.read_fan()
        assert fan.mode == FanMode.MANUAL_OVERRIDE

    def test_read_fan_temp(self) -> None:
        hw = FakeHardware()
        temp = hw.read_fan_temp()
        assert temp == 45_000


class TestFakeHardwareGestures:
    """Verify gesture read/write."""

    def test_read_gestures_returns_typed_snapshot(self) -> None:
        hw = FakeHardware()
        ges = hw.read_gestures()
        assert isinstance(ges, GesturesSnapshot)
        assert ges.available is True
        assert len(ges.mappings) == 3

    def test_set_gesture_mapping_preserves_enabled(self) -> None:
        hw = FakeHardware()
        # Disable gesture 3:1, then change its mapping.
        assert hw.set_gesture_enabled("3:1", False) is True
        assert hw.set_gesture_mapping("3:1", "leftmeta,x") is True
        entries = hw.list_gesture_mappings()
        entry = next(e for e in entries if e.id == "3:1")
        assert entry.enabled is False  # mapping changed but enabled preserved
        assert entry.mapping == "leftmeta,x"

    def test_set_gesture_enabled_unknown_id_returns_false(self) -> None:
        hw = FakeHardware()
        assert hw.set_gesture_enabled("bogus", True) is False


class TestFakeHardwareGpu:
    """Verify GPU mitigation read/write."""

    def test_read_gpu_returns_typed_snapshot(self) -> None:
        hw = FakeHardware()
        gpu = hw.read_gpu()
        assert isinstance(gpu, GpuSnapshot)
        assert gpu.available is True
        assert gpu.mitigation_enabled is False

    def test_apply_and_restore(self) -> None:
        hw = FakeHardware()
        result = hw.apply_gpu_mitigation()
        assert result["affinity_ok"] is True
        assert hw.read_gpu().mitigation_enabled is True
        result = hw.restore_gpu_mitigation()
        assert result["restored"] is True
        assert hw.read_gpu().mitigation_enabled is False


class TestHonorToolsAdapterFilesystem:
    def test_discovers_rpm_sensor_outside_temperature_hwmon(self, tmp_path) -> None:
        hwmon = tmp_path / "sys/class/hwmon"
        temperature = hwmon / "hwmon0"
        fan = hwmon / "hwmon1"
        temperature.mkdir(parents=True)
        fan.mkdir(parents=True)
        (temperature / "name").write_text("coretemp", encoding="utf-8")
        (temperature / "temp1_input").write_text("45000", encoding="utf-8")
        (fan / "name").write_text("honor_fan", encoding="utf-8")
        (fan / "fan1_input").write_text("2780", encoding="utf-8")
        assert _discover_fan_inputs(hwmon) == (fan / "fan1_input",)

    def test_unknown_dmi_never_matches(self, tmp_path) -> None:
        dmi = tmp_path / "sys/class/dmi/id"
        dmi.mkdir(parents=True)
        (dmi / "sys_vendor").write_text("Example Corp", encoding="utf-8")
        (dmi / "product_name").write_text("Generic Laptop", encoding="utf-8")
        assert HonorToolsAdapter(root_path=tmp_path).detect_platform().matched is False

    def test_unknown_honor_model_never_matches(self, tmp_path) -> None:
        dmi = tmp_path / "sys/class/dmi/id"
        cpu = tmp_path / "sys/devices/system/cpu/cpu0"
        dmi.mkdir(parents=True)
        cpu.mkdir(parents=True)
        (dmi / "sys_vendor").write_text("HONOR", encoding="utf-8")
        (dmi / "product_name").write_text("UnknownBook", encoding="utf-8")
        (cpu / "model_name").write_text(
            "Intel(R) Core(TM) Ultra 5 125H", encoding="utf-8"
        )
        assert HonorToolsAdapter(root_path=tmp_path).detect_platform().matched is False

    def test_verified_art14_identity_matches(self, tmp_path) -> None:
        dmi = tmp_path / "sys/class/dmi/id"
        cpu = tmp_path / "sys/devices/system/cpu/cpu0"
        dmi.mkdir(parents=True)
        cpu.mkdir(parents=True)
        (dmi / "sys_vendor").write_text("HONOR", encoding="utf-8")
        (dmi / "product_name").write_text("MRA-XXX", encoding="utf-8")
        (cpu / "model_name").write_text(
            "Intel(R) Core(TM) Ultra 5 125H", encoding="utf-8"
        )
        assert HonorToolsAdapter(root_path=tmp_path).detect_platform().matched is True

    def test_discovers_and_reads_non_bat0(self, tmp_path) -> None:
        supplies = tmp_path / "sys/class/power_supply"
        battery = supplies / "BAT1"
        mains = supplies / "AC"
        battery.mkdir(parents=True)
        mains.mkdir(parents=True)
        for name, value in {
            "type": "Battery",
            "capacity": "61",
            "status": "Not charging",
            "charge_control_end_threshold": "80",
            "charge_control_start_threshold": "75",
        }.items():
            (battery / name).write_text(value, encoding="utf-8")
        (mains / "type").write_text("Mains", encoding="utf-8")
        (mains / "online").write_text("1", encoding="utf-8")
        snapshot = HonorToolsAdapter(root_path=tmp_path).read_battery()
        assert snapshot.available is True
        assert snapshot.capacity_percent == 61
        assert snapshot.ac_online is True
        assert str(snapshot.status) == "not_charging"

    def test_lowering_thresholds_writes_start_before_end(self, tmp_path) -> None:
        battery = tmp_path / "sys/class/power_supply/BAT1"
        battery.mkdir(parents=True)
        for name, value in {
            "type": "Battery",
            "capacity": "61",
            "status": "Charging",
            "charge_control_end_threshold": "90",
            "charge_control_start_threshold": "85",
        }.items():
            (battery / name).write_text(value, encoding="utf-8")
        adapter = HonorToolsAdapter(root_path=tmp_path)
        result = adapter.write_battery_thresholds(60, 55)
        assert result["write_order"] == ("start", "end")
        assert result["readback_ok"] is True

    def test_raising_thresholds_writes_end_before_start(self, tmp_path) -> None:
        battery = tmp_path / "sys/class/power_supply/BAT1"
        battery.mkdir(parents=True)
        for name, value in {
            "type": "Battery",
            "capacity": "61",
            "status": "Charging",
            "charge_control_end_threshold": "60",
            "charge_control_start_threshold": "55",
        }.items():
            (battery / name).write_text(value, encoding="utf-8")
        adapter = HonorToolsAdapter(root_path=tmp_path)
        result = adapter.write_battery_thresholds(90, 85)
        assert result["write_order"] == ("end", "start")
        assert result["readback_ok"] is True
