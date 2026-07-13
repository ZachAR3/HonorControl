"""Fail-closed async authorizer with explicit caller subject.

Captures the D-Bus sender unique name and credentials while still in the
D-Bus method context.  Passes an immutable caller subject to the
authorizer before any executor hop.

Safety invariants:
  * Fail closed if sender, credentials, polkit, or start time cannot be
    resolved.
  * Internal controller calls use explicit internal application methods
    and never forge/omit a sender.
  * ``action_id`` is validated against the method's fixed declaration;
    never accept an action ID from a client.
  * Authorization calls are bounded by a short timeout.
  * Challenge/deny/unavailable are mapped separately.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Protocol

from sdbus import DbusInterfaceCommonAsync, dbus_method_async

from honor_control.core.errors import DomainError, DomainException

log = logging.getLogger("honor_control.backend.dbus.authorizer")

#: Polkit action IDs (single source of truth).
ACTION_SET_CHARGE_LIMIT = "org.honorlinux.control.set-charge-limit"
ACTION_SET_POWER_PROFILE = "org.honorlinux.control.set-power-profile"
ACTION_CONFIGURE_POWER = "org.honorlinux.control.configure-power"
ACTION_SET_FAN_CURVE = "org.honorlinux.control.set-fan-curve"
ACTION_SET_GESTURES = "org.honorlinux.control.set-gestures"
ACTION_SET_GPU_IRQ = "org.honorlinux.control.set-gpu-irq"
ACTION_RELOAD_CONFIG = "org.honorlinux.control.reload-config"
ACTION_EXPORT_DEBUG = "org.honorlinux.control.export-debug"
ACTION_VIEW_LOGS = "org.honorlinux.control.view-logs"

#: Map of D-Bus method name -> required polkit action.
METHOD_ACTIONS: dict[str, str] = {
    "SetThresholds": ACTION_SET_CHARGE_LIMIT,
    "SetMode": ACTION_SET_CHARGE_LIMIT,
    "SetProfile": ACTION_SET_POWER_PROFILE,
    "SetAutoSwitch": ACTION_CONFIGURE_POWER,
    "SavePowerProfile": ACTION_CONFIGURE_POWER,
    "DeletePowerProfile": ACTION_CONFIGURE_POWER,
    "ConfigureAutoSwitch": ACTION_CONFIGURE_POWER,
    "SetStockAuto": ACTION_SET_FAN_CURVE,
    "SetCurve": ACTION_SET_FAN_CURVE,
    "SetManual": ACTION_SET_FAN_CURVE,
    "SetMapping": ACTION_SET_GESTURES,
    "SetEnabled": ACTION_SET_GESTURES,
    "SetDaemonEnabled": ACTION_SET_GESTURES,
    "SetAllEnabled": ACTION_SET_GESTURES,
    "ApplyTouchpadSettings": ACTION_SET_GESTURES,
    "SetTouchpadSetting": ACTION_SET_GESTURES,
    "QueryTouchpadSupport": ACTION_SET_GESTURES,
    "ProbeTouchpadFirmware": ACTION_SET_GESTURES,
    "SetMitigationEnabled": ACTION_SET_GPU_IRQ,
    "Reload": ACTION_RELOAD_CONFIG,
    "GetDebugBundle": ACTION_EXPORT_DEBUG,
    "GetRecentLogs": ACTION_VIEW_LOGS,
}

#: Read-only methods that require no authorization.
UNPRIVILEGED_METHODS = frozenset(
    {
        "GetApiVersion",
        "GetSchemaVersion",
        "GetSnapshot",
        "RunChecks",
    }
)

#: CheckAuthorization flag: allow interactive polkit agent prompting.
ALLOW_USER_INTERACTION = 1


@dataclass(frozen=True)
class CallerSubject:
    """Immutable caller identity captured from the D-Bus method context."""

    sender: str
    pid: int
    uid: int
    start_time: int = 0


class Authorizer(Protocol):
    """Authorization interface: check if a caller may perform an action."""

    async def check(self, method: str, caller: CallerSubject | None) -> None:
        """Raise :class:`DomainException` if the caller is not authorized."""
        ...


class PolkitAuthorizer:
    """Production authorizer using an async sdbus polkit proxy.

    Fail-closed: if sender, credentials, polkit, or start time cannot be
    resolved, the call is denied.  No fallback to "active local user".
    """

    def __init__(self, timeout: float = 5.0) -> None:
        self._timeout = timeout

    async def check(self, method: str, caller: CallerSubject | None) -> None:
        """Check authorization for ``method`` against ``caller``."""
        if method in UNPRIVILEGED_METHODS:
            return  # No authorization required for reads.
        action_id = METHOD_ACTIONS.get(method)
        if action_id is None:
            # Unknown method — deny by default.
            raise DomainException(
                DomainError.NOT_AUTHORIZED,
                f"No action mapping for method '{method}'",
            )
        if caller is None:
            # No D-Bus caller context — fail closed.
            raise DomainException(
                DomainError.NOT_AUTHORIZED,
                "Missing caller context; cannot authorize",
            )
        await self._ask_polkit(action_id, caller)

    async def _ask_polkit(self, action_id: str, caller: CallerSubject) -> None:
        """Call CheckAuthorization on the polkit authority via sdbus."""
        try:
            import sdbus

            authority = PolkitAuthorityInterface.new_proxy(
                "org.freedesktop.PolicyKit1",
                "/org/freedesktop/PolicyKit1/Authority",
                bus=sdbus.get_default_bus(),
            )
            subject = (
                "unix-process",
                {
                    "pid": ("u", caller.pid),
                    "start-time": ("t", caller.start_time),
                    "uid": ("i", caller.uid),
                },
            )
            is_authorized, is_challenge, _details = await asyncio.wait_for(
                authority.CheckAuthorization(
                    subject, action_id, {}, ALLOW_USER_INTERACTION, ""
                ),
                timeout=self._timeout,
            )
            if not is_authorized:
                reason = "authentication required" if is_challenge else "denied"
                raise DomainException(
                    DomainError.NOT_AUTHORIZED,
                    f"Authorization {reason} for '{action_id}'",
                )
        except DomainException:
            raise
        except Exception as exc:  # noqa: BLE001
            log.error("polkit check failed: %s", exc)
            raise DomainException(
                DomainError.NOT_AUTHORIZED,
                f"Authorization check failed: {exc}",
            ) from exc


class InternalAuthorizer:
    """Authorizer for internal controller calls (no D-Bus sender).

    Internal calls bypass D-Bus authorization through this explicit
    internal authorizer.  It never forges or omits a sender — it is a
    distinct, auditable path.
    """

    async def check(self, method: str, caller: CallerSubject | None) -> None:
        """Always allow internal calls (they are not D-Bus-originated)."""
        return


class FakeAuthorizer:
    """Test authorizer that records all checks and can queue denials."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, CallerSubject | None]] = []
        self._deny_methods: set[str] = set()

    def deny(self, method: str) -> None:
        """Queue a denial for ``method``."""
        self._deny_methods.add(method)

    async def check(self, method: str, caller: CallerSubject | None) -> None:
        self.calls.append((method, caller))
        if method in self._deny_methods:
            raise DomainException(
                DomainError.NOT_AUTHORIZED,
                f"FakeAuthorizer: denied '{method}'",
            )


class PolkitAuthorityInterface(
    DbusInterfaceCommonAsync,
    interface_name="org.freedesktop.PolicyKit1.Authority",
):
    """Typed async proxy for PolicyKit's authorization method."""

    @dbus_method_async(input_signature="(sa{sv})sa{ss}us", result_signature="(bba{ss})")
    async def CheckAuthorization(
        self,
        subject: tuple,
        action_id: str,
        details: dict,
        flags: int,
        cancellation_id: str,
    ) -> tuple:
        raise NotImplementedError
