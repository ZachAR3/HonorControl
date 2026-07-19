"""Stable contract constants for the Honor Control D-Bus API.

Single source of truth for bus names, object paths, interface names, API
version, schema version, and wire keys. Both the backend service and all
clients import from here so they can never drift apart.
"""

from __future__ import annotations

#: D-Bus service / bus name served by the root backend.
BUS_NAME = "org.honorlinux.Control1"

#: Root object path exported by the backend.
OBJECT_PATH = "/org/honorlinux/Control1"

#: Major API version. Increment only on breaking D-Bus signature changes.
API_VERSION = 1

#: Snapshot schema version. Increment when snapshot field semantics change.
SCHEMA_VERSION = 4

# -- Interface names ----------------------------------------------------------

#: The service exports a single flat root interface; there are no per-feature
#: object paths or sub-interfaces.
IFACE_ROOT = BUS_NAME

# -- D-Bus error names --------------------------------------------------------

ERROR_NAMESPACE = f"{BUS_NAME}.Error"
ERR_NOT_AUTHORIZED = f"{ERROR_NAMESPACE}.NotAuthorized"
ERR_INVALID_ARGUMENT = f"{ERROR_NAMESPACE}.InvalidArgument"
ERR_UNSUPPORTED = f"{ERROR_NAMESPACE}.Unsupported"
ERR_UNAVAILABLE = f"{ERROR_NAMESPACE}.Unavailable"
ERR_BUSY = f"{ERROR_NAMESPACE}.Busy"
ERR_TIMEOUT = f"{ERROR_NAMESPACE}.Timeout"
ERR_DEPENDENCY = f"{ERROR_NAMESPACE}.Dependency"
ERR_INTERNAL = f"{ERROR_NAMESPACE}.Internal"

ALL_ERROR_NAMES = frozenset(
    {
        ERR_NOT_AUTHORIZED,
        ERR_INVALID_ARGUMENT,
        ERR_UNSUPPORTED,
        ERR_UNAVAILABLE,
        ERR_BUSY,
        ERR_TIMEOUT,
        ERR_DEPENDENCY,
        ERR_INTERNAL,
    }
)
