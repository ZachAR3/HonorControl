"""Tests for the versioned system state store (WP-02)."""

from __future__ import annotations

import asyncio
import pathlib

import pytest

from honor_control.backend.config_store import (
    STATE_SCHEMA_VERSION,
    BatteryState,
    ConfigStore,
    FanState,
    GestureMappingState,
    GesturesState,
    GpuState,
    PowerAutoSwitchState,
    PowerState,
    ServiceState,
    _state_from_dict,
    _state_to_dict,
    _toml_dump,
    default_state,
)


@pytest.fixture
def tmp_state_path(tmp_path: pathlib.Path) -> pathlib.Path:
    """Return a temporary state file path."""
    return tmp_path / "state.toml"


def _set_battery(s: ServiceState, end: int, start: int, mode: str) -> ServiceState:
    """Return a copy of ``s`` with the battery state replaced."""
    return ServiceState(
        schema_version=s.schema_version,
        battery=BatteryState(end_threshold=end, start_threshold=start, mode=mode),
        power=s.power,
        fan=s.fan,
        gestures=s.gestures,
        gpu=s.gpu,
    )


class TestConfigStoreLoad:
    """Verify loading behavior: missing file, valid round-trip, corrupt recovery."""

    def test_missing_file_uses_defaults(self, tmp_state_path: pathlib.Path) -> None:
        store = ConfigStore(state_path=tmp_state_path)
        state = store.load()
        assert state == default_state()
        assert store.valid is True

    def test_valid_round_trip(self, tmp_state_path: pathlib.Path) -> None:
        store = ConfigStore(state_path=tmp_state_path)
        store.load()
        asyncio.run(store.update(lambda s: _set_battery(s, 80, 75, "travel")))
        assert store.state.battery.end_threshold == 80

        # Reload from disk in a new store instance.
        store2 = ConfigStore(state_path=tmp_state_path)
        state2 = store2.load()
        assert state2.battery.end_threshold == 80
        assert state2.battery.mode == "travel"

    def test_corrupt_file_keeps_last_known_good(
        self, tmp_state_path: pathlib.Path
    ) -> None:
        store = ConfigStore(state_path=tmp_state_path)
        store.load()
        asyncio.run(store.update(lambda s: _set_battery(s, 60, 55, "storage")))
        assert store.state.battery.end_threshold == 60

        # Corrupt the file.
        tmp_state_path.write_text("this is not valid toml {{{{", encoding="utf-8")
        state = store.load()
        # Last-known-good state is retained.
        assert state.battery.end_threshold == 60
        assert store.valid is False

    def test_save_creates_bak_file(self, tmp_state_path: pathlib.Path) -> None:
        store = ConfigStore(state_path=tmp_state_path)
        store.load()
        # First save creates the state file.
        asyncio.run(store.update(lambda s: _set_battery(s, 70, 65, "home")))
        # Second save creates a .bak of the first version.
        asyncio.run(store.update(lambda s: _set_battery(s, 80, 75, "travel")))
        assert tmp_state_path.with_suffix(".toml.bak").exists()

    def test_fresh_process_recovers_valid_backup(
        self, tmp_state_path: pathlib.Path
    ) -> None:
        store = ConfigStore(state_path=tmp_state_path)
        store.load()
        asyncio.run(store.update(lambda s: _set_battery(s, 70, 65, "home")))
        asyncio.run(store.update(lambda s: _set_battery(s, 80, 75, "travel")))
        tmp_state_path.write_text("invalid = {{{", encoding="utf-8")

        recovered = ConfigStore(state_path=tmp_state_path)
        state = recovered.load()

        assert state.battery.end_threshold == 70
        assert recovered.valid is False
        assert recovered.last_error

        asyncio.run(recovered.update(lambda s: _set_battery(s, 90, 85, "home")))
        backup_state = ConfigStore(
            state_path=tmp_state_path.with_suffix(".toml.bak")
        ).load()
        assert backup_state.battery.end_threshold == 70


