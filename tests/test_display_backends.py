"""Tests for the platform display backends (all driven by recorded fixtures)."""

from __future__ import annotations

import json
import pathlib

from honor_control.frontend.display_backends import (
    COLOR_ACCURACY,
    COLOR_BITS_AUTOMATIC,
    COLOR_EFFICIENCY,
    DisplayPreference,
    candidate_backends,
    detect_backend,
)
from honor_control.frontend.display_backends.gnome import (
    METHOD_PERSISTENT,
    METHOD_VERIFY,
    GnomeBackend,
)
from honor_control.frontend.display_backends.kde import KdeBackend
from honor_control.frontend.display_backends.types import (
    Mode,
    available_refresh_rates,
    pick_refresh_mode,
    selectable_color_bits,
)
from honor_control.frontend.display_backends.windows import (
    WindowsBackend,
    WinMode,
)
from honor_control.frontend.display_backends.wlroots import WlrootsBackend
from honor_control.frontend.display_backends.x11 import X11Backend, parse_xrandr

FIXTURES = pathlib.Path(__file__).parent / "fixtures" / "display"


class Runner:
    """Serves canned tool output and records every invocation."""

    def __init__(self, responses: dict[tuple[str, ...], tuple[int, str]]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, argv) -> tuple[int, str]:
        key = tuple(argv)
        self.calls.append(key)
        return self.responses.get(key, (0, ""))

    @property
    def writes(self) -> list[tuple[str, ...]]:
        read_only = {"-j", "-o", "--json", "--query"}
        return [c for c in self.calls if not (set(c[1:]) & read_only)]


# -- Shared helpers ------------------------------------------------------------


class TestModeHelpers:
    MODES = [
        Mode("a", 3120, 2080, 120.0, current=True),
        Mode("b", 3120, 2080, 59.97),
        Mode("c", 1920, 1200, 90.0),
    ]

    def test_rates_only_at_the_current_resolution(self) -> None:
        assert available_refresh_rates(self.MODES) == (60, 120)

    def test_never_approximates_with_another_resolution(self) -> None:
        assert pick_refresh_mode(self.MODES, 90) is None
        assert pick_refresh_mode(self.MODES, 60).id == "b"

    def test_color_bit_choices_follow_the_reported_range(self) -> None:
        assert selectable_color_bits(6, 12) == (6, 8, 10, 12)
        assert selectable_color_bits(8, 16) == (8, 10, 12, 14, 16)
        assert selectable_color_bits(8, 8) == (8,)


# -- KDE -----------------------------------------------------------------------


def _kde_json(current: str = "1") -> str:
    def mode(mode_id, rate, width=3120, height=2080):
        return {
            "id": mode_id,
            "refreshRate": rate,
            "size": {"width": width, "height": height},
        }

    return json.dumps(
        {
            "outputs": [
                {
                    "name": "HDMI-A-1",
                    "type": 6,
                    "connected": True,
                    "enabled": True,
                    "currentModeId": "1",
                    "modes": [mode("1", 60, 1920, 1080)],
                },
                {
                    "name": "eDP-1",
                    "type": 7,
                    "connected": True,
                    "enabled": True,
                    "currentModeId": current,
                    "modes": [
                        mode("1", 120),
                        mode("2", 59.97),
                        mode("9", 90, 1920, 1200),
                    ],
                },
            ]
        }
    )


