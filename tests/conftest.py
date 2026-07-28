"""Shared test isolation for process-wide hardware safety markers."""

from __future__ import annotations

import pathlib

import pytest


@pytest.fixture(autouse=True)
def isolate_touchpad_safety_files(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("HONOR_TOUCHPAD_FIRMWARE_QUALIFIED", raising=False)
    monkeypatch.setenv(
        "HONOR_CONTROL_TOUCHPAD_QUALIFICATION_FILE",
        str(tmp_path / "touchpad-firmware-qualified"),
    )
    monkeypatch.setenv(
        "HONOR_CONTROL_TOUCHPAD_LOCK",
        str(tmp_path / "touchpad.lock"),
    )
