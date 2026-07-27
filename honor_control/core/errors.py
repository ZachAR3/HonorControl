"""Stable domain and transport error vocabulary.

Every hardware mutation, validation failure, authorization denial, and
transport problem maps to one of these codes.  They are string enums so
they survive serialization to D-Bus, JSON, and logs without import
dependencies.
"""

from __future__ import annotations

from enum import StrEnum

from honor_control.contract import (
    ERR_BUSY,
    ERR_DEPENDENCY,
    ERR_INTERNAL,
    ERR_INVALID_ARGUMENT,
    ERR_NOT_AUTHORIZED,
    ERR_TIMEOUT,
    ERR_UNAVAILABLE,
    ERR_UNSUPPORTED,
)


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
    """Lifecycle state of a background controller.

    ``STOP_TIMEOUT`` and ``CLEANUP_FAILED`` are *fault* states: a controller
    that ignored cancellation or whose cleanup raised must never be reported
    as a clean ``STOPPED``.  Health aggregation treats them as degraded so an
    unresolved stop/cleanup failure stays visible (and is not silently
    overwritten by a later transition).
    """

    STOPPED = "stopped"
    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"
    FAILED = "failed"
    STOP_TIMEOUT = "stop_timeout"
    CLEANUP_FAILED = "cleanup_failed"


class TransportError(StrEnum):
    """Client-side transport error classification."""

    SERVICE_UNAVAILABLE = "service_unavailable"
    TIMEOUT = "timeout"
    BUSY = "busy"
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
        DomainError.NOT_AUTHORIZED: ERR_NOT_AUTHORIZED,
        DomainError.INVALID_ARGUMENT: ERR_INVALID_ARGUMENT,
        DomainError.UNSUPPORTED: ERR_UNSUPPORTED,
        DomainError.UNAVAILABLE: ERR_UNAVAILABLE,
        DomainError.BUSY: ERR_BUSY,
        DomainError.TIMEOUT: ERR_TIMEOUT,
        DomainError.DEPENDENCY: ERR_DEPENDENCY,
        DomainError.INTERNAL: ERR_INTERNAL,
    }[code]