class TestConfigStoreUpdate:
    """Verify atomic update and concurrent access."""

    def test_update_is_atomic(self, tmp_state_path: pathlib.Path) -> None:
        store = ConfigStore(state_path=tmp_state_path)
        store.load()
        asyncio.run(store.update(lambda s: _set_battery(s, 80, 75, "travel")))
        assert store.state.battery.end_threshold == 80
        assert store.valid is True

    def test_concurrent_updates_preserve_both(
        self, tmp_state_path: pathlib.Path
    ) -> None:
        store = ConfigStore(state_path=tmp_state_path)
        store.load()

        def set_end(s: ServiceState) -> ServiceState:
            return _set_battery(
                s, 80, min(s.battery.start_threshold, 75), s.battery.mode
            )

        def set_start(s: ServiceState) -> ServiceState:
            return _set_battery(s, s.battery.end_threshold, 75, s.battery.mode)

        async def run_both() -> None:
            await asyncio.gather(store.update(set_end), store.update(set_start))

        asyncio.run(run_both())
        assert store.state.battery.end_threshold == 80
        assert store.state.battery.start_threshold == 75

    def test_invalid_state_raises(self, tmp_state_path: pathlib.Path) -> None:
        store = ConfigStore(state_path=tmp_state_path)
        store.load()

        with pytest.raises(Exception):
            asyncio.run(store.update(lambda s: _set_battery(s, 30, 25, "home")))
        # Old state retained.
        assert store.state.battery.end_threshold == 90

    def test_save_failure_keeps_old_state_and_marks_invalid(
        self, tmp_state_path: pathlib.Path, monkeypatch
    ) -> None:
        store = ConfigStore(state_path=tmp_state_path)
        store.load()

        def fail_save(_state: ServiceState) -> None:
            raise OSError("disk full")

        monkeypatch.setattr(store, "_save_atomic", fail_save)
        with pytest.raises(Exception):
            asyncio.run(store.update(lambda s: _set_battery(s, 80, 75, "travel")))
        assert store.state == default_state()
        assert store.valid is False
        assert "disk full" in store.last_error


class TestConfigStoreSerialization:
    """Verify TOML serialization round-trips."""

    def test_state_to_dict_and_back(self) -> None:
        original = default_state()
        d = _state_to_dict(original)
        restored = _state_from_dict(d)
        assert restored == original

    def test_toml_dump_produces_valid_toml(self) -> None:
        import tomllib

        state = default_state()
        d = _state_to_dict(state)
        toml_str = _toml_dump(d)
        parsed = tomllib.loads(toml_str)
        assert parsed["schema_version"] == state.schema_version
        assert parsed["battery"]["end_threshold"] == state.battery.end_threshold

    def test_full_nested_state_round_trips(self, tmp_state_path: pathlib.Path) -> None:
        state = ServiceState(
            battery=BatteryState(end_threshold=80, start_threshold=75, mode="travel"),
            power=PowerState(
                profile="performance",
                auto_switch=PowerAutoSwitchState(
                    enabled=True,
                    on_ac="performance",
                    on_battery="silent",
                ),
            ),
            fan=FanState(
                mode="curve",
                curves={"performance": "40000:20,95000:100"},
            ),
            gestures=GesturesState(
                mappings={
                    "3:1": GestureMappingState(
                        enabled=False,
                        mapping="leftmeta,x",
                    ),
                    "top_left": GestureMappingState(
                        enabled=True,
                        mapping="brightnessup",
                    ),
                },
                daemon_enabled=True,
            ),
            gpu=GpuState(mitigation_enabled=True),
        )
        store = ConfigStore(state_path=tmp_state_path)
        asyncio.run(store.update(lambda _current: state))

        text = tmp_state_path.read_text(encoding="utf-8")
        assert '[gestures.mappings."3:1"]' in text

        restored = ConfigStore(state_path=tmp_state_path).load()
        assert restored == state

    def test_future_schema_is_rejected(self) -> None:
        data = _state_to_dict(default_state())
        data["schema_version"] = STATE_SCHEMA_VERSION + 1
        with pytest.raises(Exception):
            _state_from_dict(data)

    def test_invalid_boolean_is_not_coerced(self) -> None:
        data = _state_to_dict(default_state())
        data["gestures"]["daemon_enabled"] = "false"
        with pytest.raises(Exception):
            _state_from_dict(data)

    def test_import_state_dict(self, tmp_state_path: pathlib.Path) -> None:
        store = ConfigStore(state_path=tmp_state_path)
        store.load()
        data = _state_to_dict(default_state())
        data["battery"]["end_threshold"] = 70
        data["battery"]["start_threshold"] = 65
        state = store.import_state_dict(data)
        assert state.battery.end_threshold == 70
        assert store.state.battery.end_threshold == 70


class TestConfigStoreNoHomeAccess:
    """Verify the store never writes to a user's home directory."""

    def test_state_path_is_not_in_home(self, tmp_state_path: pathlib.Path) -> None:
        store = ConfigStore(state_path=tmp_state_path)
        store.load()
        asyncio.run(store.update(lambda s: _set_battery(s, 80, 75, "travel")))
        home = pathlib.Path.home()
        assert not str(tmp_state_path).startswith(str(home))
