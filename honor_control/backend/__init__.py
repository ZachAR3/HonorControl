"""Backend package: the privileged root D-Bus service.

The backend imports the upstream ``honor.*`` hardware modules and exposes
their functionality over D-Bus so unprivileged frontends can drive them.
Submodules:

* :mod:`honor_control.backend.service`   — service entry point + main loop
* :mod:`honor_control.backend.dbus_api`  — sdbus interface definitions
* :mod:`honor_control.backend.polkit`    — polkit authorization checks
"""
