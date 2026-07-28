"""Tests for the versioned system state store (WP-02)."""

from __future__ import annotations

import asyncio
import builtins
import pathlib

import pytest

from honor_control.backend import config_store as config_store_module
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
from honor_control.core.errors import DomainException


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

    def test_running_reload_keeps_newer_live_state_instead_of_backup(
        self, tmp_state_path: pathlib.Path
    ) -> None:
        store = ConfigStore(state_path=tmp_state_path)
        store.load()
        asyncio.run(store.update(lambda s: _set_battery(s, 70, 65, "home")))
        asyncio.run(store.update(lambda s: _set_battery(s, 80, 75, "travel")))
        tmp_state_path.write_text("invalid = {{{", encoding="utf-8")

        state = store.reload()

        assert state.battery.end_threshold == 80
        assert store.valid is False
        assert store.last_error

        recovered = ConfigStore(state_path=tmp_state_path).load()
        assert recovered.battery.end_threshold == 70


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
        assert store.state.battery.end_threshold == 80

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

    def test_failed_atomic_replace_preserves_primary_file(
        self, tmp_state_path: pathlib.Path, monkeypatch
    ) -> None:
        store = ConfigStore(state_path=tmp_state_path)
        store.load()
        asyncio.run(store.update(lambda s: _set_battery(s, 70, 65, "home")))
        original = tmp_state_path.read_bytes()

        def fail_replace(_source: str, _destination: str) -> None:
            raise OSError("interrupted rename")

        monkeypatch.setattr(
            "honor_control.backend.config_store.os.replace", fail_replace
        )
        with pytest.raises(DomainException):
            asyncio.run(store.update(lambda s: _set_battery(s, 80, 75, "travel")))

        assert tmp_state_path.read_bytes() == original
        assert store.state.battery.end_threshold == 70
        assert not list(tmp_state_path.parent.glob("*.tmp"))


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


class TestConfigStoreMigration:
    """HC-012: ordered schema migrations and strict old-document handling."""

    @staticmethod
    def _v1_document() -> dict:
        """Golden schema-version-1 document (predates gpu and touchpad)."""
        return {
            "schema_version": 1,
            "battery": {"end_threshold": 80, "start_threshold": 75, "mode": "travel"},
            "power": {
                "profile": "balanced",
                "auto_switch": {
                    "enabled": False,
                    "on_ac": "balanced",
                    "on_battery": "silent",
                    "on_ac_script": "",
                    "on_battery_script": "",
                },
                "profiles": {},
            },
            "fan": {"mode": "stock", "curves": {}},
            "gestures": {"mappings": {}, "daemon_enabled": False},
        }

    def test_v1_file_migrates_to_current(self, tmp_state_path: pathlib.Path) -> None:
        tmp_state_path.write_text(_toml_dump(self._v1_document()), encoding="utf-8")

        store = ConfigStore(state_path=tmp_state_path)
        state = store.load()

        assert store.valid is True
        assert store.last_error == ""
        assert state.schema_version == STATE_SCHEMA_VERSION
        assert state.battery.end_threshold == 80
        assert state.battery.start_threshold == 75
        assert state.battery.mode == "travel"
        # The migration steps supplied the tables introduced after v1.
        assert state.gpu.mitigation_enabled is False
        assert state.touchpad.settings == {}
        # The original older file was snapshotted before migrating.
        premigration = tmp_state_path.with_name(
            tmp_state_path.name + config_store_module.PREMIGRATION_SUFFIX
        )
        assert premigration.exists()
        assert "schema_version = 1" in premigration.read_text(encoding="utf-8")

    def test_v2_document_migrates_in_memory(self) -> None:
        data = self._v1_document()
        data["schema_version"] = 2
        data["gpu"] = {"mitigation_enabled": True}

        state = _state_from_dict(data)

        assert state.schema_version == STATE_SCHEMA_VERSION
        assert state.gpu.mitigation_enabled is True
        assert state.touchpad.settings == {}

    def test_v3_document_adds_power_recovery_marker(self) -> None:
        data = _state_to_dict(default_state())
        data["schema_version"] = 3
        data["power"].pop("recovery_required")
        data["power"].pop("recovery_profile")

        state = _state_from_dict(data)

        assert state.schema_version == STATE_SCHEMA_VERSION
        assert state.power.recovery_required is False
        assert state.power.recovery_profile == ""

    def test_current_version_file_is_not_snapshotted(
        self, tmp_state_path: pathlib.Path
    ) -> None:
        store = ConfigStore(state_path=tmp_state_path)
        store.load()
        asyncio.run(store.update(lambda s: _set_battery(s, 80, 75, "home")))

        reloaded = ConfigStore(state_path=tmp_state_path)
        state = reloaded.load()

        premigration = tmp_state_path.with_name(
            tmp_state_path.name + config_store_module.PREMIGRATION_SUFFIX
        )
        assert not premigration.exists()
        assert reloaded.valid is True
        assert state.battery.end_threshold == 80

    def test_failed_migration_keeps_store_invalid(
        self, tmp_state_path: pathlib.Path
    ) -> None:
        data = self._v1_document()
        # Structurally migratable, but the migrated document can never
        # satisfy current validation: must not silently adopt defaults.
        data["battery"] = {"end_threshold": 10, "start_threshold": 90, "mode": "home"}
        tmp_state_path.write_text(_toml_dump(data), encoding="utf-8")

        store = ConfigStore(state_path=tmp_state_path)
        store.load()

        assert store.valid is False
        assert "schema 1" in store.last_error
        # The pre-migration snapshot of the original file still exists.
        premigration = tmp_state_path.with_name(
            tmp_state_path.name + config_store_module.PREMIGRATION_SUFFIX
        )
        assert premigration.exists()

    def test_missing_migration_step_is_rejected(self, monkeypatch) -> None:
        monkeypatch.delitem(config_store_module._SCHEMA_MIGRATIONS, 2)

        with pytest.raises(DomainException) as excinfo:
            _state_from_dict(self._v1_document())

        message = str(excinfo.value)
        assert "schema 1" in message
        assert "migration" in message.lower()


