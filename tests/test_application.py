"""Tests for the application service (WP-04 feature orchestration)."""

from __future__ import annotations

import asyncio
import pathlib
from dataclasses import replace

import pytest

from honor_control.backend.application import ApplicationService
from honor_control.backend.command_queue import HardwareCommandQueue
from honor_control.backend.config_store import ConfigStore, FanState, PowerState
from honor_control.backend.gesture_runtime import GestureProbe, GestureRuntimeStatus
from honor_control.backend.hardware import FakeHardware
from honor_control.backend.snapshot_store import SnapshotStore
from honor_control.backend.supervisor import RuntimeSupervisor
from honor_control.backend.touchpad_firmware import (
    TouchpadApplyResult,
    TouchpadFirmwareError,
)
from honor_control.core.errors import DomainException
from honor_control.core.models import FanMode, OperationStatus
from honor_control.core.touchpad import TouchpadSetting


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


def _allow_test_hook(monkeypatch) -> None:
    """Trust the system `true` binary in UID-remapped test containers."""
    executable = pathlib.Path("/usr/bin/true").resolve()
    if all(path.stat().st_uid == 0 for path in (executable, *executable.parents)):
        return
    original = ApplicationService._validate_script_command  # noqa: SLF001

    def validate(command: str) -> list[str]:
        if command == "/usr/bin/true":
            return [command]
        return original(command)

    monkeypatch.setattr(
        ApplicationService,
        "_validate_script_command",
        staticmethod(validate),
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

    def test_startup_requests_stock_fan_control(self, tmp_path) -> None:
        svc = _make_service(tmp_path)
        asyncio.run(svc.initialize())
        assert any(name == "set_fan_auto" for name, _args, _kw in svc._hw.call_log)  # noqa: SLF001


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

    def test_applied_thresholds_report_persistence_failure(
        self, tmp_path, monkeypatch
    ) -> None:
        svc = _make_service(tmp_path)
        asyncio.run(svc.initialize())

        async def fail_update(_mutator):
            raise OSError("disk full")

        monkeypatch.setattr(svc.config_store, "update", fail_update)
        result = asyncio.run(svc.set_battery_thresholds(80, 75))

        assert result.status == OperationStatus.PARTIAL
        assert result.applied is True
        assert result.persisted is False
        assert result.code == "battery_applied_not_persisted"


class TestTouchpadFirmwareMutation:
    """Verify typed touchpad writes and desired-state persistence."""

    def test_apply_profile_persists_only_after_acceptance(self, tmp_path) -> None:
        svc = _make_service(tmp_path)
        asyncio.run(svc.initialize())

        result = asyncio.run(
            svc.apply_touchpad_settings({"edge_volume": 0, "three_finger_drag": 1})
        )

        assert result.status == OperationStatus.SUCCESS
        assert result.applied is True
        assert result.persisted is True
        assert result.details["reports_applied"] == 3
        assert svc._config.state.touchpad.settings == {  # noqa: SLF001
            "edge_volume": 0,
            "three_finger_drag": 1,
        }

    def test_invalid_profile_never_reaches_hardware(self, tmp_path) -> None:
        svc = _make_service(tmp_path)
        asyncio.run(svc.initialize())
        before = len(
            [
                entry
                for entry in svc._hw.call_log  # noqa: SLF001
                if entry[0] == "apply_touchpad_settings"
            ]
        )

        with pytest.raises(DomainException):
            asyncio.run(svc.apply_touchpad_settings({"raw_command": 1}))

        after = len(
            [
                entry
                for entry in svc._hw.call_log  # noqa: SLF001
                if entry[0] == "apply_touchpad_settings"
            ]
        )
        assert after == before

    def test_support_query_returns_named_bitmap(self, tmp_path) -> None:
        svc = _make_service(tmp_path)
        asyncio.run(svc.initialize())

        support = asyncio.run(svc.query_touchpad_support())

        assert 20 in support["supported_bits"]
        assert support["known"]["20"] == "three_finger_drag"

    def test_partial_profile_persists_only_completed_settings(
        self, tmp_path, monkeypatch
    ) -> None:
        svc = _make_service(tmp_path)
        asyncio.run(svc.initialize())
        completed = TouchpadApplyResult(
            setting=TouchpadSetting.SENSITIVITY,
            value=1,
            device_path="/dev/hidraw7",
            reports=(b"\x0e" + b"\0" * 8,),
            reports_applied=1,
            clock_synchronized=True,
        )

        def fail_apply(_settings):
            raise TouchpadFirmwareError(
                "second setting failed",
                device_path="/dev/hidraw7",
                reports_applied=1,
                total_reports=2,
                completed_settings=(completed,),
            )

        monkeypatch.setattr(svc._hw, "apply_touchpad_settings", fail_apply)  # noqa: SLF001
        result = asyncio.run(
            svc.apply_touchpad_settings({"sensitivity": 1, "edge_volume": 1})
        )

        assert result.status == OperationStatus.PARTIAL
        assert result.persisted is True
        assert svc.config_store.state.touchpad.settings == {"sensitivity": 1}


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
        self, tmp_path, monkeypatch
    ) -> None:
        _allow_test_hook(monkeypatch)
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

    def test_transition_script_runs_direct_executable(
        self, tmp_path, monkeypatch
    ) -> None:
        _allow_test_hook(monkeypatch)
        svc = _make_service(tmp_path)

        async def scenario() -> None:
            status = await svc._run_transition_script(  # noqa: SLF001
                "/usr/bin/true", transition="ac", profile="balanced"
            )
            assert status == "ac script completed"
            await svc.shutdown()

        asyncio.run(scenario())

    def test_auto_switch_applies_selected_profile_and_runs_hook_once(
        self, tmp_path, monkeypatch
    ) -> None:
        _allow_test_hook(monkeypatch)
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
            assert (
                snapshot.power.auto_switch_last_script_status == "ac script completed"
            )
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

    def test_missing_hardware_results_cannot_report_success(
        self, tmp_path, monkeypatch
    ) -> None:
        svc = _make_service(tmp_path)

        async def scenario() -> None:
            await svc.initialize()
            monkeypatch.setattr(
                svc._hw,  # noqa: SLF001
                "apply_power_profile",
                lambda _name, _definition: {"profile": "performance"},
            )
            result = await svc.set_power_profile("performance")
            assert result.status == OperationStatus.PARTIAL
            assert result.applied is False
            assert result.persisted is False
            await svc.shutdown()

        asyncio.run(scenario())

    def test_hardware_success_and_persistence_failure_are_reported_separately(
        self, tmp_path, monkeypatch
    ) -> None:
        svc = _make_service(tmp_path)

        async def fail_update(_mutator) -> None:
            raise OSError("disk full")

        async def scenario() -> None:
            await svc.initialize()
            monkeypatch.setattr(svc.config_store, "update", fail_update)
            result = await svc.set_power_profile("performance")
            assert result.status == OperationStatus.PARTIAL
            assert result.code == "power_applied_not_persisted"
            assert result.applied is True
            assert result.persisted is False
            assert svc.config_store.state.power.profile == "balanced"
            assert (await svc.get_snapshot()).power.applied_profile == "performance"
            await svc.shutdown()

        asyncio.run(scenario())

    def test_power_apply_requires_fresh_observation(
        self, tmp_path, monkeypatch
    ) -> None:
        svc = _make_service(tmp_path)

        async def scenario() -> None:
            await svc.initialize()
            monkeypatch.setattr(svc, "_refresh_power", lambda: _false_refresh())
            result = await svc.set_power_profile("performance")
            assert result.status == OperationStatus.PARTIAL
            assert result.code == "power_observation_failed"
            assert result.applied is False
            assert result.persisted is True
            await svc.shutdown()

        async def _false_refresh() -> bool:
            return False

        asyncio.run(scenario())

    def test_active_profile_save_reports_persisted_apply_failure(
        self, tmp_path
    ) -> None:
        svc = _make_service(tmp_path)

        async def scenario() -> None:
            await svc.initialize()
            svc._hw.fail_next("apply_power_profile")  # noqa: SLF001
            result = await svc.save_power_profile(
                "balanced",
                "Balanced",
                "Updated definition",
                26_000_000,
                36_000_000,
                "powersave",
                "balance_power",
                "balanced",
                True,
                100,
            )
            assert result.status == OperationStatus.PARTIAL
            assert result.code == "power_profile_saved_not_applied"
            assert result.persisted is True
            assert result.applied is False
            assert (
                svc.config_store.state.power.profiles["balanced"].pl1_uw == 26_000_000
            )
            await svc.shutdown()

        asyncio.run(scenario())

    def test_reconciliation_compares_complete_observed_definition(
        self, tmp_path
    ) -> None:
        svc = _make_service(tmp_path)
        svc._hw._power_observed["rapl"]["intel-rapl:0"][  # noqa: SLF001
            "constraint_0_power_limit_uw"
        ] = 20_000_000

        async def scenario() -> None:
            await svc.initialize()
            calls = [
                name
                for name, _args, _kwargs in svc._hw.call_log  # noqa: SLF001
            ]
            assert "apply_power_profile" in calls
            assert (await svc.get_snapshot()).power.applied_profile == "balanced"
            await svc.shutdown()

        asyncio.run(scenario())

    def test_external_power_drift_clears_applied_profile(self, tmp_path) -> None:
        svc = _make_service(tmp_path)

        async def scenario() -> None:
            await svc.initialize()
            result = await svc.set_power_profile("performance")
            assert result.applied is True
            svc._hw._power_observed["epp"]["0"] = "power"  # noqa: SLF001
            await svc._refresh_power()  # noqa: SLF001
            assert (await svc.get_snapshot()).power.applied_profile == ""
            await svc.shutdown()

        asyncio.run(scenario())

    def test_auto_switch_failures_use_bounded_backoff(
        self, tmp_path, monkeypatch
    ) -> None:
        svc = _make_service(tmp_path)
        attempts: list[str] = []

        def fail_apply(name, _definition):
            attempts.append(name)
            return {
                "profile": name,
                "governor_ok": False,
                "epp_ok": False,
                "ppd_ok": False,
                "rapl_ok": False,
                "misc_ok": False,
            }

        async def scenario() -> None:
            await svc.initialize()
            monkeypatch.setattr(svc._hw, "apply_power_profile", fail_apply)  # noqa: SLF001
            monkeypatch.setattr(
                "honor_control.backend.application.AUTO_SWITCH_POLL_SECONDS",
                0.01,
            )
            monkeypatch.setattr(
                "honor_control.backend.application.AUTO_SWITCH_MAX_RETRY_SECONDS",
                0.04,
            )
            await svc.start_background()
            await svc.configure_auto_switch(True, "performance", "silent", "", "")
            await asyncio.sleep(0.13)
            assert 2 <= len(attempts) <= 6
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

    def test_failed_safe_state_suppresses_further_curve_writes(self, tmp_path) -> None:
        svc = _make_service(tmp_path)

        async def scenario() -> None:
            await svc.initialize()
            await svc.start_background()
            await svc.set_fan_curve("default", "40000:0,95000:100")
            await asyncio.sleep(2.1)
            await svc._publish_fan_failed_safe("restore failed")  # noqa: SLF001
            svc._hw.call_log.clear()  # noqa: SLF001

            await asyncio.sleep(2.1)

            writes = {
                name
                for name, _args, _kwargs in svc._hw.call_log  # noqa: SLF001
                if name in {"set_fan_manual", "set_fan_speed"}
            }
            assert writes == set()
            await svc.shutdown()

        asyncio.run(scenario())

    def test_set_manual_speed(self, tmp_path) -> None:
        svc = _make_service(tmp_path)
        asyncio.run(svc.initialize())
        result = asyncio.run(svc.set_fan_manual(50, 300))
        assert result.status == OperationStatus.SUCCESS
        assert result.applied is True
        assert result.persisted is False  # manual is never persisted

    def test_manual_mode_failure_never_writes_speed(
        self, tmp_path, monkeypatch
    ) -> None:
        svc = _make_service(tmp_path)
        asyncio.run(svc.initialize())
        hardware = svc._hw  # noqa: SLF001
        hardware.call_log.clear()

        def reject_manual(speed: int) -> bool:
            hardware._log("set_fan_manual", speed)  # noqa: SLF001
            return False

        monkeypatch.setattr(hardware, "set_fan_manual", reject_manual)
        result = asyncio.run(svc.set_fan_manual(50, 300))
        names = [name for name, _args, _kwargs in hardware.call_log]

        assert result.status == OperationStatus.FAILED
        assert "set_fan_speed" not in names
        assert "set_fan_auto" in names

    def test_manual_speed_exception_restores_stock_auto(
        self, tmp_path
    ) -> None:
        svc = _make_service(tmp_path)

        async def scenario() -> None:
            await svc.initialize()
            svc._hw.call_log.clear()  # noqa: SLF001
            svc._hw.fail_next("set_fan_speed")  # noqa: SLF001

            result = await svc.set_fan_manual(50, 300)

            assert result.status == OperationStatus.FAILED
            assert svc._hw._fan_mode == FanMode.STOCK  # noqa: SLF001
            names = [
                name for name, _args, _kwargs in svc._hw.call_log  # noqa: SLF001
            ]
            assert names.index("set_fan_speed") < names.index("set_fan_auto")
            await svc.shutdown()

        asyncio.run(scenario())

    def test_manual_watchdog_exception_restores_stock_auto(
        self, tmp_path
    ) -> None:
        svc = _make_service(tmp_path)

        async def scenario() -> None:
            await svc.initialize()
            hardware = svc._hw  # noqa: SLF001
            hardware.set_fan_manual(50)
            hardware.set_fan_speed(50)
            hardware.fail_next("read_fan_temp")
            svc._manual_ttl_seconds = 0.01  # noqa: SLF001

            with pytest.raises(DomainException):
                await svc._manual_fan_expiry()  # noqa: SLF001

            assert hardware._fan_mode == FanMode.STOCK  # noqa: SLF001
            assert (await svc.get_snapshot()).fan.mode == FanMode.STOCK
            await svc.shutdown()

        asyncio.run(scenario())

    def test_manual_timeout_queues_stock_auto_before_later_work(
        self, tmp_path, monkeypatch
    ) -> None:
        import time

        svc = _make_service(tmp_path)

        async def scenario() -> None:
            await svc.initialize()
            hardware = svc._hw  # noqa: SLF001
            original_speed = hardware.set_fan_speed
            original_run = svc.queue.run

            def slow_speed(speed: int) -> bool:
                time.sleep(0.08)
                return original_speed(speed)

            async def short_fan_timeout(
                name,
                func,
                *args,
                timeout=10.0,
                timeout_recovery=None,
            ):
                if name == "fan_manual_speed":
                    timeout = 0.01
                return await original_run(
                    name,
                    func,
                    *args,
                    timeout=timeout,
                    timeout_recovery=timeout_recovery,
                )

            monkeypatch.setattr(hardware, "set_fan_speed", slow_speed)
            monkeypatch.setattr(svc.queue, "run", short_fan_timeout)

            result = await svc.set_fan_manual(50, 300)

            assert result.status == OperationStatus.PARTIAL
            assert result.code == "fan_manual_restore_pending"
            with pytest.raises(DomainException) as busy:
                await svc.set_auto_switch(True)
            assert str(busy.value.code) == "busy"
            recovery = svc._fan_restore_task  # noqa: SLF001
            assert recovery is not None
            assert await asyncio.wait_for(asyncio.shield(recovery), timeout=1)
            assert hardware._fan_mode == FanMode.STOCK  # noqa: SLF001
            await svc.shutdown()

        asyncio.run(scenario())

    def test_manual_apply_and_restore_failure_publish_failed_safe(
        self, tmp_path, monkeypatch
    ) -> None:
        svc = _make_service(tmp_path)

        async def scenario() -> None:
            await svc.initialize()
            hardware = svc._hw  # noqa: SLF001

            def fail_speed(speed: int) -> bool:
                hardware._log("set_fan_speed", speed)  # noqa: SLF001
                raise RuntimeError("speed write failed")

            def reject_auto() -> bool:
                hardware._log("set_fan_auto")  # noqa: SLF001
                return False

            monkeypatch.setattr(hardware, "set_fan_speed", fail_speed)
            monkeypatch.setattr(hardware, "set_fan_auto", reject_auto)

            result = await svc.set_fan_manual(50, 300)

            assert result.status == OperationStatus.PARTIAL
            assert result.code == "fan_manual_restore_failed"
            assert (await svc.get_snapshot()).fan.mode == FanMode.FAILED_SAFE
            await svc.shutdown()

        asyncio.run(scenario())

    def test_manual_speed_rejects_unsafe_hot_target(self, tmp_path) -> None:
        svc = _make_service(tmp_path)
        asyncio.run(svc.initialize())
        svc._hw._fan_temp = 96_000  # noqa: SLF001
        with pytest.raises(DomainException):
            asyncio.run(svc.set_fan_manual(80, 300))

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
        assert result.applied is False

    def test_reload_curve_to_stock_restores_hardware(self, tmp_path) -> None:
        svc = _make_service(tmp_path)

        async def scenario() -> None:
            await svc.initialize()
            await svc.start_background()
            await svc.set_fan_curve("default", "40000:20,95000:100")
            await asyncio.sleep(2.1)
            assert svc._hw._fan_mode == FanMode.MANUAL_OVERRIDE  # noqa: SLF001

            writer = ConfigStore(state_path=svc.config_store.state_path)
            writer.load()
            await writer.update(
                lambda state: replace(
                    state,
                    fan=FanState(mode="stock", curves=state.fan.curves),
                )
            )

            result = await svc.reload()

            assert result.status == OperationStatus.SUCCESS
            assert result.applied is True
            assert svc.config_store.state.fan.mode == "stock"
            assert svc._hw._fan_mode == FanMode.STOCK  # noqa: SLF001
            await svc.shutdown()

        asyncio.run(scenario())

    def test_reload_stock_to_curve_starts_controller_without_false_apply(
        self, tmp_path
    ) -> None:
        svc = _make_service(tmp_path)

        async def scenario() -> None:
            await svc.initialize()
            await svc.start_background()
            writer = ConfigStore(state_path=svc.config_store.state_path)
            writer.load()
            await writer.update(
                lambda state: replace(
                    state,
                    fan=FanState(
                        mode="curve",
                        curves={"default": "40000:20,95000:100"},
                    ),
                )
            )

            result = await svc.reload()

            assert result.status == OperationStatus.SUCCESS
            assert result.applied is False
            await asyncio.sleep(2.1)
            assert svc._hw._fan_mode == FanMode.MANUAL_OVERRIDE  # noqa: SLF001
            await svc.shutdown()

        asyncio.run(scenario())

    def test_reload_reconciles_changed_power_profile(self, tmp_path) -> None:
        svc = _make_service(tmp_path)

        async def scenario() -> None:
            await svc.initialize()
            await svc.start_background()
            writer = ConfigStore(state_path=svc.config_store.state_path)
            writer.load()
            await writer.update(
                lambda state: replace(
                    state,
                    power=PowerState(
                        profile="performance",
                        auto_switch=state.power.auto_switch,
                        profiles=state.power.profiles,
                    ),
                )
            )

            result = await svc.reload()

            assert result.status == OperationStatus.SUCCESS
            assert result.applied is True
            assert svc._hw._power_profile == "performance"  # noqa: SLF001
            await svc.shutdown()

        asyncio.run(scenario())


class TestLifecycle:
    """Verify mutation admission and teardown ordering."""

    def test_shutdown_drains_manual_transition_then_restores_stock(
        self, tmp_path, monkeypatch
    ) -> None:
        import threading

        svc = _make_service(tmp_path)
        entered = threading.Event()
        release = threading.Event()

        async def scenario() -> None:
            await svc.initialize()
            hardware = svc._hw  # noqa: SLF001
            original_speed = hardware.set_fan_speed

            def blocked_speed(speed: int) -> bool:
                entered.set()
                assert release.wait(timeout=2)
                return original_speed(speed)

            monkeypatch.setattr(hardware, "set_fan_speed", blocked_speed)
            mutation = asyncio.create_task(svc.set_fan_manual(50, 300))
            assert await asyncio.to_thread(entered.wait, 1)
            shutdown = asyncio.create_task(svc.shutdown())
            await asyncio.sleep(0)
            assert not shutdown.done()
            release.set()

            result = await mutation
            await shutdown

            assert result.applied is True
            assert hardware._fan_mode == FanMode.STOCK  # noqa: SLF001
            with pytest.raises(DomainException) as closing:
                await svc.set_auto_switch(True)
            assert str(closing.value.code) == "unavailable"

        asyncio.run(scenario())
