"""Client-only declaration of the public Honor Control D-Bus contract."""

from __future__ import annotations

from sdbus import DbusInterfaceCommonAsync, dbus_method_async, dbus_signal_async

from honor_control.contract import IFACE_ROOT


class ControlProxy(DbusInterfaceCommonAsync, interface_name=IFACE_ROOT):
    @dbus_method_async(result_signature="u")
    async def GetApiVersion(self) -> int: ...

    @dbus_method_async(result_signature="u")
    async def GetSchemaVersion(self) -> int: ...

    @dbus_method_async(result_signature="a{sv}")
    async def GetSnapshot(self) -> dict: ...

    @dbus_method_async(result_signature="a{sv}")
    async def RunChecks(self) -> dict: ...

    @dbus_method_async(input_signature="ii", result_signature="a{sv}")
    async def SetThresholds(self, end: int, start: int) -> dict: ...

    @dbus_method_async(input_signature="s", result_signature="a{sv}")
    async def SetMode(self, mode: str) -> dict: ...

    @dbus_method_async(input_signature="s", result_signature="a{sv}")
    async def SetProfile(self, profile: str) -> dict: ...

    @dbus_method_async(input_signature="b", result_signature="a{sv}")
    async def SetAutoSwitch(self, enabled: bool) -> dict: ...

    @dbus_method_async(input_signature="sssiisssbi", result_signature="a{sv}")
    async def SavePowerProfile(
        self,
        name: str,
        label: str,
        description: str,
        pl1_uw: int,
        pl2_uw: int,
        governor: str,
        epp: str,
        ppd_profile: str,
        turbo_enabled: bool,
        max_perf_pct: int,
    ) -> dict: ...

    @dbus_method_async(input_signature="s", result_signature="a{sv}")
    async def DeletePowerProfile(self, name: str) -> dict: ...

    @dbus_method_async(input_signature="bssss", result_signature="a{sv}")
    async def ConfigureAutoSwitch(
        self,
        enabled: bool,
        on_ac: str,
        on_battery: str,
        on_ac_script: str,
        on_battery_script: str,
    ) -> dict: ...

    @dbus_method_async(result_signature="a{sv}")
    async def SetStockAuto(self) -> dict: ...

    @dbus_method_async(input_signature="sa(ii)", result_signature="a{sv}")
    async def SetCurve(self, profile: str, points: list) -> dict: ...

    @dbus_method_async(input_signature="iu", result_signature="a{sv}")
    async def SetManual(self, speed: int, ttl_seconds: int) -> dict: ...

    @dbus_method_async(input_signature="ss", result_signature="a{sv}")
    async def SetMapping(self, gesture_id: str, combo: str) -> dict: ...

    @dbus_method_async(input_signature="sb", result_signature="a{sv}")
    async def SetEnabled(self, gesture_id: str, enabled: bool) -> dict: ...

    @dbus_method_async(input_signature="b", result_signature="a{sv}")
    async def SetAllEnabled(self, enabled: bool) -> dict: ...

    @dbus_method_async(input_signature="b", result_signature="a{sv}")
    async def SetDaemonEnabled(self, enabled: bool) -> dict: ...

    @dbus_method_async(input_signature="a{si}", result_signature="a{sv}")
    async def ApplyTouchpadSettings(self, settings: dict) -> dict: ...

    @dbus_method_async(input_signature="si", result_signature="a{sv}")
    async def SetTouchpadSetting(self, setting: str, value: int) -> dict: ...

    @dbus_method_async(result_signature="a{sv}")
    async def QueryTouchpadSupport(self) -> dict: ...

    @dbus_method_async(result_signature="a{sv}")
    async def ProbeTouchpadFirmware(self) -> dict: ...

    @dbus_method_async(input_signature="b", result_signature="a{sv}")
    async def SetMitigationEnabled(self, enabled: bool) -> dict: ...

    @dbus_method_async(result_signature="a{sv}")
    async def Reload(self) -> dict: ...

    @dbus_method_async(result_signature="a{sv}")
    async def GetDebugBundle(self) -> dict: ...

    @dbus_method_async(input_signature="u", result_signature="as")
    async def GetRecentLogs(self, lines: int) -> list: ...

    @dbus_signal_async(signal_signature="tas")
    def StateChanged(self): ...