class TestKde:
    def _backend(self, current="1", text=None) -> tuple[KdeBackend, Runner]:
        text = (
            text
            if text is not None
            else (FIXTURES / "kscreen-doctor-o.txt").read_text()
        )
        runner = Runner(
            {
                ("kscreen-doctor", "-j"): (0, _kde_json(current)),
                ("kscreen-doctor", "-o"): (0, text),
            }
        )
        return KdeBackend(runner), runner

    def test_capabilities_come_from_the_real_kscreen_output(self) -> None:
        backend, _runner = self._backend()
        caps = backend.query()
        assert caps.output == "eDP-1"
        assert caps.refresh_rates == (60, 120)
        assert caps.color_bits == (6, 8, 10, 12)  # from "range: [6; 12]"
        assert caps.color_bits_automatic is True
        assert caps.color_priority is True

    def test_output_without_color_controls_offers_neither(self) -> None:
        text = "Output: 1 eDP-1 uuid\n\tModes: 1:3120x2080@120.00*!\n"
        caps = self._backend(text=text)[0].query()
        assert caps.color_bits == ()
        assert caps.color_priority is False

    def test_unknown_color_resolution_is_unsupported(self) -> None:
        text = (
            "Output: 1 eDP-1 uuid\n\tColor power preference: prefer accuracy\n"
            "\tColor resolution: unknown\n"
        )
        caps = self._backend(text=text)[0].query()
        assert caps.color_bits == ()
        assert caps.color_priority is True

    def test_applies_all_three_options_to_the_panel_only(self) -> None:
        backend, runner = self._backend()
        result = backend.apply(DisplayPreference(60, 8, COLOR_EFFICIENCY))
        assert result.ok and result.changed
        assert runner.writes == [
            (
                "kscreen-doctor",
                "output.eDP-1.mode.2",
                "output.eDP-1.maxbpc.8",
                "output.eDP-1.colorPowerTradeoff.preferEfficiency",
            )
        ]

    def test_automatic_depth_uses_the_automatic_keyword(self) -> None:
        backend, runner = self._backend()
        backend.apply(DisplayPreference(color_bits=COLOR_BITS_AUTOMATIC))
        assert runner.writes == [("kscreen-doctor", "output.eDP-1.maxbpc.automatic")]

    def test_values_already_in_effect_are_not_set_again(self) -> None:
        # Fixture: 10 bits per color, prefer accuracy, current mode "1" (120 Hz).
        backend, runner = self._backend()
        result = backend.apply(DisplayPreference(120, 10, COLOR_ACCURACY))
        assert result.ok and not result.changed
        assert runner.writes == []

    def test_unoffered_values_are_skipped_not_approximated(self) -> None:
        backend, runner = self._backend()
        backend.apply(DisplayPreference(90, 16))  # 90 Hz: other resolution; 16 > 12
        assert runner.writes == []

    def test_failed_write_is_reported(self) -> None:
        backend, runner = self._backend()
        runner.responses[("kscreen-doctor", "output.eDP-1.mode.2")] = (9, "")
        assert backend.apply(DisplayPreference(60)).ok is False

    def test_availability_needs_a_kde_session_and_the_tool(self) -> None:
        def make(desktop, tool):
            return KdeBackend(
                env={"XDG_CURRENT_DESKTOP": desktop},
                which=lambda _name: "/usr/bin/kscreen-doctor" if tool else None,
            )

        assert make("KDE", True).is_available()
        assert not make("GNOME", True).is_available()
        assert not make("KDE", False).is_available()


# -- wlroots -------------------------------------------------------------------

WLR_JSON = json.dumps(
    [
        {
            "name": "HDMI-A-1",
            "enabled": True,
            "modes": [
                {"width": 1920, "height": 1080, "refresh": 60.0, "current": True}
            ],
        },
        {
            "name": "eDP-1",
            "enabled": True,
            "modes": [
                {"width": 2880, "height": 1800, "refresh": 120.0, "current": True},
                {"width": 2880, "height": 1800, "refresh": 60.001},
                {"width": 1920, "height": 1200, "refresh": 90.0},
            ],
        },
    ]
)


class TestWlroots:
    def test_reads_the_internal_panel_and_switches_its_mode(self) -> None:
        runner = Runner({("wlr-randr", "--json"): (0, WLR_JSON)})
        backend = WlrootsBackend(runner)
        caps = backend.query()
        assert (caps.output, caps.refresh_rates) == ("eDP-1", (60, 120))
        assert not caps.supports_color_bits and not caps.color_priority
        assert backend.apply(DisplayPreference(60)).changed
        assert runner.writes == [
            ("wlr-randr", "--output", "eDP-1", "--mode", "2880x1800@60.001Hz")
        ]

    def test_current_rate_is_a_no_op(self) -> None:
        runner = Runner({("wlr-randr", "--json"): (0, WLR_JSON)})
        assert not WlrootsBackend(runner).apply(DisplayPreference(120)).changed
        assert runner.writes == []

    def test_old_wlr_randr_without_json_is_unavailable(self) -> None:
        runner = Runner({("wlr-randr", "--json"): (1, "unknown option")})
        assert WlrootsBackend(runner).query() is None

    def test_needs_a_wayland_session_and_the_tool(self) -> None:
        backend = WlrootsBackend(
            env={"WAYLAND_DISPLAY": "wayland-1"}, which=lambda _n: "/bin/wlr-randr"
        )
        assert backend.is_available()
        assert not WlrootsBackend(env={}, which=lambda _n: "/bin/x").is_available()


