"""Backend package: the privileged root D-Bus service.

The backend imports the upstream ``honor.*`` hardware modules (behind
``HonorToolsAdapter``) and exposes their functionality over D-Bus so
unprivileged frontends can drive them.  Submodules:

* :mod:`honor_control.backend.service`          — service entry point + main loop
* :mod:`honor_control.backend.application`      — ApplicationService use cases
* :mod:`honor_control.backend.command_queue`    — serialized hardware queue
* :mod:`honor_control.backend.config_store`     — atomic versioned state store
* :mod:`honor_control.backend.snapshot_store`   — cached snapshots + signals
* :mod:`honor_control.backend.supervisor`       — background controller lifecycle
* :mod:`honor_control.backend.hardware`         — HonorToolsAdapter / HardwarePort
* :mod:`honor_control.backend.gesture_runtime`  — hidraw -> uinput dispatch
* :mod:`honor_control.backend.touchpad_firmware`— typed HID firmware transport
* :mod:`honor_control.backend.dbus.api`         — sdbus interface definitions
* :mod:`honor_control.backend.dbus.authorizer`  — polkit authorization checks
* :mod:`honor_control.backend.dbus.codec`       — DTO <-> D-Bus conversion
"""
