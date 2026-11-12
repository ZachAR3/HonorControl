"""Tests for the session-side AC/battery display policy and its Power page card."""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSettings  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from honor_control.core.models import PowerSnapshot, SystemSnapshot  # noqa: E402
from honor_control.frontend import display_policy as dp  # noqa: E402
from honor_control.frontend.display_backends import (  # noqa: E402
    ApplyResult,
    DisplayCapabilities,
    DisplayPreference,
)

FULL_CAPS = DisplayCapabilities(
    backend="Fake",
    output="eDP-1",
    refresh_rates=(60, 120),
    color_bits=(8, 10, 12),
    color_bits_automatic=True,
    color_priority=True,
)
REFRESH_ONLY_CAPS = DisplayCapabilities(
    backend="Fake", output="DISPLAY1", refresh_rates=(48, 60, 144)
)


class FakeBackend:
    name = "Fake"

    def __init__(self, caps=FULL_CAPS, ok: bool = True, changed: bool = True) -> None:
        self.caps = caps
        self.ok = ok
        self.changed = changed
        self.applied: list[DisplayPreference] = []

    def is_available(self) -> bool:
        return True

    def query(self):
        return self.caps

    def apply(self, preference):
        self.applied.append(preference)
        return ApplyResult(self.ok, self.changed, "" if self.ok else "boom")


@pytest.fixture
def settings(tmp_path) -> QSettings:
    return QSettings(str(tmp_path / "display.ini"), QSettings.Format.IniFormat)


@pytest.fixture(autouse=True)
def enabled(monkeypatch) -> None:
    monkeypatch.delenv(dp.DISABLE_ENV, raising=False)


def _snapshot(ac_online: bool | None) -> SystemSnapshot:
    return SystemSnapshot(power=PowerSnapshot(ac_online=ac_online))


def _policy(settings, backend=None, **kwargs) -> dp.DisplayPolicy:
    return dp.DisplayPolicy(
        settings=settings,
        backend=backend if backend is not None else FakeBackend(),
        synchronous=True,
        poll_system_power=False,
        **kwargs,
    )


class TestPreferences:
    def test_round_trip(self, settings) -> None:
        wanted = DisplayPreference(60, 10, dp.COLOR_EFFICIENCY)
        dp.save_preference(settings, False, wanted)
        assert dp.load_preference(settings, False) == wanted
        assert dp.load_preference(settings, True) == DisplayPreference()

    def test_automatic_depth_is_preserved(self, settings) -> None:
        dp.save_preference(
            settings, True, DisplayPreference(color_bits=dp.COLOR_BITS_AUTOMATIC)
        )
        assert dp.load_preference(settings, True).color_bits == dp.COLOR_BITS_AUTOMATIC

    def test_invalid_stored_values_are_sanitised(self, settings) -> None:
        settings.setValue("display/battery/refresh_hz", "fast")
        settings.setValue("display/battery/color_bits", 99)
        settings.setValue("display/battery/color", "vivid")
        assert dp.load_preference(settings, False) == DisplayPreference()


class TestPolicy:
    def test_applies_on_first_reading_and_on_transitions_only(self, settings) -> None:
        backend = FakeBackend()
        dp.save_preference(settings, False, DisplayPreference(60, 8, "efficiency"))
        dp.save_preference(settings, True, DisplayPreference(120, 12, "accuracy"))
        policy = _policy(settings, backend)

        policy.on_snapshot(_snapshot(False))
        policy.on_snapshot(_snapshot(False))  # ordinary refresh: untouched
        policy.on_snapshot(_snapshot(True))
        assert backend.applied == [
            DisplayPreference(60, 8, "efficiency"),
            DisplayPreference(120, 12, "accuracy"),
        ]

    def test_unknown_power_source_never_applies(self, settings) -> None:
        backend = FakeBackend()
        dp.save_preference(settings, False, DisplayPreference(60))
        policy = _policy(settings, backend)
        policy.on_power_source(None)
        assert backend.applied == []

    def test_default_preferences_never_call_the_backend(self, settings) -> None:
        backend = FakeBackend()
        policy = _policy(settings, backend)
        policy.on_power_source(False)
        policy.on_power_source(True)
        assert backend.applied == []

    def test_editing_the_active_source_applies_immediately(self, settings) -> None:
        backend = FakeBackend()
        policy = _policy(settings, backend)
        policy.on_power_source(False)
        policy.set_preference(True, DisplayPreference(120))  # inactive source
        assert backend.applied == []
        policy.set_preference(False, DisplayPreference(60))
        assert backend.applied == [DisplayPreference(60)]

    def test_failure_and_success_are_reported(self, settings) -> None:
        dp.save_preference(settings, False, DisplayPreference(60))
        messages: list[str] = []
        failing = _policy(settings, FakeBackend(ok=False))
        failing.status_changed.connect(messages.append)
        failing.on_power_source(False)
        assert messages == ["Display settings for battery could not be applied (boom)"]

        messages.clear()
        working = _policy(settings, FakeBackend())
        working.status_changed.connect(messages.append)
        working.on_power_source(False)
        assert messages == ["Display settings applied for battery"]

    def test_an_unchanged_display_stays_silent(self, settings) -> None:
        dp.save_preference(settings, False, DisplayPreference(60))
        messages: list[str] = []
        policy = _policy(settings, FakeBackend(changed=False))
        policy.status_changed.connect(messages.append)
        policy.on_power_source(False)
        assert messages == []

    def test_backend_exceptions_do_not_escape(self, settings) -> None:
        class Exploding(FakeBackend):
            def apply(self, preference):
                raise RuntimeError("driver crashed")

        dp.save_preference(settings, False, DisplayPreference(60))
        _policy(settings, Exploding()).on_power_source(False)

    def test_kill_switch_disables_everything(self, settings, monkeypatch) -> None:
        monkeypatch.setenv(dp.DISABLE_ENV, "1")
        backend = FakeBackend()
        dp.save_preference(settings, False, DisplayPreference(60))
        policy = _policy(settings, backend)
        assert policy.available is False and policy.disabled_by_environment
        policy.on_power_source(False)
        policy.query_capabilities()
        assert backend.applied == []

    def test_no_backend_means_unavailable(self, settings) -> None:
        policy = dp.DisplayPolicy(
            settings=settings,
            backend_factory=lambda: None,
            synchronous=True,
            poll_system_power=False,
        )
        assert policy.available is False
        policy.on_power_source(False)  # must not raise

    def test_capabilities_are_published(self, settings) -> None:
        policy = _policy(settings)
        seen: list[object] = []
        policy.capabilities_changed.connect(seen.append)
        policy.query_capabilities()
        assert seen == [FULL_CAPS]
        assert policy.capabilities == FULL_CAPS