# -- X11 -----------------------------------------------------------------------


class TestX11:
    def test_parses_a_real_xwayland_listing(self) -> None:
        outputs = parse_xrandr((FIXTURES / "xrandr-xwayland.txt").read_text())
        panel = next(o for o in outputs if o.name == "eDP-1")
        assert panel.current_bits is None  # no max bpc property there
        assert next(m for m in panel.modes if m.current).width == 3120

    def _native(self) -> tuple[X11Backend, Runner]:
        text = (FIXTURES / "xrandr-x11-native.txt").read_text()
        runner = Runner({("xrandr", "--query", "--props"): (0, text)})
        return X11Backend(runner), runner

    def test_capabilities_skip_interlaced_modes_and_external_outputs(self) -> None:
        caps = self._native()[0].query()
        assert caps.output == "eDP-1"
        assert caps.refresh_rates == (48, 60, 144)
        assert caps.color_bits == (6, 8, 10, 12)
        assert caps.color_bits_automatic is False

    def test_rate_and_depth_go_out_in_one_command(self) -> None:
        backend, runner = self._native()
        assert backend.apply(DisplayPreference(60, 10)).changed
        assert runner.writes == [
            (
                "xrandr",
                "--output",
                "eDP-1",
                "--mode",
                "1920x1080",
                "--rate",
                "60.00",
                "--set",
                "max bpc",
                "10",
            )
        ]

    def test_automatic_depth_is_not_offered_on_x11(self) -> None:
        backend, runner = self._native()
        backend.apply(DisplayPreference(color_bits=COLOR_BITS_AUTOMATIC))
        assert runner.writes == []

    def test_not_offered_on_a_wayland_session(self) -> None:
        backend = X11Backend(
            env={"DISPLAY": ":0", "XDG_SESSION_TYPE": "wayland"},
            which=lambda _n: "/usr/bin/xrandr",
        )
        assert not backend.is_available()
        backend = X11Backend(
            env={"DISPLAY": ":0", "XDG_SESSION_TYPE": "x11"},
            which=lambda _n: "/usr/bin/xrandr",
        )
        assert backend.is_available()


# -- GNOME ---------------------------------------------------------------------


def _gnome_state(current: str = "2880x1800@120.000"):
    def mode(mode_id, width, height, rate):
        return (
            mode_id,
            width,
            height,
            rate,
            1.0,
            [1.0],
            {"is-current": ("b", True)} if mode_id == current else {},
        )

    panel_modes = [
        mode("2880x1800@120.000", 2880, 1800, 120.0),
        mode("2880x1800@59.999", 2880, 1800, 59.999),
        mode("1920x1200@90.000", 1920, 1200, 90.0),
    ]
    external_modes = [mode("1920x1080@60.000", 1920, 1080, 60.0)]
    external_modes[0][6]["is-current"] = ("b", True)
    monitors = [
        (
            ("eDP-1", "BOE", "0x0", "0"),
            panel_modes,
            {"is-builtin": ("b", True), "color-mode": ("u", 0)},
        ),
        (("HDMI-1", "DEL", "X", "1"), external_modes, {}),
    ]
    logical = [
        (0, 0, 2.0, 0, True, [("eDP-1", "BOE", "0x0", "0")], {}),
        (1440, 0, 1.0, 0, False, [("HDMI-1", "DEL", "X", "1")], {}),
    ]
    return (7, monitors, logical, {"layout-mode": ("u", 1)})


class FakeMutter:
    def __init__(self, state, fail_verify: bool = False) -> None:
        self.state = state
        self.fail_verify = fail_verify
        self.applied: list[tuple] = []

    def get_current_state(self):
        return self.state

    def apply_monitors_config(self, serial, method, logical, properties):
        if method == METHOD_VERIFY and self.fail_verify:
            raise RuntimeError("rejected")
        self.applied.append((serial, method, logical, properties))


