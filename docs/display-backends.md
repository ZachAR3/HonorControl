# Display preferences per power source

The Power page's **Display on AC / battery** card lets you pick display
options for each power source. Choices are staged and saved with the window's
shared **Apply changes** bar. They are applied by the session frontend (the
tray, alone or inside the window) when the laptop moves between AC and
battery (and straight away for the power source you are on), never by the root
service, and never change the resolution.

The card only shows options the built-in panel and the platform backend
support, and lists the panel's own values (for example 60/120 Hz on one laptop
and 48/60/144 Hz on another). Preferences are stored per user (`QSettings`,
organisation `HonorLinux`, application `Honor Control`). Set
`HONOR_CONTROL_DISABLE_DISPLAY_POLICY=1` to turn the feature off entirely.

## Backends

The backend is chosen from the running session, in this order:

| Backend | Used when | Refresh rate | Colour depth | Colour accuracy |
|---|---|---|---|---|
| KDE Plasma | `XDG_CURRENT_DESKTOP` contains `KDE` and `kscreen-doctor` is installed (Wayland or X11) | yes | yes (panel's range, or automatic) | yes |
| GNOME / Mutter | GNOME-family desktop (GNOME, Budgie, Pantheon, ...) and `python-sdbus` | yes | no | no |
| wlroots | `WAYLAND_DISPLAY` is set and `wlr-randr` (0.4+, for `--json`) is installed: Sway, Hyprland, river, labwc, ... | yes | no | no |
| X11 | An X11 session (`DISPLAY` set, not a Wayland session) with `xrandr` | yes | yes, through the `max bpc` connector property when the driver exposes it | no |
| Windows | Windows (`ctypes`, no extra packages) | yes | no | no |

Notes:

- Only the **built-in panel** is touched (connector `eDP`/`LVDS`/`DSI`, GNOME's
  `is-builtin`, or Windows' embedded output technology). External monitors are
  left alone. If no built-in panel is found, nothing is offered.
- A saved value the panel does not offer at its current resolution (for
  example after moving the settings to another laptop) is kept, labelled
  "not offered here", and skipped when applying rather than approximated.
- Colour depth means the maximum bits per colour channel. Higher depth can
  cost display bandwidth and power; "Automatic" lets the compositor choose
  where it supports that.
- On Windows there is no service yet, so the AC state is read from
  `GetSystemPowerStatus` every 30 s.
- On a Wayland session without a supported tool (for example plain Weston, or
  Hyprland without `wlr-randr`) no backend is available and the card says so.

## Verification status

The KDE backend has been exercised against a live KDE Plasma 6 session
(refresh rate, colour depth and colour accuracy all switched and restored).
The GNOME, wlroots, X11 and Windows backends are covered by unit tests built
on recorded or documented data and fake system interfaces; they have not yet
been run on those desktops.
