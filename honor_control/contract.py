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

# -- Object paths for feature interfaces --------------------------------------

PATH_BATTERY = f"{OBJECT_PATH}/Battery"
PATH_POWER = f"{OBJECT_PATH}/Power"
PATH_FAN = f"{OBJECT_PATH}/Fan"
PATH_GESTURES = f"{OBJECT_PATH}/Gestures"
PATH_GPU = f"{OBJECT_PATH}/Gpu"
PATH_DIAGNOSTICS = f"{OBJECT_PATH}/Diagnostics"

# -- Interface names ----------------------------------------------------------

IFACE_ROOT = BUS_NAME
IFACE_BATTERY = f"{BUS_NAME}.Battery"
IFACE_POWER = f"{BUS_NAME}.Power"
IFACE_FAN = f"{BUS_NAME}.Fan"
IFACE_GESTURES = f"{BUS_NAME}.Gestures"
IFACE_GPU = f"{BUS_NAME}.Gpu"
IFACE_DIAGNOSTICS = f"{BUS_NAME}.Diagnostics"

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
