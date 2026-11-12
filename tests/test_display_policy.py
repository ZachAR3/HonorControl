"""Tests for the session-side AC/battery display preferences."""

from __future__ import annotations

import json
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSettings  # noqa: E402

from honor_control.core.models import PowerSnapshot, SystemSnapshot  # noqa: E402
from honor_control.frontend import display_policy as dp  # noqa: E402


def _document(current: str = "1") -> dict:
    return {
        "outputs": [
            {
                "name": "HDMI-A-1",
                "type": 6,
                "connected": True,
                "enabled": True,
                "currentModeId": "1",
                "modes": [
                    {"id": "1", "refreshRate": 60, "size": {"width": 1, "height": 1}}
                ],
            },
            {
                "name": "eDP-1",
                "type": 7,
                "connected": True,
                "enabled": True,
                "currentModeId": current,
                "modes": [
                    {
                        "id": "1",
                        "refreshRate": 120,
                        "size": {"width": 3120, "height": 2080},
                    },
                    {
                        "id": "2",
                        "refreshRate": 59.97,
                        "size": {"width": 3120, "height": 2080},
                    },
                    {
                        "id": "9",
                        "refreshRate": 90,
                        "size": {"width": 1920, "height": 1200},
                    },
                ],
            },
        ]
    }


@pytest.fixture
def settings(tmp_path) -> QSettings:
    return QSettings(str(tmp_path / "display.ini"), QSettings.Format.IniFormat)


class _Runner:
    """Records kscreen-doctor invocations and serves a JSON document."""

    def __init__(self, document: dict | None = None, code: int = 0) -> None:
        self.document = document if document is not None else _document()
        self.code = code
        self.calls: list[list[str]] = []

    def __call__(self, arguments: list[str]) -> tuple[int, str]:
        self.calls.append(list(arguments))
        if arguments == ["-j"]:
            return 0, json.dumps(self.document)
        return self.code, ""

    @property
    def writes(self) -> list[list[str]]:
        return [call for call in self.calls if call != ["-j"]]


def _snapshot(ac_online: bool | None) -> SystemSnapshot:
    return SystemSnapshot(power=PowerSnapshot(ac_online=ac_online))


@pytest.fixture
def enabled(monkeypatch) -> None:
    monkeypatch.delenv(dp.DISABLE_ENV, raising=False)


class TestPureHelpers:
    def test_rates_are_limited_to_the_panels_current_resolution(self) -> None:
        assert dp.available_refresh_rates(_document()) == [60, 120]

    def test_no_internal_panel_means_no_rates_and_no_arguments(self) -> None:
        document = {"outputs": [_document()["outputs"][0]]}
        assert dp.available_refresh_rates(document) == []
        assert dp.build_arguments(document, dp.DisplayPreference(60, "accuracy")) == []

    def test_refresh_and_color_target_only_the_panel(self) -> None:
        arguments = dp.build_arguments(
            _document(), dp.DisplayPreference(60, dp.COLOR_EFFICIENCY)
        )
        assert arguments == [
            "output.eDP-1.mode.2",
            "output.eDP-1.colorPowerTradeoff.preferEfficiency",
        ]

    def test_current_mode_is_not_set_again(self) -> None:
        assert dp.build_arguments(_document("2"), dp.DisplayPreference(60)) == []

    def test_unoffered_rate_is_skipped_not_approximated(self) -> None:
        # 90 Hz exists only at another resolution; never change resolution.
        assert dp.build_arguments(_document(), dp.DisplayPreference(90)) == []

    def test_noop_preference_builds_nothing(self) -> None:
        assert dp.build_arguments(_document(), dp.DisplayPreference()) == []

    def test_invalid_stored_values_are_sanitised(self, settings) -> None:
        settings.setValue("display/battery/refresh_hz", "fast")
        settings.setValue("display/battery/color", "vivid")
        assert dp.load_preference(settings, False) == dp.DisplayPreference()


