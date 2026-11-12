"""Backend-neutral types for per-power-source display preferences.

Every platform exposes a different subset of display controls.  A backend
therefore reports :class:`DisplayCapabilities` for the built-in panel, and the
UI offers exactly what the panel and backend support instead of a fixed list.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

#: ``refresh_hz`` meaning "leave the refresh rate alone".
REFRESH_UNCHANGED = 0
#: ``color_bits`` meaning "leave the colour depth alone".
COLOR_BITS_UNCHANGED = 0
#: ``color_bits`` meaning "let the driver/compositor choose".
COLOR_BITS_AUTOMATIC = -1
#: ``color`` values; the empty string means "leave it alone".
COLOR_UNCHANGED = ""
COLOR_ACCURACY = "accuracy"
COLOR_EFFICIENCY = "efficiency"

#: Two refresh rates are "the same" within this tolerance (59.94 vs 60, ...).
REFRESH_TOLERANCE_HZ = 1.0

#: Colour depths (bits per channel) a user can sensibly pick, ascending.
SELECTABLE_COLOR_BITS = (6, 8, 10, 12, 14, 16)

#: Connector-name prefixes of built-in laptop panels.
INTERNAL_CONNECTOR_PREFIXES = ("eDP", "LVDS", "DSI")


@dataclass(frozen=True)
class DisplayPreference:
    """What to apply when the laptop enters one power source."""

    refresh_hz: int = REFRESH_UNCHANGED
    color_bits: int = COLOR_BITS_UNCHANGED
    color: str = COLOR_UNCHANGED

    @property
    def is_noop(self) -> bool:
        return (
            self.refresh_hz == REFRESH_UNCHANGED
            and self.color_bits == COLOR_BITS_UNCHANGED
            and self.color == COLOR_UNCHANGED
        )


@dataclass(frozen=True)
class DisplayCapabilities:
    """What the built-in panel can be switched between on this system."""

    #: Human-readable backend label, for example ``"KDE Plasma (kscreen-doctor)"``.
    backend: str
    #: Connector or device name of the panel the preferences apply to.
    output: str
    #: Distinct refresh rates (Hz) at the panel's *current* resolution.
    refresh_rates: tuple[int, ...] = ()
    #: Selectable explicit colour depths (bits per channel); empty = unsupported.
    color_bits: tuple[int, ...] = ()
    #: Whether "automatic" is a valid colour depth choice.
    color_bits_automatic: bool = False
    #: Whether the colour accuracy/efficiency trade-off can be set.
    color_priority: bool = False

    @property
    def supports_refresh(self) -> bool:
        return len(self.refresh_rates) > 1

    @property
    def supports_color_bits(self) -> bool:
        return bool(self.color_bits)

    @property
    def supports_anything(self) -> bool:
        return self.supports_refresh or self.supports_color_bits or self.color_priority


@dataclass(frozen=True)
class ApplyResult:
    """Outcome of applying a preference."""

    ok: bool
    #: True when something was actually changed on the display.
    changed: bool = False
    message: str = ""


@dataclass(frozen=True)
class Mode:
    """One video mode of an output."""

    id: str
    width: int
    height: int
    refresh: float
    current: bool = False


class DisplayBackend(Protocol):
    """A platform/desktop-specific way to read and set display options."""

    #: Human-readable label shown in the UI.
    name: str

    def is_available(self) -> bool:
        """Cheap check: is this backend plausible on this system right now?"""

    def query(self) -> DisplayCapabilities | None:
        """Read the built-in panel's capabilities, or ``None`` if unreadable."""

    def apply(self, preference: DisplayPreference) -> ApplyResult:
        """Apply what is applicable of ``preference`` to the built-in panel."""


# -- Helpers shared by backends ----------------------------------------------


def is_internal_connector(name: str) -> bool:
    """Return whether a connector name looks like a built-in laptop panel."""
    return name.startswith(INTERNAL_CONNECTOR_PREFIXES)


def current_mode(modes: Sequence[Mode]) -> Mode | None:
    return next((mode for mode in modes if mode.current), None)


def modes_at_current_size(modes: Sequence[Mode]) -> list[Mode]:
    """Return the modes sharing the current resolution (never change size)."""
    current = current_mode(modes)
    if current is None:
        return []
    return [
        mode
        for mode in modes
        if (mode.width, mode.height) == (current.width, current.height)
    ]


def available_refresh_rates(modes: Sequence[Mode]) -> tuple[int, ...]:
    """Return the distinct whole-Hz rates offered at the current resolution."""
    rates = {round(mode.refresh) for mode in modes_at_current_size(modes)}
    return tuple(sorted(rate for rate in rates if rate > 0))


def pick_refresh_mode(modes: Sequence[Mode], target_hz: int) -> Mode | None:
    """Return the mode closest to ``target_hz`` at the current resolution.

    ``None`` when the panel offers nothing within tolerance; callers skip the
    change rather than approximate with a different rate.
    """
    candidates = [
        mode
        for mode in modes_at_current_size(modes)
        if abs(mode.refresh - target_hz) <= REFRESH_TOLERANCE_HZ
    ]
    if not candidates:
        return None
    return min(candidates, key=lambda mode: abs(mode.refresh - target_hz))


def selectable_color_bits(minimum: int, maximum: int) -> tuple[int, ...]:
    """Return the standard colour depths inside a driver-reported range."""
    return tuple(bits for bits in SELECTABLE_COLOR_BITS if minimum <= bits <= maximum)
