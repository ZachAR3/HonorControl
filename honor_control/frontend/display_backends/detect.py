"""Choose the display backend that fits the running system."""

from __future__ import annotations

import os
import sys
from collections.abc import Callable, Mapping

from honor_control.frontend.display_backends.gnome import GnomeBackend
from honor_control.frontend.display_backends.kde import KdeBackend
from honor_control.frontend.display_backends.types import DisplayBackend
from honor_control.frontend.display_backends.windows import WindowsBackend
from honor_control.frontend.display_backends.wlroots import WlrootsBackend
from honor_control.frontend.display_backends.x11 import X11Backend


def candidate_backends(
    *,
    platform: str | None = None,
    env: Mapping[str, str] | None = None,
    which: Callable[[str], str | None] | None = None,
) -> list[DisplayBackend]:
    """Return backends in priority order for this platform and session."""
    platform = platform if platform is not None else sys.platform
    environment = env if env is not None else os.environ
    if platform == "win32":
        return [WindowsBackend()]
    if not platform.startswith("linux"):
        return []
    extra = {"which": which} if which is not None else {}
    return [
        KdeBackend(env=environment, **extra),
        GnomeBackend(env=environment),
        WlrootsBackend(env=environment, **extra),
        X11Backend(env=environment, **extra),
    ]


def detect_backend(
    *,
    platform: str | None = None,
    env: Mapping[str, str] | None = None,
    which: Callable[[str], str | None] | None = None,
) -> DisplayBackend | None:
    """Return the first plausible backend, or ``None`` if there is none."""
    for backend in candidate_backends(platform=platform, env=env, which=which):
        if backend.is_available():
            return backend
    return None