class TestConfigStoreUnknownKeys:
    """HC-012: unknown hardware-state keys fail closed."""

    def test_unknown_keys_make_store_invalid(
        self, tmp_state_path: pathlib.Path
    ) -> None:
        data = _state_to_dict(default_state())
        data["power"]["unknown_key"] = "foobar"
        data["bogus_section"] = {"nested": 1}
        tmp_state_path.write_text(_toml_dump(data), encoding="utf-8")

        store = ConfigStore(state_path=tmp_state_path)
        store.load()

        assert store.valid is False
        assert "power.unknown_key" in store.last_error
        assert "bogus_section" in store.last_error

    def test_unknown_keys_inside_dynamic_entries_are_rejected(
        self, tmp_state_path: pathlib.Path
    ) -> None:
        data = _state_to_dict(default_state())
        data["power"]["profiles"]["balanced"]["typo_field"] = 5
        tmp_state_path.write_text(_toml_dump(data), encoding="utf-8")

        store = ConfigStore(state_path=tmp_state_path)
        store.load()

        assert store.valid is False
        assert "power.profiles.balanced.typo_field" in store.last_error

    def test_clean_file_reports_no_unknown_keys(
        self, tmp_state_path: pathlib.Path
    ) -> None:
        store = ConfigStore(state_path=tmp_state_path)
        store.load()
        asyncio.run(store.update(lambda s: _set_battery(s, 80, 75, "home")))

        reloaded = ConfigStore(state_path=tmp_state_path)
        reloaded.load()

        assert reloaded.valid is True
        assert reloaded.unknown_keys == []


class TestConfigStoreBackupRecovery:
    """HC-013: backup inspection failures must degrade, not raise."""

    def test_backup_exists_check_permission_error(
        self, tmp_state_path: pathlib.Path, monkeypatch
    ) -> None:
        tmp_state_path.write_text("not valid toml {{{{", encoding="utf-8")

        real_exists = pathlib.Path.exists

        def guarded_exists(self, *args, **kwargs):
            if self.name.endswith(".toml.bak"):
                raise PermissionError(13, "Permission denied", str(self))
            return real_exists(self, *args, **kwargs)

        monkeypatch.setattr(pathlib.Path, "exists", guarded_exists)

        store = ConfigStore(state_path=tmp_state_path)
        state = store.load()  # must not raise a second exception

        assert store.valid is False
        assert store.last_error
        assert state == default_state()

    def test_backup_read_permission_error(
        self, tmp_state_path: pathlib.Path, monkeypatch
    ) -> None:
        tmp_state_path.write_text("not valid toml {{{{", encoding="utf-8")
        tmp_state_path.with_suffix(".toml.bak").write_text(
            _toml_dump(_state_to_dict(default_state())), encoding="utf-8"
        )

        real_open = builtins.open

        def guarded_open(file, *args, **kwargs):
            if str(file).endswith(".toml.bak"):
                raise PermissionError(13, "Permission denied", str(file))
            return real_open(file, *args, **kwargs)

        monkeypatch.setattr(builtins, "open", guarded_open)

        store = ConfigStore(state_path=tmp_state_path)
        state = store.load()  # must not raise a second exception

        assert store.valid is False
        assert store.last_error
        assert state == default_state()