class TestDisplayPolicy:
    def _policy(self, settings, runner) -> dp.DisplayPolicy:
        return dp.DisplayPolicy(settings=settings, runner=runner, synchronous=True)

    def test_applies_on_first_snapshot_and_on_transitions_only(
        self, settings, enabled
    ) -> None:
        dp.save_preference(settings, False, dp.DisplayPreference(60, "efficiency"))
        dp.save_preference(settings, True, dp.DisplayPreference(120, "accuracy"))
        runner = _Runner(_document("1"))
        policy = self._policy(settings, runner)

        policy.on_snapshot(_snapshot(False))
        assert runner.writes == [
            [
                "output.eDP-1.mode.2",
                "output.eDP-1.colorPowerTradeoff.preferEfficiency",
            ]
        ]
        policy.on_snapshot(_snapshot(False))  # ordinary refresh: untouched
        assert len(runner.writes) == 1

        runner.document = _document("2")
        policy.on_snapshot(_snapshot(True))
        assert runner.writes[-1] == [
            "output.eDP-1.mode.1",
            "output.eDP-1.colorPowerTradeoff.preferAccuracy",
        ]

    def test_unknown_ac_state_never_applies(self, settings, enabled) -> None:
        dp.save_preference(settings, False, dp.DisplayPreference(60))
        runner = _Runner()
        policy = self._policy(settings, runner)
        policy.on_snapshot(_snapshot(None))
        assert runner.calls == []

    def test_default_preferences_never_run_kscreen_doctor(
        self, settings, enabled
    ) -> None:
        runner = _Runner()
        policy = self._policy(settings, runner)
        policy.on_snapshot(_snapshot(False))
        policy.on_snapshot(_snapshot(True))
        assert runner.calls == []

    def test_editing_the_active_source_applies_immediately(
        self, settings, enabled
    ) -> None:
        runner = _Runner(_document("1"))
        policy = self._policy(settings, runner)
        policy.on_snapshot(_snapshot(False))
        policy.set_preference(True, dp.DisplayPreference(60))  # inactive source
        assert runner.writes == []
        policy.set_preference(False, dp.DisplayPreference(60))
        assert runner.writes == [["output.eDP-1.mode.2"]]
        assert policy.preference(False) == dp.DisplayPreference(60)

    def test_failure_is_reported_without_raising(self, settings, enabled) -> None:
        dp.save_preference(settings, False, dp.DisplayPreference(60))
        policy = self._policy(settings, _Runner(code=1))
        messages: list[str] = []
        policy.status_changed.connect(messages.append)
        policy.on_snapshot(_snapshot(False))
        assert messages == ["Display settings for battery could not be applied"]

    def test_kill_switch_disables_all_display_changes(
        self, settings, monkeypatch
    ) -> None:
        monkeypatch.setenv(dp.DISABLE_ENV, "1")
        dp.save_preference(settings, False, dp.DisplayPreference(60))
        runner = _Runner()
        policy = self._policy(settings, runner)
        assert policy.available is False
        policy.on_snapshot(_snapshot(False))
        policy.query_refresh_rates()
        assert runner.calls == []

    def test_query_publishes_rates(self, settings, enabled) -> None:
        policy = self._policy(settings, _Runner())
        seen: list[object] = []
        policy.refresh_rates_changed.connect(seen.append)
        policy.query_refresh_rates()
        assert seen == [[60, 120]]
        assert policy.refresh_rates == [60, 120]


class TestPowerPageDisplayCard:
    def test_page_shows_saved_choices_and_saves_edits(self, settings, enabled) -> None:
        from PySide6.QtWidgets import QApplication

        from honor_control.frontend.gui.pages.power import PowerPage
        from honor_control.frontend.gui.state import GuiState

        app = QApplication.instance() or QApplication([])
        dp.save_preference(settings, False, dp.DisplayPreference(60, "efficiency"))
        runner = _Runner(_document("1"))
        policy = dp.DisplayPolicy(settings=settings, runner=runner, synchronous=True)
        page = PowerPage(GuiState())
        page.set_display_policy(policy)
        app.processEvents()

        battery_refresh = page.display_refresh[False]
        assert [battery_refresh.itemData(i) for i in range(battery_refresh.count())] == [
            0,
            60,
            120,
        ]
        assert battery_refresh.currentData() == 60
        assert page.display_color[False].currentData() == "efficiency"
        assert page.display_refresh[True].currentData() == 0
        assert runner.writes == []  # showing the page never changes the display

        ac_refresh = page.display_refresh[True]
        ac_refresh.setCurrentIndex(ac_refresh.findData(120))
        assert policy.preference(True) == dp.DisplayPreference(120, "")
        page.deleteLater()