class TestPowerPageDisplayCard:
    @pytest.fixture
    def app(self):
        return QApplication.instance() or QApplication([])

    def _page(self, app, policy):
        from honor_control.frontend.gui.pages.power import PowerPage
        from honor_control.frontend.gui.state import GuiState

        page = PowerPage(GuiState())
        page.show()  # row visibility is only meaningful for a shown page
        page.set_display_policy(policy)
        app.processEvents()
        return page

    @staticmethod
    def _choices(combo) -> list[object]:
        return [combo.itemData(i) for i in range(combo.count())]

    def test_every_supported_option_is_shown_with_the_panels_own_values(
        self, app, settings
    ) -> None:
        dp.save_preference(settings, False, DisplayPreference(60, 8, "efficiency"))
        page = self._page(app, _policy(settings))

        assert self._choices(page.display_refresh[False]) == [0, 60, 120]
        assert self._choices(page.display_bits[False]) == [0, -1, 8, 10, 12]
        assert page.display_refresh[False].currentData() == 60
        assert page.display_bits[False].currentData() == 8
        assert page.display_color[False].currentData() == "efficiency"
        for combo in (
            page.display_refresh[True],
            page.display_bits[True],
            page.display_color[True],
        ):
            assert combo.isVisibleTo(page)
        page.deleteLater()

    def test_unsupported_options_are_hidden(self, app, settings) -> None:
        page = self._page(app, _policy(settings, FakeBackend(REFRESH_ONLY_CAPS)))
        assert self._choices(page.display_refresh[True]) == [0, 48, 60, 144]
        assert page.display_refresh[True].isVisibleTo(page)
        assert not page.display_bits[True].isVisibleTo(page)
        assert not page.display_color[True].isVisibleTo(page)
        assert "DISPLAY1" in page.display_hint.text()
        page.deleteLater()

    def test_saved_values_the_panel_does_not_offer_are_kept_and_labelled(
        self, app, settings
    ) -> None:
        dp.save_preference(settings, True, DisplayPreference(90, 16))
        page = self._page(app, _policy(settings))
        refresh = page.display_refresh[True]
        assert refresh.currentData() == 90
        assert refresh.currentText() == "90 Hz (not offered here)"
        assert page.display_bits[True].currentText().endswith("(not offered here)")
        page.deleteLater()

    def test_edits_are_staged_until_applied_and_can_be_discarded(
        self, app, settings
    ) -> None:
        backend = FakeBackend()
        policy = _policy(settings, backend)
        policy.on_power_source(False)
        page = self._page(app, policy)
        assert page.pending_changes() == []

        ac_refresh = page.display_refresh[True]
        ac_refresh.setCurrentIndex(ac_refresh.findData(120))
        (change,) = page.pending_changes()
        assert change.summary == "Display on AC / battery"
        assert change.requires_service is False  # stored locally
        assert policy.preference(True) == DisplayPreference()  # not saved yet

        page.discard_pending()
        assert page.pending_changes() == []
        assert page.display_refresh[True].currentData() == 0

        ac_refresh.setCurrentIndex(ac_refresh.findData(120))
        page.apply_pending()
        assert policy.preference(True) == DisplayPreference(120, 0, "")
        assert page.pending_changes() == []
        assert backend.applied == []  # AC is not the active source
        page.deleteLater()

    def test_applying_the_active_source_changes_the_display(
        self, app, settings
    ) -> None:
        backend = FakeBackend()
        policy = _policy(settings, backend)
        policy.on_power_source(False)
        page = self._page(app, policy)
        battery_refresh = page.display_refresh[False]
        battery_refresh.setCurrentIndex(battery_refresh.findData(60))
        assert backend.applied == []
        page.apply_pending()
        assert backend.applied == [DisplayPreference(60)]
        page.deleteLater()

    def test_showing_the_page_never_changes_the_display(self, app, settings) -> None:
        backend = FakeBackend()
        page = self._page(app, _policy(settings, backend))
        assert backend.applied == []
        page.deleteLater()

    def test_without_a_backend_the_card_says_so(self, app, settings) -> None:
        policy = dp.DisplayPolicy(
            settings=settings,
            backend_factory=lambda: None,
            synchronous=True,
            poll_system_power=False,
        )
        page = self._page(app, policy)
        assert not page._display_form_widget.isVisibleTo(page)  # noqa: SLF001
        assert "no supported display backend" in page.display_status.text()
        page.deleteLater()
