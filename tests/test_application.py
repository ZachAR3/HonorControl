"""Tests for the application service (WP-04 feature orchestration)."""

from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest

from honor_control.backend.application import ApplicationService
from honor_control.backend.command_queue import HardwareCommandQueue
from honor_control.backend.config_store import ConfigStore
from honor_control.backend.gesture_runtime import GestureProbe, GestureRuntimeStatus
from honor_control.backend.hardware import FakeHardware
from honor_control.backend.snapshot_store import SnapshotStore
from honor_control.backend.supervisor import RuntimeSupervisor
from honor_control.core.errors import DomainException
from honor_control.core.models import FanMode, OperationStatus


def _make_service(tmp_path=None) -> ApplicationService:
    """Build an ApplicationService wired to FakeHardware."""
    hw = FakeHardware()
    config = (
        ConfigStore(state_path=str(tmp_path / "state.toml"))
        if tmp_path
        else ConfigStore(state_path="/tmp/test-honor-state.toml")
    )
    return ApplicationService(
        hardware=hw,
        config_store=config,
        snapshot_store=SnapshotStore(),
        command_queue=HardwareCommandQueue(),
        supervisor=RuntimeSupervisor(),
    )


class _StubGestureRuntime:
    def __init__(self) -> None:
        self.status = GestureRuntimeStatus()

    def probe(self) -> GestureProbe:
        return GestureProbe(
            available=True,
            device_found=True,
            device_path="/dev/hidraw-test",
            uinput_found=True,
        )

    async def run(self) -> None:
        self.status = replace(
            self.status,
            running=True,
            device_found=True,
            uinput_ready=True,
            device_path="/dev/hidraw-test",
        )
        try:
            await asyncio.Event().wait()
        finally:
            self.status = replace(self.status, running=False, uinput_ready=False)


class TestApplicationServiceReads:
    """Verify read operations and snapshot initialization."""

    def test_get_api_version(self) -> None:
        svc = _make_service()
        asyncio.run(svc.initialize())
        assert asyncio.run(svc.get_api_version()) == 1

    def test_get_schema_version(self) -> None:
        from honor_control.contract import SCHEMA_VERSION

        svc = _make_service()
        asyncio.run(svc.initialize())
        assert asyncio.run(svc.get_schema_version()) == SCHEMA_VERSION

    def test_get_snapshot_after_init(self) -> None:
        svc = _make_service()
        asyncio.run(svc.initialize())
        snap = asyncio.run(svc.get_snapshot())
        assert snap.sequence > 0
        assert snap.battery.available is True
        assert snap.platform.matched is True


class TestBatteryMutation:
    """Verify battery threshold and mode mutations."""

    def test_set_thresholds_success(self, tmp_path) -> None:
        svc = _make_service(tmp_path)
        asyncio.run(svc.initialize())
        result = asyncio.run(svc.set_battery_thresholds(80, 75))
        assert result.status == OperationStatus.SUCCESS
        assert result.applied is True
        assert result.persisted is True

    def test_set_thresholds_invalid_raises(self, tmp_path) -> None:
        svc = _make_service(tmp_path)
        asyncio.run(svc.initialize())
        with pytest.raises(DomainException):
            asyncio.run(svc.set_battery_thresholds(30, 25))

    def test_set_mode_home(self, tmp_path) -> None:
        svc = _make_service(tmp_path)
        asyncio.run(svc.initialize())
        result = asyncio.run(svc.set_battery_mode("home"))
        assert result.status == OperationStatus.SUCCESS
        assert result.applied is True

    def test_set_mode_custom_rejected(self, tmp_path) -> None:
        svc = _make_service(tmp_path)
        asyncio.run(svc.initialize())
        with pytest.raises(DomainException):
            asyncio.run(svc.set_battery_mode("custom"))