class TestGnome:
    def test_reads_only_the_builtin_monitor(self) -> None:
        caps = GnomeBackend(FakeMutter(_gnome_state())).query()
        assert caps.output == "eDP-1"
        assert caps.refresh_rates == (60, 120)
        assert not caps.supports_color_bits

    def test_resubmits_the_whole_layout_with_one_changed_mode(self) -> None:
        mutter = FakeMutter(_gnome_state())
        result = GnomeBackend(mutter).apply(DisplayPreference(60))
        assert result.ok and result.changed
        # Verified first, then persisted, with the same payload.
        assert [call[1] for call in mutter.applied] == [
            METHOD_VERIFY,
            METHOD_PERSISTENT,
        ]
        serial, _method, logical, properties = mutter.applied[-1]
        assert serial == 7
        assert properties == {"layout-mode": ("u", 1)}
        assert logical == [
            (
                0,
                0,
                2.0,
                0,
                True,
                [("eDP-1", "2880x1800@59.999", {"color-mode": ("u", 0)})],
            ),
            (1440, 0, 1.0, 0, False, [("HDMI-1", "1920x1080@60.000", {})]),
        ]

    def test_rejected_verification_never_persists(self) -> None:
        mutter = FakeMutter(_gnome_state(), fail_verify=True)
        result = GnomeBackend(mutter).apply(DisplayPreference(60))
        assert not result.ok
        assert mutter.applied == []

    def test_current_rate_and_unoffered_rate_are_no_ops(self) -> None:
        mutter = FakeMutter(_gnome_state())
        backend = GnomeBackend(mutter)
        assert not backend.apply(DisplayPreference(120)).changed
        assert not backend.apply(DisplayPreference(90)).changed
        assert mutter.applied == []

    def test_unreachable_service_means_no_capabilities(self) -> None:
        class Down:
            def get_current_state(self):
                raise OSError("no bus")

        assert GnomeBackend(Down()).query() is None


# -- Windows -------------------------------------------------------------------


class FakeWin:
    def __init__(self, device="\\\\.\\DISPLAY1", accept=True) -> None:
        self.device = device
        self.accept = accept
        self.set_calls: list[WinMode] = []
        self._modes = [
            WinMode(2880, 1800, 32, 120, current=True),
            WinMode(2880, 1800, 32, 60),
            WinMode(2880, 1800, 32, 48),
            WinMode(1920, 1200, 32, 90),
            WinMode(2880, 1800, 16, 100),
        ]

    def internal_device(self):
        return self.device

    def modes(self, device):
        return list(self._modes)

    def set_frequency(self, device, mode):
        self.set_calls.append(mode)
        return self.accept


class TestWindows:
    def test_rates_are_limited_to_the_current_size_and_bit_depth(self) -> None:
        caps = WindowsBackend(FakeWin()).query()
        assert caps.refresh_rates == (48, 60, 120)
        assert not caps.supports_color_bits and not caps.color_priority

    def test_switches_only_the_refresh_rate(self) -> None:
        api = FakeWin()
        assert WindowsBackend(api).apply(DisplayPreference(60)).changed
        assert api.set_calls == [WinMode(2880, 1800, 32, 60)]

    def test_current_and_unoffered_rates_are_no_ops(self) -> None:
        api = FakeWin()
        backend = WindowsBackend(api)
        assert not backend.apply(DisplayPreference(120)).changed
        assert not backend.apply(DisplayPreference(90)).changed  # other resolution
        assert not backend.apply(DisplayPreference(100)).changed  # other bit depth
        assert api.set_calls == []

    def test_rejected_mode_is_reported(self) -> None:
        assert not WindowsBackend(FakeWin(accept=False)).apply(DisplayPreference(60)).ok

    def test_no_display_means_no_capabilities(self) -> None:
        assert WindowsBackend(FakeWin(device=None)).query() is None


# -- Detection -----------------------------------------------------------------


