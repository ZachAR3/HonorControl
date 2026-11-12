"""Per-power-source display preferences applied from the desktop session.

Refresh rate and KWin's colour accuracy/efficiency trade-off are properties of
the *user's* compositor session, so they deliberately do not live in the root
service: a system daemon has no business (and no reliable way) to reconfigure
a user's Wayland outputs.  Instead the session frontend that is already
running (the tray, or the tray embedded in the main window) watches the
service snapshot and, when the laptop moves between AC and battery, applies
the user's display preferences for that power source through
``kscreen-doctor``.

Preferences are local application settings (``QSettings``), stored under a
fixed organisation/application pair so the standalone tray and the GUI read
the same values even though they are separate Qt applications.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from PySide6.QtCore import QObject, QSettings, Signal

from honor_control.core.models import SystemSnapshot

log = logging.getLogger("honor_control.frontend.display_policy")

#: Shared settings scope (must not depend on the running QApplication's name).
SETTINGS_ORGANIZATION = "HonorLinux"
SETTINGS_APPLICATION = "Honor Control"

#: Set to ``1`` to stop the frontend from ever touching display configuration
#: (used by the test-suite, and available as a user kill-switch).
DISABLE_ENV = "HONOR_CONTROL_DISABLE_DISPLAY_POLICY"

KSCREEN_DOCTOR = "kscreen-doctor"
KSCREEN_TIMEOUT_SECONDS = 8.0

#: ``refresh_hz`` value meaning "leave the refresh rate alone".
REFRESH_UNCHANGED = 0
#: ``color`` values; the empty string means "leave it alone".
COLOR_UNCHANGED = ""
COLOR_ACCURACY = "accuracy"
COLOR_EFFICIENCY = "efficiency"
_COLOR_ARGUMENTS = {
    COLOR_ACCURACY: "preferAccuracy",
    COLOR_EFFICIENCY: "preferEfficiency",
}

#: KScreen output type of a built-in laptop panel.
_OUTPUT_TYPE_PANEL = 7
_INTERNAL_NAME_PREFIXES = ("eDP", "LVDS", "DSI")
#: Two modes are "the same rate" within this tolerance (59.94 vs 60, ...).
_REFRESH_TOLERANCE_HZ = 1.0

#: ``runner(arguments) -> (returncode, stdout)``; replaceable for tests.
Runner = Callable[[list[str]], tuple[int, str]]


@dataclass(frozen=True)
class DisplayPreference:
    """What to apply when the laptop enters one power source."""

    refresh_hz: int = REFRESH_UNCHANGED
    color: str = COLOR_UNCHANGED

    @property
    def is_noop(self) -> bool:
        return self.refresh_hz == REFRESH_UNCHANGED and self.color == COLOR_UNCHANGED


def _source_key(ac_online: bool) -> str:
    return "ac" if ac_online else "battery"


def open_settings() -> QSettings:
    """Return the settings store shared by the GUI and the standalone tray."""
    return QSettings(SETTINGS_ORGANIZATION, SETTINGS_APPLICATION)


def load_preference(settings: QSettings, ac_online: bool) -> DisplayPreference:
    """Read (and sanitise) the preference for one power source."""
    prefix = f"display/{_source_key(ac_online)}"
    try:
        refresh = int(settings.value(f"{prefix}/refresh_hz", REFRESH_UNCHANGED))
    except (TypeError, ValueError):
        refresh = REFRESH_UNCHANGED
    if not 0 <= refresh <= 1000:
        refresh = REFRESH_UNCHANGED
    color = str(settings.value(f"{prefix}/color", COLOR_UNCHANGED) or "")
    if color not in _COLOR_ARGUMENTS:
        color = COLOR_UNCHANGED
    return DisplayPreference(refresh_hz=refresh, color=color)


def save_preference(
    settings: QSettings, ac_online: bool, preference: DisplayPreference
) -> None:
    prefix = f"display/{_source_key(ac_online)}"
    settings.setValue(f"{prefix}/refresh_hz", int(preference.refresh_hz))
    settings.setValue(f"{prefix}/color", preference.color)


# -- Pure helpers over ``kscreen-doctor -j`` output ---------------------------


def internal_output(document: dict[str, Any]) -> dict[str, Any] | None:
    """Return the enabled built-in panel from a KScreen JSON document."""
    outputs = document.get("outputs")
    if not isinstance(outputs, list):
        return None
    for output in outputs:
        if not isinstance(output, dict):
            continue
        if not output.get("connected") or not output.get("enabled"):
            continue
        name = str(output.get("name", ""))
        if output.get("type") == _OUTPUT_TYPE_PANEL or name.startswith(
            _INTERNAL_NAME_PREFIXES
        ):
            return output
    return None


def _current_mode(output: dict[str, Any]) -> dict[str, Any] | None:
    current_id = str(output.get("currentModeId", ""))
    for mode in output.get("modes") or ():
        if isinstance(mode, dict) and str(mode.get("id", "")) == current_id:
            return mode
    return None


def _modes_at_current_size(output: dict[str, Any]) -> list[dict[str, Any]]:
    current = _current_mode(output)
    if current is None:
        return []
    size = current.get("size")
    return [
        mode
        for mode in output.get("modes") or ()
        if isinstance(mode, dict) and mode.get("size") == size
    ]


def _rate(mode: dict[str, Any]) -> float:
    try:
        return float(mode.get("refreshRate", 0.0))
    except (TypeError, ValueError):
        return 0.0


def available_refresh_rates(document: dict[str, Any]) -> list[int]:
    """Return the distinct refresh rates offered at the current resolution."""
    output = internal_output(document)
    if output is None:
        return []
    rates = {round(_rate(mode)) for mode in _modes_at_current_size(output)}
    return sorted(rate for rate in rates if rate > 0)


def build_arguments(
    document: dict[str, Any], preference: DisplayPreference
) -> list[str]:
    """Translate a preference into ``kscreen-doctor`` arguments.

    Only the built-in panel is touched, the resolution is never changed, and a
    refresh rate the panel does not offer at the current resolution is skipped
    rather than approximated.  Returns an empty list when nothing must change.
    """
    output = internal_output(document)
    if output is None or preference.is_noop:
        return []
    name = str(output.get("name", ""))
    if not name:
        return []
    arguments: list[str] = []
    if preference.refresh_hz != REFRESH_UNCHANGED:
        current = _current_mode(output)
        candidates = [
            mode
            for mode in _modes_at_current_size(output)
            if abs(_rate(mode) - preference.refresh_hz) <= _REFRESH_TOLERANCE_HZ
        ]
        if candidates:
            target = min(
                candidates, key=lambda mode: abs(_rate(mode) - preference.refresh_hz)
            )
            if current is None or str(target.get("id")) != str(current.get("id")):
                arguments.append(f"output.{name}.mode.{target.get('id')}")
        else:
            log.info(
                "display: %s offers no %d Hz mode at the current resolution",
                name,
                preference.refresh_hz,
            )
    color_argument = _COLOR_ARGUMENTS.get(preference.color)
    if color_argument:
        # KScreen's JSON does not expose the current trade-off, so this is set
        # unconditionally; it is idempotent.
        arguments.append(f"output.{name}.colorPowerTradeoff.{color_argument}")
    return arguments


def _run_kscreen_doctor(arguments: list[str]) -> tuple[int, str]:
    completed = subprocess.run(  # noqa: S603 - fixed binary, no shell
        [KSCREEN_DOCTOR, *arguments],
        capture_output=True,
        text=True,
        timeout=KSCREEN_TIMEOUT_SECONDS,
        check=False,
    )
    return completed.returncode, completed.stdout


class DisplayPolicy(QObject):
    """Apply display preferences when the power source changes.

    Preferences are applied on the first snapshot after start (so a session
    that begins on battery gets the battery settings), on every AC/battery
    transition, and when the user edits them.  They are *not* re-applied on
    ordinary refreshes, so a manual change in System Settings sticks until the
    next transition.
    """

    #: Human-readable outcome of the last apply (for a status line).
    status_changed = Signal(str)
    #: Refresh rates (Hz) the panel offers at its current resolution.
    refresh_rates_changed = Signal(object)

    def __init__(
        self,
        *,
        settings: QSettings | None = None,
        runner: Runner | None = None,
        synchronous: bool = False,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._settings = settings if settings is not None else open_settings()
        self._runner = runner
        self._synchronous = synchronous
        self._last_ac: bool | None = None
        self._lock = threading.Lock()
        self._refresh_rates: list[int] = []
        self._status = ""

    # -- Introspection --

    @property
    def available(self) -> bool:
        """Return whether display configuration can be driven at all."""
        if os.environ.get(DISABLE_ENV, "").strip() == "1":
            return False
        return self._runner is not None or shutil.which(KSCREEN_DOCTOR) is not None

    @property
    def refresh_rates(self) -> list[int]:
        return list(self._refresh_rates)

    @property
    def status(self) -> str:
        return self._status

    @property
    def last_ac_online(self) -> bool | None:
        return self._last_ac

    def preference(self, ac_online: bool) -> DisplayPreference:
        return load_preference(self._settings, ac_online)

    # -- Mutations --

    def set_preference(self, ac_online: bool, preference: DisplayPreference) -> None:
        """Persist a preference and apply it now if that source is active."""
        save_preference(self._settings, ac_online, preference)
        self._settings.sync()
        if self._last_ac is not None and self._last_ac == ac_online:
            self._dispatch(ac_online, preference)

    def on_snapshot(self, snapshot: SystemSnapshot) -> None:
        """Slot for ``GuiController.snapshot_received``."""
        ac_online = snapshot.power.ac_online
        if ac_online is None or ac_online == self._last_ac:
            return
        self._last_ac = ac_online
        # Another frontend process may have edited the shared settings file.
        self._settings.sync()
        self._dispatch(ac_online, load_preference(self._settings, ac_online))

    def query_refresh_rates(self) -> None:
        """Discover the panel's refresh rates (asynchronously)."""
        if not self.available:
            return
        self._start(self._query_worker)

    # -- Workers (off the Qt main thread unless ``synchronous``) --

    def _dispatch(self, ac_online: bool, preference: DisplayPreference) -> None:
        if not self.available or preference.is_noop:
            return
        self._start(lambda: self._apply_worker(ac_online, preference))

    def _start(self, work: Callable[[], None]) -> None:
        if self._synchronous:
            work()
            return
        threading.Thread(target=work, name="honor-display-policy", daemon=True).start()

    def _run(self, arguments: list[str]) -> tuple[int, str]:
        runner = self._runner or _run_kscreen_doctor
        return runner(arguments)

    def _query_document(self) -> dict[str, Any] | None:
        try:
            code, stdout = self._run(["-j"])
            if code != 0:
                return None
            document = json.loads(stdout)
        except (OSError, subprocess.SubprocessError, ValueError) as exc:
            log.warning("display: could not query outputs: %s", exc)
            return None
        return document if isinstance(document, dict) else None

    def _query_worker(self) -> None:
        with self._lock:
            document = self._query_document()
        if document is None:
            return
        rates = available_refresh_rates(document)
        self._refresh_rates = rates
        self.refresh_rates_changed.emit(rates)

    def _apply_worker(self, ac_online: bool, preference: DisplayPreference) -> None:
        source = "AC" if ac_online else "battery"
        with self._lock:
            document = self._query_document()
            if document is None:
                self._set_status(f"Display settings for {source} could not be applied")
                return
            arguments = build_arguments(document, preference)
            if not arguments:
                return
            try:
                code, _stdout = self._run(arguments)
            except (OSError, subprocess.SubprocessError) as exc:
                log.warning("display: kscreen-doctor failed: %s", exc)
                code = 1
        if code == 0:
            log.info("display: applied %s for %s", " ".join(arguments), source)
            self._set_status(f"Display settings applied for {source}")
        else:
            self._set_status(f"Display settings for {source} could not be applied")

    def _set_status(self, message: str) -> None:
        self._status = message
        self.status_changed.emit(message)