class TestPowerMutation:
    """Verify power profile mutations."""

    def test_set_profile_success(self, tmp_path) -> None:
        svc = _make_service(tmp_path)
        asyncio.run(svc.initialize())
        result = asyncio.run(svc.set_power_profile("balanced"))
        assert result.status == OperationStatus.SUCCESS
        assert result.applied is True

    def test_set_profile_invalid_name(self, tmp_path) -> None:
        svc = _make_service(tmp_path)
        asyncio.run(svc.initialize())
        with pytest.raises(DomainException):
            asyncio.run(svc.set_power_profile("1bad"))

    def test_set_auto_switch(self, tmp_path) -> None:
        svc = _make_service(tmp_path)
        asyncio.run(svc.initialize())
        result = asyncio.run(svc.set_auto_switch(True))
        assert result.status == OperationStatus.SUCCESS
        assert result.persisted is True

    def test_unknown_profile_is_rejected(self, tmp_path) -> None:
        svc = _make_service(tmp_path)
        asyncio.run(svc.initialize())
        with pytest.raises(DomainException):
            asyncio.run(svc.set_power_profile("made_up"))

    def test_auto_apply_does_not_replace_manual_profile(self, tmp_path) -> None:
        svc = _make_service(tmp_path)

        async def scenario() -> None:
            await svc.initialize()
            result = await svc._apply_power_profile(  # noqa: SLF001
                "performance", persist_desired=False
            )
            assert result.applied is True
            assert result.persisted is False
            assert svc.config_store.state.power.profile == "balanced"
            await svc.shutdown()

        asyncio.run(scenario())

    def test_create_and_apply_custom_profile(self, tmp_path) -> None:
        svc = _make_service(tmp_path)

        async def scenario() -> None:
            await svc.initialize()
            saved = await svc.save_power_profile(
                "development",
                "Development",
                "Sustained compiler workload",
                28_000_000,
                42_000_000,
                "powersave",
                "balance_performance",
                "balanced",
                True,
                100,
            )
            assert saved.persisted is True
            assert "development" in svc.config_store.state.power.profiles
            applied = await svc.set_power_profile("development")
            assert applied.applied is True
            call = next(
                entry
                for entry in reversed(svc._hw.call_log)  # noqa: SLF001
                if entry[0] == "apply_power_profile"
            )
            assert call[1][0] == "development"
            assert call[1][1]["pl1_uw"] == 28_000_000
            snapshot = await svc.get_snapshot()
            assert snapshot.power.applied_profile == "development"
            assert any(p.name == "development" for p in snapshot.power.profiles)
            await svc.shutdown()
            reloaded = ConfigStore(state_path=tmp_path / "state.toml")
            reloaded.load()
            assert reloaded.state.power.profiles["development"].pl2_uw == 42_000_000

        asyncio.run(scenario())

    def test_builtin_profile_cannot_be_deleted(self, tmp_path) -> None:
        svc = _make_service(tmp_path)
        asyncio.run(svc.initialize())
        with pytest.raises(DomainException):
            asyncio.run(svc.delete_power_profile("balanced"))

    def test_auto_switch_configuration_persists_choices_and_scripts(
        self, tmp_path
    ) -> None:
        svc = _make_service(tmp_path)

        async def scenario() -> None:
            await svc.initialize()
            result = await svc.configure_auto_switch(
                True,
                "performance",
                "silent",
                "/usr/bin/true",
                "",
            )
            assert result.persisted is True
            policy = svc.config_store.state.power.auto_switch
            assert policy.on_ac == "performance"
            assert policy.on_battery == "silent"
            assert policy.on_ac_script == "/usr/bin/true"
            await svc.shutdown()

        asyncio.run(scenario())

    def test_auto_switch_script_rejects_relative_executable(self, tmp_path) -> None:
        svc = _make_service(tmp_path)
        asyncio.run(svc.initialize())
        with pytest.raises(DomainException):
            asyncio.run(
                svc.configure_auto_switch(
                    True, "balanced", "silent", "relative-script", ""
                )
            )

    def test_transition_script_runs_direct_executable(self, tmp_path) -> None:
        svc = _make_service(tmp_path)

        async def scenario() -> None:
            status = await svc._run_transition_script(  # noqa: SLF001
                "/usr/bin/true", transition="ac", profile="balanced"
            )
            assert status == "ac script completed"
            await svc.shutdown()

        asyncio.run(scenario())

    def test_auto_switch_applies_selected_profile_and_runs_hook_once(
        self, tmp_path
    ) -> None:
        svc = _make_service(tmp_path)

        async def scenario() -> None:
            await svc.initialize()
            await svc.start_background()
            await svc.configure_auto_switch(
                True, "performance", "silent", "/usr/bin/true", ""
            )
            await asyncio.sleep(2.1)
            snapshot = await svc.get_snapshot()
            assert snapshot.power.applied_profile == "performance"
            assert snapshot.power.auto_switch_last_script_status == "ac script completed"
            calls = sum(
                name == "apply_power_profile"
                for name, _args, _kwargs in svc._hw.call_log  # noqa: SLF001
            )
            await asyncio.sleep(2.1)
            assert (
                sum(
                    name == "apply_power_profile"
                    for name, _args, _kwargs in svc._hw.call_log  # noqa: SLF001
                )
                == calls
            )
            await svc.shutdown()

        asyncio.run(scenario())


