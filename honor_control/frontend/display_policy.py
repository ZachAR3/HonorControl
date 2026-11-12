"""Per-power-source display preferences applied from the user's session.

Refresh rate and colour options are properties of the *user's* display
session, so they deliberately do not live in the root service: a system daemon
has no business (and no reliable way) to reconfigure a user's outputs.  The
session frontend that is already running (the tray, or the tray embedded in
the main window) watches the service snapshot and, when the laptop moves
between AC and battery, applies the user's preferences for that power source
through a platform backend (KDE, GNOME, wlroots, X11 or Windows; see
``honor_control.frontend.display_backends``).

Preferences are local application settings (``QSettings``) stored under a
fixed organisation/application pair, so the standalone tray and the GUI read
the same values even though they are separate Qt applications.
"""

from __future__ import annotations

import logging
import os
import sys
import threading
from collections.abc import Callable

from PySide6.QtCore import QObject, QSettings, QTimer, Signal

from honor_control.core.models import SystemSnapshot
from honor_control.frontend.display_backends import (
    COLOR_ACCURACY,
    COLOR_BITS_AUTOMATIC,
    COLOR_BITS_UNCHANGED,
    COLOR_EFFICIENCY,
    COLOR_UNCHANGED,
    REFRESH_UNCHANGED,
    DisplayBackend,
    DisplayCapabilities,
    DisplayPreference,
    detect_backend,
)
from honor_control.frontend.display_backends.windows import system_ac_online

log = logging.getLogger("honor_control.frontend.display_policy")

#: Shared settings scope (must not depend on the running QApplication's name).
SETTINGS_ORGANIZATION = "HonorLinux"
SETTINGS_APPLICATION = "Honor Control"

#: Set to ``1`` to stop the frontend from ever touching display configuration
#: (used by the test-suite, and available as a user kill-switch).
DISABLE_ENV = "HONOR_CONTROL_DISABLE_DISPLAY_POLICY"

#: How often the AC state is read where no service supplies it (Windows).
SYSTEM_POWER_POLL_MS = 30_000

__all__ = [
    "COLOR_ACCURACY",
    "COLOR_BITS_AUTOMATIC",
    "COLOR_BITS_UNCHANGED",
    "COLOR_EFFICIENCY",
    "COLOR_UNCHANGED",
    "DISABLE_ENV",
    "REFRESH_UNCHANGED",
    "DisplayCapabilities",
    "DisplayPolicy",
    "DisplayPreference",
    "load_preference",
    "open_settings",
    "save_preference",
]

_COLORS = (COLOR_ACCURACY, COLOR_EFFICIENCY)


def _source_key(ac_online: bool) -> str:
    return "ac" if ac_online else "battery"


def open_settings() -> QSettings:
    """Return the settings store shared by the GUI and the standalone tray."""
    return QSettings(SETTINGS_ORGANIZATION, SETTINGS_APPLICATION)


def _int_setting(settings: QSettings, key: str, default: int) -> int:
    try:
        return int(settings.value(key, default))
    except (TypeError, ValueError):
        return default


def load_preference(settings: QSettings, ac_online: bool) -> DisplayPreference:
    """Read (and sanitise) the preference for one power source."""
    prefix = f"display/{_source_key(ac_online)}"
    refresh = _int_setting(settings, f"{prefix}/refresh_hz", REFRESH_UNCHANGED)
    if not 0 <= refresh <= 1000:
        refresh = REFRESH_UNCHANGED
    bits = _int_setting(settings, f"{prefix}/color_bits", COLOR_BITS_UNCHANGED)
    if bits != COLOR_BITS_AUTOMATIC and not 0 <= bits <= 16:
        bits = COLOR_BITS_UNCHANGED
    color = str(settings.value(f"{prefix}/color", COLOR_UNCHANGED) or "")
    if color not in _COLORS:
        color = COLOR_UNCHANGED
    return DisplayPreference(refresh_hz=refresh, color_bits=bits, color=color)


def save_preference(
    settings: QSettings, ac_online: bool, preference: DisplayPreference
) -> None:
    prefix = f"display/{_source_key(ac_online)}"
    settings.setValue(f"{prefix}/refresh_hz", int(preference.refresh_hz))
    settings.setValue(f"{prefix}/color_bits", int(preference.color_bits))
    settings.setValue(f"{prefix}/color", preference.color)