class TestDetection:
    @staticmethod
    def _pick(**kwargs):
        backend = detect_backend(**kwargs)
        return type(backend).__name__ if backend else None

    @staticmethod
    def _tools(*names):
        return lambda name: f"/usr/bin/{name}" if name in names else None

    def test_kde(self) -> None:
        assert (
            self._pick(
                platform="linux",
                env={"XDG_CURRENT_DESKTOP": "KDE", "WAYLAND_DISPLAY": "w"},
                which=self._tools("kscreen-doctor", "xrandr"),
            )
            == "KdeBackend"
        )

    def test_gnome_on_wayland_uses_mutter_not_xrandr(self) -> None:
        assert (
            self._pick(
                platform="linux",
                env={
                    "XDG_CURRENT_DESKTOP": "ubuntu:GNOME",
                    "XDG_SESSION_TYPE": "wayland",
                    "WAYLAND_DISPLAY": "w",
                    "DISPLAY": ":0",
                },
                which=self._tools("xrandr"),
            )
            == "GnomeBackend"
        )

    def test_sway_uses_wlr_randr(self) -> None:
        assert (
            self._pick(
                platform="linux",
                env={"XDG_CURRENT_DESKTOP": "sway", "WAYLAND_DISPLAY": "w"},
                which=self._tools("wlr-randr"),
            )
            == "WlrootsBackend"
        )

    def test_xfce_on_x11_uses_xrandr(self) -> None:
        assert (
            self._pick(
                platform="linux",
                env={
                    "XDG_CURRENT_DESKTOP": "XFCE",
                    "DISPLAY": ":0",
                    "XDG_SESSION_TYPE": "x11",
                },
                which=self._tools("xrandr"),
            )
            == "X11Backend"
        )

    def test_wayland_without_a_known_tool_has_no_backend(self) -> None:
        assert (
            self._pick(
                platform="linux",
                env={
                    "XDG_CURRENT_DESKTOP": "weston",
                    "WAYLAND_DISPLAY": "w",
                    "DISPLAY": ":0",
                    "XDG_SESSION_TYPE": "wayland",
                },
                which=self._tools("xrandr"),
            )
            is None
        )

    def test_windows_and_unsupported_platforms(self) -> None:
        assert [type(b).__name__ for b in candidate_backends(platform="win32")] == [
            "WindowsBackend"
        ]
        assert candidate_backends(platform="darwin") == []


class TestWindowsStructures:
    """Layouts must equal the documented Win32 sizes (checked on any host)."""

    def test_sizes_match_the_windows_sdk(self) -> None:
        import ctypes

        from honor_control.frontend.display_backends.windows import win_structs

        structs = win_structs()
        sizes = {
            name: ctypes.sizeof(getattr(structs, name))
            for name in (
                "DEVMODEW",
                "DISPLAY_DEVICEW",
                "LUID",
                "PATH",
                "MODE",
                "HEADER",
                "SOURCE_NAME",
                "SYSTEM_POWER_STATUS",
            )
        }
        assert sizes == {
            "DEVMODEW": 220,
            "DISPLAY_DEVICEW": 840,
            "LUID": 8,
            "PATH": 72,  # DISPLAYCONFIG_PATH_INFO
            "MODE": 64,  # DISPLAYCONFIG_MODE_INFO
            "HEADER": 20,  # DISPLAYCONFIG_DEVICE_INFO_HEADER
            "SOURCE_NAME": 84,  # DISPLAYCONFIG_SOURCE_DEVICE_NAME
            "SYSTEM_POWER_STATUS": 12,
        }

    def test_field_offsets_that_the_code_relies_on(self) -> None:
        from honor_control.frontend.display_backends.windows import win_structs

        structs = win_structs()
        assert structs.DEVMODEW.dmFields.offset == 72
        assert structs.DEVMODEW.dmBitsPerPel.offset == 168
        assert structs.DEVMODEW.dmPelsWidth.offset == 172
        assert structs.DEVMODEW.dmDisplayFrequency.offset == 184
        assert structs.TARGET.outputTechnology.offset == 16

    def test_non_windows_hosts_have_no_ac_reading(self) -> None:
        import sys

        from honor_control.frontend.display_backends.windows import system_ac_online

        if sys.platform != "win32":
            assert system_ac_online() is None