class TestFanMutation:
    """Verify fan mode mutations."""

    def test_set_stock_auto(self, tmp_path) -> None:
        svc = _make_service(tmp_path)
        asyncio.run(svc.initialize())
        result = asyncio.run(svc.set_fan_stock_auto())
        assert result.status == OperationStatus.SUCCESS
        assert result.applied is True

    def test_set_curve_valid(self, tmp_path) -> None:
        svc = _make_service(tmp_path)
        asyncio.run(svc.initialize())
        result = asyncio.run(svc.set_fan_curve("default", "40000:0,95000:100"))
        assert result.status == OperationStatus.SUCCESS
        assert result.persisted is True

    def test_set_curve_invalid_raises(self, tmp_path) -> None:
        svc = _make_service(tmp_path)
        asyncio.run(svc.initialize())
        with pytest.raises(DomainException):
            asyncio.run(svc.set_fan_curve("default", "40000:0"))

    def test_curve_controller_publishes_computed_target(self, tmp_path) -> None:
        svc = _make_service(tmp_path)

        async def scenario() -> None:
            await svc.initialize()
            await svc.start_background()
            await svc.set_fan_curve("default", "40000:0,95000:100")
            await asyncio.sleep(2.1)
            fan = (await svc.get_snapshot()).fan
            assert fan.mode == FanMode.CURVE
            assert fan.target_speed is not None
            target = fan.target_speed
            await svc._refresh_fan()  # noqa: SLF001
            assert (await svc.get_snapshot()).fan.target_speed == target
            await svc.shutdown()

        asyncio.run(scenario())

    def test_set_manual_speed(self, tmp_path) -> None:
        svc = _make_service(tmp_path)
        asyncio.run(svc.initialize())
        result = asyncio.run(svc.set_fan_manual(50, 300))
        assert result.status == OperationStatus.SUCCESS
        assert result.applied is True
        assert result.persisted is False  # manual is never persisted

    def test_set_manual_speed_out_of_range(self, tmp_path) -> None:
        svc = _make_service(tmp_path)
        asyncio.run(svc.initialize())
        with pytest.raises(DomainException):
            asyncio.run(svc.set_fan_manual(150, 300))


