"""KDE Plasma backend (``kscreen-doctor``), on Wayland and X11 sessions.

``kscreen-doctor -j`` supplies the modes; its human-readable ``-o`` output is
the only place that reports the colour-depth range and the colour
accuracy/efficiency trade-off, and it also tells whether the output supports
them at all.  Those strings are plain English (not translated) and are read
with a fixed locale.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from honor_control.frontend.display_backends.runner import Runner, default_runner
from honor_control.frontend.display_backends.types import (
    COLOR_ACCURACY,
    COLOR_BITS_AUTOMATIC,
    COLOR_EFFICIENCY,
    ApplyResult,
    DisplayCapabilities,
    DisplayPreference,
    Mode,
    available_refresh_rates,
    current_mode,
    is_internal_connector,
    pick_refresh_mode,
    selectable_color_bits,
)

log = logging.getLogger("honor_control.frontend.display_backends.kde")

BINARY = "kscreen-doctor"
#: KScreen output type of a built-in laptop panel.
_OUTPUT_TYPE_PANEL = 7

_ANSI = re.compile(r"\x1b\[[0-9;]*m")
_HEADER = re.compile(r"^Output:\s*\d+\s+(\S+)")
_COLOR_POWER = re.compile(
    r"Color power preference:\s*(prefer efficiency and performance|prefer accuracy)"
)
_COLOR_RESOLUTION = re.compile(
    r"Color resolution:\s*(?:(?P<explicit>\d+) bits per color"
    r"|automatic \((?P<auto>\d+)\)|unknown)"
    r"(?:,\s*range:\s*\[(?P<min>\d+);\s*(?P<max>\d+)\])?"
)
_TRADEOFF_ARGUMENT = {
    COLOR_ACCURACY: "preferAccuracy",
    COLOR_EFFICIENCY: "preferEfficiency",
}


@dataclass(frozen=True)
class _State:
    name: str
    modes: tuple[Mode, ...]
    #: Colour depths offered (empty = not supported on this output).
    color_bits: tuple[int, ...]
    #: Current explicit depth, or ``COLOR_BITS_AUTOMATIC``.
    current_bits: int
    #: ``COLOR_ACCURACY`` / ``COLOR_EFFICIENCY`` / ``None`` (unsupported).
    tradeoff: str | None


def _output_text(text: str, name: str) -> str:
    """Return the ``-o`` text block belonging to one output."""
    lines: list[str] = []
    collecting = False
    for line in _ANSI.sub("", text).splitlines():
        header = _HEADER.match(line)
        if header:
            if collecting:
                break
            collecting = header.group(1) == name
            continue
        if collecting:
            lines.append(line)
    return "\n".join(lines)


def _parse_modes(output: Mapping[str, object]) -> tuple[Mode, ...]:
    current_id = str(output.get("currentModeId", ""))
    modes: list[Mode] = []
    for raw in output.get("modes") or ():  # type: ignore[attr-defined]
        if not isinstance(raw, dict):
            continue
        size = raw.get("size") or {}
        try:
            modes.append(
                Mode(
                    id=str(raw["id"]),
                    width=int(size["width"]),
                    height=int(size["height"]),
                    refresh=float(raw["refreshRate"]),
                    current=str(raw["id"]) == current_id,
                )
            )
        except (KeyError, TypeError, ValueError):
            continue
    return tuple(modes)


class KdeBackend:
    name = "KDE Plasma (kscreen-doctor)"

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
        desktop = self._env.get("XDG_CURRENT_DESKTOP", "").upper()
        return "KDE" in desktop and self._which(BINARY) is not None

    # -- Reading --

    def _read(self) -> _State | None:
        code, stdout = self._runner([BINARY, "-j"])
        if code != 0:
            return None
        try:
            document = json.loads(stdout)
        except ValueError:
            return None
        outputs = document.get("outputs") if isinstance(document, dict) else None
        panel = next(
            (
                output
                for output in outputs or ()
                if isinstance(output, dict)
                and output.get("connected")
                and output.get("enabled")
                and (
                    output.get("type") == _OUTPUT_TYPE_PANEL
                    or is_internal_connector(str(output.get("name", "")))
                )
            ),
            None,
        )
        if panel is None or not panel.get("name"):
            return None
        name = str(panel["name"])

        color_bits: tuple[int, ...] = ()
        current_bits = COLOR_BITS_AUTOMATIC
        tradeoff: str | None = None
        code, text = self._runner([BINARY, "-o"])
        if code == 0:
            block = _output_text(text, name)
            power = _COLOR_POWER.search(block)
            if power:
                tradeoff = (
                    COLOR_EFFICIENCY
                    if "efficiency" in power.group(1)
                    else COLOR_ACCURACY
                )
            resolution = _COLOR_RESOLUTION.search(block)
            if resolution and resolution.group("min") is not None:
                color_bits = selectable_color_bits(
                    int(resolution.group("min")), int(resolution.group("max"))
                )
                if resolution.group("explicit") is not None:
                    current_bits = int(resolution.group("explicit"))
        return _State(
            name=name,
            modes=_parse_modes(panel),
            color_bits=color_bits,
            current_bits=current_bits,
            tradeoff=tradeoff,
        )

    def query(self) -> DisplayCapabilities | None:
        state = self._read()
        if state is None:
            return None
        return DisplayCapabilities(
            backend=self.name,
            output=state.name,
            refresh_rates=available_refresh_rates(state.modes),
            color_bits=state.color_bits,
            color_bits_automatic=bool(state.color_bits),
            color_priority=state.tradeoff is not None,
        )

    # -- Writing --

    def apply(self, preference: DisplayPreference) -> ApplyResult:
        state = self._read()
        if state is None:
            return ApplyResult(False, message="could not read the display state")
        prefix = f"output.{state.name}"
        arguments: list[str] = []

        if preference.refresh_hz > 0:
            target = pick_refresh_mode(state.modes, preference.refresh_hz)
            current = current_mode(state.modes)
            if target is None:
                log.info(
                    "display: %s offers no %d Hz mode at its current resolution",
                    state.name,
                    preference.refresh_hz,
                )
            elif current is None or target.id != current.id:
                arguments.append(f"{prefix}.mode.{target.id}")

        if state.color_bits and preference.color_bits != 0:
            if preference.color_bits == COLOR_BITS_AUTOMATIC:
                if state.current_bits != COLOR_BITS_AUTOMATIC:
                    arguments.append(f"{prefix}.maxbpc.automatic")
            elif (
                preference.color_bits in state.color_bits
                and preference.color_bits != state.current_bits
            ):
                arguments.append(f"{prefix}.maxbpc.{preference.color_bits}")

        wanted = _TRADEOFF_ARGUMENT.get(preference.color)
        if wanted and state.tradeoff is not None and preference.color != state.tradeoff:
            arguments.append(f"{prefix}.colorPowerTradeoff.{wanted}")

        if not arguments:
            return ApplyResult(True, changed=False)
        code, _stdout = self._runner([BINARY, *arguments])
        if code != 0:
            return ApplyResult(False, message=f"{BINARY} failed ({code})")
        log.info("display: applied %s", " ".join(arguments))
        return ApplyResult(True, changed=True)
