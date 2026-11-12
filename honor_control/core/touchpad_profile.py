"""Validation and loading for the typed touchpad TOML profile.

The profile format is shared by the D-Bus CLI and the direct diagnostic CLI.
Keeping its parser in ``core`` avoids importing the Linux hidraw transport when
the unprivileged ``honorctl touchpad apply`` command only needs to read a
profile before sending it over D-Bus.
"""

from __future__ import annotations

import pathlib
import tomllib

from honor_control.core.touchpad import (
    TouchpadSetting,
    parse_touchpad_setting,
    parse_touchpad_value,
)


def load_touchpad_profile(
    path: pathlib.Path,
) -> tuple[dict[TouchpadSetting, int], bool | None]:
    """Load and fully validate the narrow touchpad TOML schema.

    The optional ``[master]`` table is retained here because the standalone
    diagnostic CLI can apply it through the WMI helper.  The D-Bus CLI rejects
    profiles containing that table before making a service call.
    """
    with path.open("rb") as stream:
        document = tomllib.load(stream)
    unknown_tables = sorted(set(document) - {"settings", "master"})
    if unknown_tables:
        raise ValueError(f"unknown profile table(s): {', '.join(unknown_tables)}")

    raw_settings = document.get("settings", {})
    if not isinstance(raw_settings, dict):
        raise ValueError("[settings] must be a TOML table")
    settings: dict[TouchpadSetting, int] = {}
    for name, raw_value in raw_settings.items():
        canonical = parse_touchpad_setting(name)
        if canonical in settings:
            raise ValueError(f"duplicate touchpad setting {canonical.value!r}")
        if not isinstance(raw_value, (str, int, bool)):
            raise ValueError(f"{name} must be a string, integer, or boolean")
        settings[canonical] = parse_touchpad_value(canonical, raw_value)

    master: bool | None = None
    if "master" in document:
        raw_master = document["master"]
        if not isinstance(raw_master, dict):
            raise ValueError("[master] must be a TOML table")
        unknown_master = sorted(set(raw_master) - {"enabled"})
        if unknown_master:
            raise ValueError(f"unknown [master] key(s): {', '.join(unknown_master)}")
        if "enabled" not in raw_master or not isinstance(raw_master["enabled"], bool):
            raise ValueError("[master].enabled must be true or false")
        master = raw_master["enabled"]

    if not settings and master is None:
        raise ValueError("profile contains neither [settings] values nor [master]")
    return settings, master