class TestGestureMutation:
    """Verify gesture mapping and enable/disable."""

    def test_set_mapping_success(self, tmp_path) -> None:
        svc = _make_service(tmp_path)
        asyncio.run(svc.initialize())
        result = asyncio.run(svc.set_gesture_mapping("3:1", "leftmeta,x"))
        assert result.status == OperationStatus.SUCCESS

    def test_set_mapping_invalid_combo(self, tmp_path) -> None:
        svc = _make_service(tmp_path)
        asyncio.run(svc.initialize())
        with pytest.raises(DomainException):
            asyncio.run(svc.set_gesture_mapping("3:1", "1nv4l!d"))

    def test_set_enabled_preserves_mapping(self, tmp_path) -> None:
        svc = _make_service(tmp_path)
        asyncio.run(svc.initialize())
        # Set a custom mapping first.
        asyncio.run(svc.set_gesture_mapping("3:1", "leftmeta,x"))
        # Disable the gesture.
        result = asyncio.run(svc.set_gesture_enabled("3:1", False))
        assert result.status == OperationStatus.SUCCESS
        # Re-enable — mapping should still be there.
        result = asyncio.run(svc.set_gesture_enabled("3:1", True))
        assert result.status == OperationStatus.SUCCESS

    def test_set_all_enabled(self, tmp_path) -> None:
        svc = _make_service(tmp_path)
        asyncio.run(svc.initialize())
        result = asyncio.run(svc.set_all_gestures_enabled(True))
        assert result.status == OperationStatus.SUCCESS

    def test_mapping_does_not_write_honor_tools_config(self, tmp_path) -> None:
        svc = _make_service(tmp_path)
        asyncio.run(svc.initialize())
        hardware = svc._hw  # noqa: SLF001
        hardware.call_log.clear()
        asyncio.run(svc.set_gesture_mapping("3:1", "leftmeta,x"))
        names = [name for name, _args, _kwargs in hardware.call_log]
        assert "set_gesture_mapping" not in names

    def test_daemon_lifecycle_is_persisted_and_observable(self, tmp_path) -> None:
        runtime = _StubGestureRuntime()
        svc = ApplicationService(
            FakeHardware(),
            config_store=ConfigStore(state_path=tmp_path / "state.toml"),
            gesture_runtime=runtime,  # type: ignore[arg-type]
        )

        async def scenario() -> None:
            await svc.initialize()
            result = await svc.set_gesture_daemon_enabled(True)
            assert result.applied is True
            assert svc.config_store.state.gestures.daemon_enabled is True
            assert (await svc.get_snapshot()).gestures.daemon_running is True
            result = await svc.set_gesture_daemon_enabled(False)
            assert result.applied is True
            assert (await svc.get_snapshot()).gestures.daemon_running is False
            await svc.shutdown()

        asyncio.run(scenario())


class TestGpuMutation:
    """Verify GPU mitigation enable/disable."""

    def test_enable_mitigation(self, tmp_path) -> None:
        svc = _make_service(tmp_path)
        asyncio.run(svc.initialize())
        result = asyncio.run(svc.set_gpu_mitigation_enabled(True))
        assert result.status == OperationStatus.SUCCESS
        assert result.applied is True

    def test_disable_mitigation(self, tmp_path) -> None:
        svc = _make_service(tmp_path)
        asyncio.run(svc.initialize())
        result = asyncio.run(svc.set_gpu_mitigation_enabled(False))
        assert result.status == OperationStatus.SUCCESS


class TestDiagnostics:
    """Verify diagnostic checks and debug bundle."""

    def test_reload_rejects_corrupt_state(self, tmp_path) -> None:
        svc = _make_service(tmp_path)

        async def scenario() -> None:
            await svc.initialize()
            (tmp_path / "state.toml").write_text(
                "schema_version = 1\n[battery]\nend_threshold = 'broken'\n",
                encoding="utf-8",
            )
            result = await svc.reload()
            assert result.status == OperationStatus.FAILED
            assert result.code == "config_invalid"
            await svc.shutdown()

        asyncio.run(scenario())

    def test_run_checks(self, tmp_path) -> None:
        svc = _make_service(tmp_path)
        asyncio.run(svc.initialize())
        result = asyncio.run(svc.run_checks())
        assert "overall" in result
        assert "checks" in result

    def test_get_debug_bundle(self, tmp_path) -> None:
        svc = _make_service(tmp_path)
        asyncio.run(svc.initialize())
        bundle = asyncio.run(svc.get_debug_bundle())
        assert "api_version" in bundle
        assert "sequence" in bundle
        assert "service" in bundle

    def test_get_recent_logs_validates_lines(self, tmp_path) -> None:
        svc = _make_service(tmp_path)
        asyncio.run(svc.initialize())
        with pytest.raises(DomainException):
            asyncio.run(svc.get_recent_logs(0))
        with pytest.raises(DomainException):
            asyncio.run(svc.get_recent_logs(501))


class TestReload:
    """Verify config reload."""

    def test_reload_success(self, tmp_path) -> None:
        svc = _make_service(tmp_path)
        asyncio.run(svc.initialize())
        result = asyncio.run(svc.reload())
        assert result.status == OperationStatus.SUCCESS
