"""wlroots backend (``wlr-randr``): Sway, Hyprland, river, labwc, and similar.

Needs ``wlr-randr`` 0.4 or newer for ``--json``.  Only the refresh rate is
offered: the wlr-output-management protocol has no colour-depth control.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
from collections.abc import Callable, Mapping

from honor_control.frontend.display_backends.runner import Runner, default_runner
from honor_control.frontend.display_backends.types import (
    ApplyResult,
    DisplayCapabilities,
    DisplayPreference,
    Mode,
    available_refresh_rates,
    current_mode,
    is_internal_connector,
    pick_refresh_mode,
)

log = logging.getLogger("honor_control.frontend.display_backends.wlroots")

BINARY = "wlr-randr"


class WlrootsBackend:
    name = "wlroots (wlr-randr)"

    def __init__(
        self,
        runner: Runner | None = None,
        *,
        env: Mapping[str, str] | None = None,
        which: Callable[[str], str | None] = shutil.which,
    ) -> None:
        self._runner = runner or default_runner
        self._env = env if env is not None else os.environ
        self._which = which

    def is_available(self) -> bool:
        return bool(self._env.get("WAYLAND_DISPLAY")) and (
            self._which(BINARY) is not None
        )

    def _read(self) -> tuple[str, tuple[Mode, ...]] | None:
        code, stdout = self._runner([BINARY, "--json"])
        if code != 0:
            return None
        try:
            outputs = json.loads(stdout)
        except ValueError:
            return None
        for output in outputs if isinstance(outputs, list) else ():
            name = str(output.get("name", "")) if isinstance(output, dict) else ""
            if not name or not is_internal_connector(name) or not output.get("enabled"):
                continue
            modes: list[Mode] = []
            for index, raw in enumerate(output.get("modes") or ()):
                try:
                    modes.append(
                        Mode(
                            id=str(index),
                            width=int(raw["width"]),
                            height=int(raw["height"]),
                            refresh=float(raw["refresh"]),
                            current=bool(raw.get("current")),
                        )
                    )
                except (KeyError, TypeError, ValueError):
                    continue
            return name, tuple(modes)
        return None

    def query(self) -> DisplayCapabilities | None:
        state = self._read()
        if state is None:
            return None
        name, modes = state
        return DisplayCapabilities(
            backend=self.name,
            output=name,
            refresh_rates=available_refresh_rates(modes),
        )

    def apply(self, preference: DisplayPreference) -> ApplyResult:
        state = self._read()
        if state is None:
            return ApplyResult(False, message="could not read the display state")
        name, modes = state
        if preference.refresh_hz <= 0:
            return ApplyResult(True, changed=False)
        target = pick_refresh_mode(modes, preference.refresh_hz)
        current = current_mode(modes)
        if target is None or (current is not None and target.id == current.id):
            return ApplyResult(True, changed=False)
        mode = f"{target.width}x{target.height}@{target.refresh:.3f}Hz"
        code, _stdout = self._runner([BINARY, "--output", name, "--mode", mode])
        if code != 0:
            return ApplyResult(False, message=f"{BINARY} failed ({code})")
        log.info("display: switched %s to %s", name, mode)
        return ApplyResult(True, changed=True)
