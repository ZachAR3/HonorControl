"""Client-side transport-to-domain error mapping.

Classifies D-Bus errors into stable transport error codes so the GUI,
tray, and CLI can present consistent messages.
"""

from __future__ import annotations

import logging

from honor_control.core.errors import TransportError

log = logging.getLogger("honor_control.client.errors")


class ClientError(Exception):
    """Client-side exception carrying a stable transport error code."""

    def __init__(self, code: TransportError, message: str = "") -> None:
        self.code = code
        self.message = message or code.value
        super().__init__(self.message)


def classify_dbus_error(error_name: str, message: str = "") -> ClientError:
    """Map a D-Bus error name to a :class:`ClientError`."""
    if "NotAuthorized" in error_name:
        return ClientError(TransportError.NOT_AUTHORIZED, message)
    if "InvalidArgument" in error_name:
        return ClientError(TransportError.INVALID_REQUEST, message)
    if "Unsupported" in error_name:
        return ClientError(TransportError.FEATURE_UNAVAILABLE, message)
    if "Unavailable" in error_name:
        return ClientError(TransportError.FEATURE_UNAVAILABLE, message)
    if "Busy" in error_name:
        return ClientError(TransportError.INTERNAL, message)
    if "Timeout" in error_name:
        return ClientError(TransportError.TIMEOUT, message)
    if "Dependency" in error_name:
        return ClientError(TransportError.FEATURE_UNAVAILABLE, message)
    if "Internal" in error_name:
        return ClientError(TransportError.INTERNAL, message)
    if "ServiceUnknown" in error_name or "NameHasNoOwner" in error_name:
        return ClientError(TransportError.SERVICE_UNAVAILABLE, message)
    if "Timeout" in error_name or "NoReply" in error_name:
        return ClientError(TransportError.TIMEOUT, message)
    return ClientError(TransportError.INTERNAL, message)
