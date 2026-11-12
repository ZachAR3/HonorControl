"""GNOME backend (Mutter ``org.gnome.Mutter.DisplayConfig`` over the session bus).

Refresh rate is switched by re-submitting the current monitor layout with a
different mode id for the built-in panel.  Mutter exposes no colour-depth or
colour-priority control, so only the refresh rate is offered here.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import os
from collections.abc import Mapping, Sequence
from typing import Any, Protocol

from honor_control.frontend.display_backends.types import (
    ApplyResult,
    DisplayCapabilities,
    DisplayPreference,
    Mode,
    available_refresh_rates,
    current_mode,
    pick_refresh_mode,
)

log = logging.getLogger("honor_control.frontend.display_backends.gnome")

BUS_NAME = "org.gnome.Mutter.DisplayConfig"
OBJECT_PATH = "/org/gnome/Mutter/DisplayConfig"

#: Desktops whose compositor is Mutter (or a fork exposing the same service).
_DESKTOPS = ("GNOME", "BUDGIE", "UBUNTU", "ZORIN", "PANTHEON", "ENDLESS")

METHOD_VERIFY = 0
METHOD_PERSISTENT = 2

_STATE_SIGNATURE = "ua((ssss)a(siiddada{sv})a{sv})a(iiduba(ssss)a{sv})a{sv}"
_APPLY_SIGNATURE = "uua(iiduba(ssa{sv}))a{sv}"


class Transport(Protocol):
    """Reads and writes the display configuration (replaceable in tests)."""

    def get_current_state(self) -> tuple[Any, ...]: ...

    def apply_monitors_config(
        self, serial: int, method: int, logical_monitors: list, properties: dict
    ) -> None: ...


@functools.lru_cache(maxsize=1)
def _proxy_class():
    """Declare the Mutter interface once (sdbus classes must not be re-declared)."""
    from sdbus import DbusInterfaceCommonAsync, dbus_method_async

    class MutterDisplayConfig(
        DbusInterfaceCommonAsync, interface_name="org.gnome.Mutter.DisplayConfig"
    ):
        @dbus_method_async("", _STATE_SIGNATURE)
        async def get_current_state(self): ...

        @dbus_method_async(_APPLY_SIGNATURE, "")
        async def apply_monitors_config(self, serial, method, logical, props): ...

    return MutterDisplayConfig


class SdbusTransport:
    """Talk to Mutter with python-sdbus (already a dependency on Linux)."""

    @staticmethod
    def _proxy():
        import sdbus

        return _proxy_class().new_proxy(BUS_NAME, OBJECT_PATH, sdbus.sd_bus_open_user())

    def get_current_state(self) -> tuple[Any, ...]:
        async def _call():
            return await self._proxy().get_current_state()

        return tuple(asyncio.run(_call()))

    def apply_monitors_config(
        self, serial: int, method: int, logical_monitors: list, properties: dict
    ) -> None:
        async def _call():
            await self._proxy().apply_monitors_config(
                serial, method, logical_monitors, properties
            )

        asyncio.run(_call())


def _unwrap(value: Any) -> Any:
    """Return the payload of a D-Bus variant ``(signature, value)``."""
    if isinstance(value, tuple) and len(value) == 2 and isinstance(value[0], str):
        return value[1]
    return value


class _Panel:
    """The built-in monitor and the data needed to re-submit the layout."""

    def __init__(self, state: Sequence[Any]) -> None:
        self.serial, monitors, self.logical_monitors, props = state
        self.global_properties: Mapping[str, Any] = props
        self.monitors = monitors
        self.connector = ""
        self.modes: tuple[Mode, ...] = ()
        for ids, modes, properties in monitors:
            if bool(_unwrap(properties.get("is-builtin", False))):
                self.connector = ids[0]
                self.modes = tuple(
                    Mode(
                        id=str(mode[0]),
                        width=int(mode[1]),
                        height=int(mode[2]),
                        refresh=float(mode[3]),
                        current=bool(_unwrap(mode[6].get("is-current", False))),
                    )
                    for mode in modes
                )
                break

    def is_enabled(self) -> bool:
        return any(
            monitor[0] == self.connector
            for logical in self.logical_monitors
            for monitor in logical[5]
        )

    def _monitor_mode_id(self, connector: str) -> str:
        for ids, modes, _properties in self.monitors:
            if ids[0] == connector:
                for mode in modes:
                    if bool(_unwrap(mode[6].get("is-current", False))):
                        return str(mode[0])
        return ""

    def _monitor_properties(self, connector: str) -> dict[str, tuple[str, Any]]:
        """Carry over per-monitor settings that the apply call would reset."""
        for ids, _modes, properties in self.monitors:
            if ids[0] != connector:
                continue
            carried: dict[str, tuple[str, Any]] = {}
            if "color-mode" in properties:
                carried["color-mode"] = ("u", int(_unwrap(properties["color-mode"])))
            if bool(_unwrap(properties.get("is-underscanning", False))):
                carried["underscanning"] = ("b", True)
            return carried
        return {}

    def layout_with_mode(self, mode_id: str) -> list[tuple]:
        """Return the current layout with the panel switched to ``mode_id``."""
        layout = []
        for x, y, scale, transform, primary, monitors, _props in self.logical_monitors:
            entries = []
            for ids in monitors:
                connector = ids[0]
                entries.append(
                    (
                        connector,
                        mode_id
                        if connector == self.connector
                        else self._monitor_mode_id(connector),
                        self._monitor_properties(connector),
                    )
                )
            layout.append((x, y, scale, transform, primary, entries))
        return layout

    def properties(self) -> dict[str, tuple[str, Any]]:
        carried: dict[str, tuple[str, Any]] = {}
        if "layout-mode" in self.global_properties:
            carried["layout-mode"] = (
                "u",
                int(_unwrap(self.global_properties["layout-mode"])),
            )
        return carried


class GnomeBackend:
    name = "GNOME (Mutter)"

    def __init__(
        self,
        transport: Transport | None = None,
        *,
        env: Mapping[str, str] | None = None,
    ) -> None:
        self._transport = transport
        self._env = env if env is not None else os.environ

    def is_available(self) -> bool:
        if self._transport is not None:
            return True
        desktop = self._env.get("XDG_CURRENT_DESKTOP", "").upper()
        if not any(name in desktop for name in _DESKTOPS):
            return False
        try:
            import sdbus  # noqa: F401
        except ImportError:
            return False
        return True

    def _read(self) -> _Panel | None:
        transport = self._transport or SdbusTransport()
        try:
            state = transport.get_current_state()
        except Exception as exc:  # noqa: BLE001 - any bus failure means unavailable
            log.info("display: Mutter DisplayConfig unavailable: %s", exc)
            return None
        try:
            panel = _Panel(state)
        except (TypeError, ValueError, IndexError, KeyError) as exc:
            log.warning("display: unexpected Mutter state: %s", exc)
            return None
        return panel if panel.connector and panel.modes else None

    def query(self) -> DisplayCapabilities | None:
        panel = self._read()
        if panel is None:
            return None
        return DisplayCapabilities(
            backend=self.name,
            output=panel.connector,
            refresh_rates=available_refresh_rates(panel.modes),
        )

    def apply(self, preference: DisplayPreference) -> ApplyResult:
        panel = self._read()
        if panel is None:
            return ApplyResult(False, message="could not read the display state")
        if preference.refresh_hz <= 0:
            return ApplyResult(True, changed=False)
        target = pick_refresh_mode(panel.modes, preference.refresh_hz)
        current = current_mode(panel.modes)
        if target is None or (current is not None and target.id == current.id):
            return ApplyResult(True, changed=False)
        if not panel.is_enabled():
            return ApplyResult(True, changed=False)  # lid closed / panel off
        transport = self._transport or SdbusTransport()
        layout = panel.layout_with_mode(target.id)
        properties = panel.properties()
        try:
            transport.apply_monitors_config(
                panel.serial, METHOD_VERIFY, layout, properties
            )
            transport.apply_monitors_config(
                panel.serial, METHOD_PERSISTENT, layout, properties
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("display: ApplyMonitorsConfig failed: %s", exc)
            return ApplyResult(False, message="Mutter rejected the new display mode")
        log.info("display: switched %s to mode %s", panel.connector, target.id)
        return ApplyResult(True, changed=True)
