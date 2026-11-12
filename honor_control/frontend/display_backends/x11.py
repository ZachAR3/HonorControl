"""X11 backend (``xrandr``): any desktop running on an X.Org session.

Refresh rate uses ``--mode/--rate``.  Colour depth uses the ``max bpc``
connector property exposed by the kernel DRM drivers (i915, amdgpu, nouveau).
"""

from __future__ import annotations

import logging
import os
import re
import shutil
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field

from honor_control.frontend.display_backends.runner import Runner, default_runner
from honor_control.frontend.display_backends.types import (
    COLOR_BITS_AUTOMATIC,
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

log = logging.getLogger("honor_control.frontend.display_backends.x11")

BINARY = "xrandr"

_OUTPUT = re.compile(r"^(?P<name>\S+) connected\b")
_MODE_LINE = re.compile(r"^\s+(?P<w>\d+)x(?P<h>\d+)(?P<interlaced>i?)\s+(?P<rest>.+)$")
_RATE_TOKEN = re.compile(r"^(?P<rate>\d+(?:\.\d+)?)(?P<flags>[*+]*)$")
_MAX_BPC = re.compile(r"^\s+max bpc:\s*(?P<value>\d+)")
_RANGE = re.compile(r"^\s+range:\s*\((?P<min>\d+),\s*(?P<max>\d+)\)")


@dataclass
class _Output:
    name: str
    modes: list[Mode] = field(default_factory=list)
    #: ``Mode.id`` -> the exact rate text xrandr printed (passed back to --rate).
    rate_text: dict[str, str] = field(default_factory=dict)
    current_bits: int | None = None
    bits_range: tuple[int, int] | None = None


def parse_xrandr(text: str) -> list[_Output]:
    """Parse ``xrandr --query --props`` into per-output data."""
    outputs: list[_Output] = []
    current: _Output | None = None
    in_bpc = False
    for line in text.splitlines():
        header = _OUTPUT.match(line)
        if header:
            current = _Output(name=header.group("name"))
            outputs.append(current)
            in_bpc = False
            continue
        if current is None:
            continue
        if line and not line[0].isspace():
            current = None  # a disconnected output or the next section
            continue
        bpc = _MAX_BPC.match(line)
        if bpc:
            current.current_bits = int(bpc.group("value"))
            in_bpc = True
            continue
        if in_bpc:
            rng = _RANGE.match(line)
            if rng:
                current.bits_range = (int(rng.group("min")), int(rng.group("max")))
                in_bpc = False
                continue
        mode = _MODE_LINE.match(line)
        if not mode or mode.group("interlaced"):
            continue
        width, height = int(mode.group("w")), int(mode.group("h"))
        for token in mode.group("rest").split():
            rate = _RATE_TOKEN.match(token)
            if not rate:
                continue
            mode_id = f"{width}x{height}@{rate.group('rate')}"
            current.modes.append(
                Mode(
                    id=mode_id,
                    width=width,
                    height=height,
                    refresh=float(rate.group("rate")),
                    current="*" in rate.group("flags"),
                )
            )
            current.rate_text[mode_id] = rate.group("rate")
    return outputs


class X11Backend:
    name = "X11 (xrandr)"

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
        # On a Wayland session xrandr only talks to XWayland and cannot
        # change the real display, so it is not offered there.
        if self._env.get("XDG_SESSION_TYPE", "").lower() == "wayland":
            return False
        return bool(self._env.get("DISPLAY")) and self._which(BINARY) is not None

    def _read(self) -> _Output | None:
        code, stdout = self._runner([BINARY, "--query", "--props"])
        if code != 0:
            return None
        return next(
            (
                output
                for output in parse_xrandr(stdout)
                if is_internal_connector(output.name) and output.modes
            ),
            None,
        )

    def query(self) -> DisplayCapabilities | None:
        output = self._read()
        if output is None:
            return None
        bits: tuple[int, ...] = ()
        if output.bits_range is not None:
            bits = selectable_color_bits(*output.bits_range)
        return DisplayCapabilities(
            backend=self.name,
            output=output.name,
            refresh_rates=available_refresh_rates(output.modes),
            color_bits=bits,
        )

    def apply(self, preference: DisplayPreference) -> ApplyResult:
        output = self._read()
        if output is None:
            return ApplyResult(False, message="could not read the display state")
        arguments: list[str] = []

        if preference.refresh_hz > 0:
            target = pick_refresh_mode(output.modes, preference.refresh_hz)
            current = current_mode(output.modes)
            if target is not None and (current is None or target.id != current.id):
                arguments += [
                    "--mode",
                    f"{target.width}x{target.height}",
                    "--rate",
                    output.rate_text[target.id],
                ]

        if (
            output.bits_range is not None
            and preference.color_bits not in (0, COLOR_BITS_AUTOMATIC)
            and preference.color_bits in selectable_color_bits(*output.bits_range)
            and preference.color_bits != output.current_bits
        ):
            arguments += ["--set", "max bpc", str(preference.color_bits)]

        if not arguments:
            return ApplyResult(True, changed=False)
        code, _stdout = self._runner([BINARY, "--output", output.name, *arguments])
        if code != 0:
            return ApplyResult(False, message=f"{BINARY} failed ({code})")
        log.info("display: applied %s %s", output.name, " ".join(arguments))
        return ApplyResult(True, changed=True)
