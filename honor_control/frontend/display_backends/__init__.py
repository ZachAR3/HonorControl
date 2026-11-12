"""Platform/desktop display backends for per-power-source display settings."""

from honor_control.frontend.display_backends.detect import (
    candidate_backends,
    detect_backend,
)
from honor_control.frontend.display_backends.types import (
    COLOR_ACCURACY,
    COLOR_BITS_AUTOMATIC,
    COLOR_BITS_UNCHANGED,
    COLOR_EFFICIENCY,
    COLOR_UNCHANGED,
    REFRESH_UNCHANGED,
    ApplyResult,
    DisplayBackend,
    DisplayCapabilities,
    DisplayPreference,
)

__all__ = [
    "COLOR_ACCURACY",
    "COLOR_BITS_AUTOMATIC",
    "COLOR_BITS_UNCHANGED",
    "COLOR_EFFICIENCY",
    "COLOR_UNCHANGED",
    "REFRESH_UNCHANGED",
    "ApplyResult",
    "DisplayBackend",
    "DisplayCapabilities",
    "DisplayPreference",
    "candidate_backends",
    "detect_backend",
]