class DisplayPolicy(QObject):
    """Apply display preferences when the power source changes.

    Preferences are applied on the first power-source reading after start (so
    a session that begins on battery gets the battery settings), on every
    AC/battery transition, and when the user edits the active source's
    preference.  They are *not* re-applied on ordinary refreshes, so a manual
    change in the desktop's own settings sticks until the next transition.
    """

    #: Human-readable outcome of the last apply (for a status line).
    status_changed = Signal(str)
    #: ``DisplayCapabilities`` of the built-in panel, or ``None`` if unreadable.
    capabilities_changed = Signal(object)

    def __init__(
        self,
        *,
        settings: QSettings | None = None,
        backend: DisplayBackend | None = None,
        backend_factory: Callable[[], DisplayBackend | None] = detect_backend,
        synchronous: bool = False,
        poll_system_power: bool | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._settings = settings if settings is not None else open_settings()
        self._backend = backend
        self._backend_factory = backend_factory
        self._backend_resolved = backend is not None
        self._synchronous = synchronous
        self._last_ac: bool | None = None
        self._lock = threading.Lock()
        self._capabilities: DisplayCapabilities | None = None
        self._status = ""
        self._power_timer: QTimer | None = None
        if poll_system_power is None:
            poll_system_power = sys.platform == "win32"
        if poll_system_power and self.available:
            # No service supplies the AC state on this platform; read it here.
            self._power_timer = QTimer(self)
            self._power_timer.setInterval(SYSTEM_POWER_POLL_MS)
            self._power_timer.timeout.connect(self._poll_system_power)
            self._power_timer.start()
            self._poll_system_power()

    # -- Introspection --

    @property
    def backend(self) -> DisplayBackend | None:
        if not self._backend_resolved:
            self._backend = self._backend_factory()
            self._backend_resolved = True
        return self._backend

    @property
    def available(self) -> bool:
        """Return whether display configuration can be driven at all."""
        if os.environ.get(DISABLE_ENV, "").strip() == "1":
            return False
        return self.backend is not None

    @property
    def disabled_by_environment(self) -> bool:
        return os.environ.get(DISABLE_ENV, "").strip() == "1"

    @property
    def capabilities(self) -> DisplayCapabilities | None:
        return self._capabilities

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
        self.on_power_source(snapshot.power.ac_online)

    def on_power_source(self, ac_online: bool | None) -> None:
        """Apply the matching preference when the power source changes."""
        if ac_online is None or ac_online == self._last_ac:
            return
        self._last_ac = ac_online
        # Another frontend process may have edited the shared settings file.
        self._settings.sync()
        self._dispatch(ac_online, load_preference(self._settings, ac_online))

    def query_capabilities(self) -> None:
        """Discover what the panel supports (asynchronously)."""
        if not self.available:
            return
        self._start(self._query_worker)

    # -- Workers (off the Qt main thread unless ``synchronous``) --

    def _poll_system_power(self) -> None:
        self.on_power_source(system_ac_online())

    def _dispatch(self, ac_online: bool, preference: DisplayPreference) -> None:
        if not self.available or preference.is_noop:
            return
        self._start(lambda: self._apply_worker(ac_online, preference))

    def _start(self, work: Callable[[], None]) -> None:
        if self._synchronous:
            work()
            return
        threading.Thread(target=work, name="honor-display-policy", daemon=True).start()

    def _query_worker(self) -> None:
        backend = self.backend
        if backend is None:
            return
        with self._lock:
            try:
                capabilities = backend.query()
            except Exception as exc:  # noqa: BLE001 - never let a worker die noisily
                log.warning("display: capability query failed: %s", exc)
                capabilities = None
        self._capabilities = capabilities
        self.capabilities_changed.emit(capabilities)

    def _apply_worker(self, ac_online: bool, preference: DisplayPreference) -> None:
        backend = self.backend
        if backend is None:
            return
        source = "AC" if ac_online else "battery"
        with self._lock:
            try:
                result = backend.apply(preference)
            except Exception as exc:  # noqa: BLE001
                log.warning("display: apply failed: %s", exc)
                self._set_status(f"Display settings for {source} could not be applied")
                return
        if not result.ok:
            detail = f" ({result.message})" if result.message else ""
            self._set_status(
                f"Display settings for {source} could not be applied{detail}"
            )
        elif result.changed:
            self._set_status(f"Display settings applied for {source}")

    def _set_status(self, message: str) -> None:
        self._status = message
        self.status_changed.emit(message)
