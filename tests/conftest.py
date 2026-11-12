"""Shared test isolation for process-wide hardware safety markers."""

from __future__ import annotations

import pathlib

import pytest


@pytest.fixture(autouse=True)
def isolate_touchpad_safety_files(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keep the advisory writer lock isolated for every test."""
    monkeypatch.setenv(
        "HONOR_CONTROL_TOUCHPAD_LOCK",
        str(tmp_path / "touchpad.lock"),
    )
    # GUI/tray tests must never reconfigure the developer's real displays.
    monkeypatch.setenv("HONOR_CONTROL_DISABLE_DISPLAY_POLICY", "1")
