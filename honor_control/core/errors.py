"""Stable domain and transport error vocabulary.

Every hardware mutation, validation failure, authorization denial, and
transport problem maps to one of these codes.  They are string enums so
they survive serialization to D-Bus, JSON, and logs without import
dependencies.
"""

from __future__ import annotations

from enum import StrEnum


class DomainError(StrEnum):
    """Stable error codes for application-layer failures."""

    NOT_AUTHORIZED = "not_authorized"
    INVALID_ARGUMENT = "invalid_argument"
    UNSUPPORTED = "unsupported"
    UNAVAILABLE = "unavailable"
    BUSY = "busy"
    TIMEOUT = "timeout"
    DEPENDENCY = "dependency"
    INTERNAL = "internal"


class OperationStatus(StrEnum):
    """Status of a requested mutation that reached the application layer."""

    SUCCESS = "success"
    PARTIAL = "partial"
    REJECTED = "rejected"
    UNAVAILABLE = "unavailable"
    FAILED = "failed"


class CapabilityStatus(StrEnum):
    """Why a feature is or is not available.

    ``supported`` means every probe passed and writes are safe.
    Every other value means writes must be refused.
    """

    SUPPORTED = "supported"
    UNAVAILABLE = "unavailable"
    DISABLED = "disabled"
    UNSUPPORTED = "unsupported"
    EXPERIMENTAL = "experimental"


class ControllerState(StrEnum):
    """Lifecycle state of a background controller."""

    STOPPED = "stopped"
    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"
    FAILED = "failed"


class TransportError(StrEnum):
    """Client-side transport error classification."""

    SERVICE_UNAVAILABLE = "service_unavailable"
    TIMEOUT = "timeout"
    NOT_AUTHORIZED = "not_authorized"
    INVALID_REQUEST = "invalid_request"
    FEATURE_UNAVAILABLE = "feature_unavailable"
    API_MISMATCH = "api_mismatch"
    INTERNAL = "internal"


class DomainException(Exception):
    """Application-layer exception carrying a stable error code.

    The D-Bus layer maps this to the corresponding error name.
    ``code`` is the stable :class:`DomainError`; ``message`` is safe to
    return to clients; ``detail`` is logged server-side only.
    """

    def __init__(
        self,
        code: DomainError,
        message: str = "",
        *,
        detail: str = "",
    ) -> None:
        self.code = code
        self.message = message or code.value
        self.detail = detail
        super().__init__(self.message)


def error_name_for(code: DomainError) -> str:
    """Map a domain error code to its D-Bus error name."""
    return {
        DomainError.NOT_AUTHORIZED: "org.honorlinux.Control1.Error.NotAuthorized",
        DomainError.INVALID_ARGUMENT: "org.honorlinux.Control1.Error.InvalidArgument",
        DomainError.UNSUPPORTED: "org.honorlinux.Control1.Error.Unsupported",
        DomainError.UNAVAILABLE: "org.honorlinux.Control1.Error.Unavailable",
        DomainError.BUSY: "org.honorlinux.Control1.Error.Busy",
        DomainError.TIMEOUT: "org.honorlinux.Control1.Error.Timeout",
        DomainError.DEPENDENCY: "org.honorlinux.Control1.Error.Dependency",
        DomainError.INTERNAL: "org.honorlinux.Control1.Error.Internal",
    }[code]
