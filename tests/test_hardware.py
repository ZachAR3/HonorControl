"""Tests for the hardware port and FakeHardware (WP-03)."""

from __future__ import annotations

import subprocess
import sys
import types
from types import SimpleNamespace

import pytest

from honor_control.backend.hardware import (
    FakeHardware,
    HonorToolsAdapter,
    _discover_fan_inputs,
    verify_power_definition,
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


def _make_power_adapter(tmp_path, monkeypatch):
    cpu_root = tmp_path / "sys/devices/system/cpu"
    for index in range(2):
        cpufreq = cpu_root / f"cpu{index}/cpufreq"
        cpufreq.mkdir(parents=True)
        (cpufreq / "scaling_governor").write_text("powersave", encoding="utf-8")
        (cpufreq / "energy_performance_preference").write_text(
            "balance_power", encoding="utf-8"
        )
    pstate = cpu_root / "intel_pstate"
    pstate.mkdir()
    (pstate / "no_turbo").write_text("0", encoding="utf-8")
    (pstate / "max_perf_pct").write_text("100", encoding="utf-8")

    rapl = tmp_path / "sys/class/powercap/intel-rapl:0"
    rapl.mkdir(parents=True)
    (rapl / "constraint_0_power_limit_uw").write_text("25000000", encoding="utf-8")
    (rapl / "constraint_1_power_limit_uw").write_text("35000000", encoding="utf-8")

    ppd = {"profile": "balanced", "set_ok": True}

    def run_powerprofilesctl(*args: str) -> subprocess.CompletedProcess[str]:
        if args == ("get",):
            return subprocess.CompletedProcess(
                ["powerprofilesctl", *args], 0, ppd["profile"] + "\n", ""
            )
        if args[:1] == ("set",) and ppd["set_ok"]:
            ppd["profile"] = args[1]
            return subprocess.CompletedProcess(["powerprofilesctl", *args], 0, "", "")
        return subprocess.CompletedProcess(
            ["powerprofilesctl", *args], 1, "", "set failed"
        )

    adapter = HonorToolsAdapter(root_path=tmp_path)
    adapter._platform = object()  # noqa: SLF001
    adapter._platform_detected = True  # noqa: SLF001
    monkeypatch.setattr(adapter, "_run_powerprofilesctl", run_powerprofilesctl)
    monkeypatch.setattr(
        "honor_control.backend.hardware.shutil.which",
        lambda _cmd: "/usr/bin/powerprofilesctl",
    )
    monkeypatch.setattr("honor_control.backend.hardware.PPD_SETTLE_SECONDS", 0)
    return adapter, ppd


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

    def test_verified_art14_identity_matches(self, tmp_path, monkeypatch) -> None:
        dmi = tmp_path / "sys/class/dmi/id"
        cpu = tmp_path / "sys/devices/system/cpu/cpu0"
        dmi.mkdir(parents=True)
        cpu.mkdir(parents=True)
        (dmi / "sys_vendor").write_text("HONOR", encoding="utf-8")
        (dmi / "product_name").write_text("MRA-XXX", encoding="utf-8")
        (cpu / "model_name").write_text(
            "Intel(R) Core(TM) Ultra 5 125H", encoding="utf-8"
        )
        honor_module = types.ModuleType("honor")
        honor_module.__path__ = []  # type: ignore[attr-defined]
        platform_module = types.ModuleType("honor.platform")
        platform_module.detect = lambda: SimpleNamespace(name="MRA-XXX")  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "honor", honor_module)
        monkeypatch.setitem(sys.modules, "honor.platform", platform_module)
        adapter = HonorToolsAdapter(root_path=tmp_path)
        adapter._honor_ok = True  # noqa: SLF001
        assert adapter.detect_platform().matched is True

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

    def test_power_capability_checks_every_required_resource(
        self, tmp_path, monkeypatch
    ) -> None:
        adapter, _ppd = _make_power_adapter(tmp_path, monkeypatch)
        capability = adapter.get_power_capability()
        assert capability.status == CapabilityStatus.SUPPORTED
        assert all(str(tmp_path) in path for path in capability.resources)

    def test_power_apply_writes_and_verifies_complete_definition(
        self, tmp_path, monkeypatch
    ) -> None:
        adapter, ppd = _make_power_adapter(tmp_path, monkeypatch)
        definition = {
            "pl1_uw": 35_000_000,
            "pl2_uw": 55_000_000,
            "governor": "powersave",
            "epp": "performance",
            "ppd_profile": "performance",
            "turbo_enabled": True,
            "max_perf_pct": 95,
        }
        result = adapter.apply_power_profile("performance", definition)
        assert result.get("error") is None
        assert all(
            result[key] is True
            for key in (
                "governor_ok",
                "epp_ok",
                "ppd_ok",
                "rapl_ok",
                "misc_ok",
            )
        )
        assert ppd["profile"] == "performance"
        assert all(verify_power_definition(result["observed"], definition).values())
        assert adapter.read_power().available is True

    def test_power_apply_sets_powersave_before_ppd_transition(
        self, tmp_path, monkeypatch
    ) -> None:
        adapter, _ppd = _make_power_adapter(tmp_path, monkeypatch)
        for cpu in adapter._power_cpu_dirs():  # noqa: SLF001
            (cpu / "cpufreq/scaling_governor").write_text(
                "performance", encoding="utf-8"
            )
        governors_seen_by_ppd: list[tuple[str, ...]] = []
        original_set_ppd = adapter._set_ppd_profile  # noqa: SLF001

        def set_ppd(profile: str) -> bool:
            governors_seen_by_ppd.append(
                tuple(
                    (cpu / "cpufreq/scaling_governor")
                    .read_text(encoding="utf-8")
                    .strip()
                    for cpu in adapter._power_cpu_dirs()  # noqa: SLF001
                )
            )
            return original_set_ppd(profile)

        monkeypatch.setattr(adapter, "_set_ppd_profile", set_ppd)
        definition = {
            "pl1_uw": 25_000_000,
            "pl2_uw": 35_000_000,
            "governor": "powersave",
            "epp": "balance_power",
            "ppd_profile": "balanced",
            "turbo_enabled": True,
            "max_perf_pct": 100,
        }
        result = adapter.apply_power_profile("balanced", definition)

        assert result.get("error") is None
        assert governors_seen_by_ppd == [("powersave", "powersave")]
        assert result["ppd_ok"] is True

    def test_power_apply_reports_epp_conflict_with_performance_governor(
        self, tmp_path, monkeypatch
    ) -> None:
        adapter, _ppd = _make_power_adapter(tmp_path, monkeypatch)
        original_write = adapter._write_text_verified  # noqa: SLF001

        def reject_epp_in_performance_governor(path, value):
            if path.name == "energy_performance_preference":
                governor = (
                    path.parent.joinpath("scaling_governor")
                    .read_text(encoding="utf-8")
                    .strip()
                )
                if governor == "performance":
                    return False
            return original_write(path, value)

        monkeypatch.setattr(
            adapter, "_write_text_verified", reject_epp_in_performance_governor
        )
        definition = {
            "pl1_uw": 35_000_000,
            "pl2_uw": 55_000_000,
            "governor": "performance",
            "epp": "performance",
            "ppd_profile": "performance",
            "turbo_enabled": True,
            "max_perf_pct": 100,
        }

        result = adapter.apply_power_profile("performance", definition)

        assert result["governor_ok"] is True
        assert result["epp_ok"] is False

    def test_power_apply_reports_readback_mismatch(self, tmp_path, monkeypatch) -> None:
        adapter, _ppd = _make_power_adapter(tmp_path, monkeypatch)
        original_write = adapter._write_text_verified  # noqa: SLF001

        def skip_epp_write(path, value):
            if path.name == "energy_performance_preference":
                return True
            return original_write(path, value)

        monkeypatch.setattr(adapter, "_write_text_verified", skip_epp_write)
        definition = {
            "pl1_uw": 12_000_000,
            "pl2_uw": 18_000_000,
            "governor": "powersave",
            "epp": "power",
            "ppd_profile": "power-saver",
            "turbo_enabled": True,
            "max_perf_pct": 80,
        }
        result = adapter.apply_power_profile("silent", definition)
        assert result["epp_ok"] is False
        assert result["rapl_ok"] is True
        assert result["rollback"]["ok"] is True
        assert (
            tmp_path / "sys/class/powercap/intel-rapl:0/constraint_0_power_limit_uw"
        ).read_text(encoding="utf-8") == "25000000"

    @pytest.mark.parametrize(
        ("response", "expected"),
        [
            ("0x0\n", True),
            ("0x0\x00", True),
            ("0x0\x00\n", True),
            ("Error: AE_NOT_FOUND\n", False),
            ("", False),
        ],
    )
    def test_acpi_fan_call_requires_zero_result(
        self, tmp_path, monkeypatch, response, expected
    ) -> None:
        adapter = HonorToolsAdapter(root_path=tmp_path)

        class AcpiStream:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def write(self, _value):
                return None

            def flush(self):
                return None

            def read(self):
                return response

        monkeypatch.setattr("pathlib.Path.open", lambda *_args, **_kwargs: AcpiStream())
        platform = SimpleNamespace(acpi_call_path="/proc/acpi/call")
        assert adapter._verified_acpi_call(platform, "COMMAND") is expected  # noqa: SLF001

    def test_ppd_failure_aborts_profile_power_writes(
        self, tmp_path, monkeypatch
    ) -> None:
        adapter, ppd = _make_power_adapter(tmp_path, monkeypatch)
        ppd["set_ok"] = False
        definition = {
            "pl1_uw": 12_000_000,
            "pl2_uw": 18_000_000,
            "governor": "powersave",
            "epp": "power",
            "ppd_profile": "power-saver",
            "turbo_enabled": False,
            "max_perf_pct": 70,
        }
        result = adapter.apply_power_profile("silent", definition)
        assert result["ppd_ok"] is False
        assert result["writes"]["ppd"] is False
        assert all(result["writes"]["governor_pre"].values())
        assert (
            tmp_path / "sys/class/powercap/intel-rapl:0/constraint_0_power_limit_uw"
        ).read_text(encoding="utf-8") == "25000000"

    def test_invalid_definition_is_rejected_before_hardware_writes(
        self, tmp_path, monkeypatch
    ) -> None:
        adapter, ppd = _make_power_adapter(tmp_path, monkeypatch)
        definition = {
            "pl1_uw": 12_000_000,
            "pl2_uw": 18_000_000,
            "governor": "powersave",
            "epp": "power",
            "ppd_profile": "power-saver",
            "turbo_enabled": "yes",
            "max_perf_pct": 70,
        }
        result = adapter.apply_power_profile("silent", definition)
        assert "error" in result
        assert ppd["profile"] == "balanced"


def test_incomplete_power_observation_never_verifies() -> None:
    definition = {
        "pl1_uw": 25_000_000,
        "pl2_uw": 35_000_000,
        "governor": "powersave",
        "epp": "balance_power",
        "ppd_profile": "balanced",
        "turbo_enabled": True,
        "max_perf_pct": 100,
    }
    assert not any(verify_power_definition({}, definition).values())
