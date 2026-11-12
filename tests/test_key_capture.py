"""Shortcut recording: chord logic and key-name translation."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402
from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from honor_control.core.gestures import KEY_CODES  # noqa: E402
from honor_control.core.validation import validate_key_combo  # noqa: E402
from honor_control.frontend.gui.key_capture import (  # noqa: E402
    XKB_OFFSET,
    RecordButton,
    ShortcutRecorder,
    key_name,
)


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def test_scan_code_maps_to_exact_linux_name() -> None:
    assert key_name(KEY_CODES["pagedown"] + XKB_OFFSET, 0) == "pagedown"
    # Left and right modifiers are distinguished by the scan code.
    assert key_name(KEY_CODES["rightmeta"] + XKB_OFFSET, int(Qt.Key.Key_Meta)) == (
        "rightmeta"
    )


def test_qt_key_is_the_fallback_without_a_scan_code() -> None:
    assert key_name(0, int(Qt.Key.Key_Meta)) == "leftmeta"
    assert key_name(0, int(Qt.Key.Key_V)) == "v"
    assert key_name(0, int(Qt.Key.Key_5)) == "5"
    assert key_name(0, int(Qt.Key.Key_F4)) == "f4"
    assert key_name(0, int(Qt.Key.Key_PageDown)) == "pagedown"
    assert key_name(0, 0x7FFFFFF) is None


def test_every_recordable_name_is_valid_for_the_service() -> None:
    for name in KEY_CODES:
        assert validate_key_combo(name) == [name]


def test_chord_is_modifiers_then_the_final_key() -> None:
    rec = ShortcutRecorder()
    assert rec.press("leftmeta") is None
    assert rec.press("pagedown") == "leftmeta,pagedown"


def test_single_key_and_modifier_only_chords() -> None:
    rec = ShortcutRecorder()
    assert rec.press("print") == "print"
    assert rec.press("leftctrl") is None
    assert rec.press("leftalt") is None
    assert rec.release("leftalt") is None
    assert rec.release("leftctrl") == "leftctrl,leftalt"


def test_escape_cancels_only_when_nothing_else_is_held() -> None:
    rec = ShortcutRecorder()
    assert rec.press("esc") == ShortcutRecorder.CANCELLED
    assert rec.press("leftctrl") is None
    assert rec.press("esc") == "leftctrl,esc"


def test_record_button_emits_and_stops(qapp) -> None:
    button = RecordButton()
    got: list[str] = []
    button.recorded.connect(got.append)
    button.handle_key("v", pressed=True)  # ignored: not recording
    assert got == []
    button.start()
    assert button.text() == RecordButton.ACTIVE_TEXT
    button.handle_key("leftmeta", pressed=True)
    button.handle_key("v", pressed=True)
    assert got == ["leftmeta,v"]
    assert not button.recording
    assert button.text() == RecordButton.IDLE_TEXT


def test_record_button_escape_cancels_without_emitting(qapp) -> None:
    button = RecordButton()
    got: list[str] = []
    button.recorded.connect(got.append)
    button.start()
    button.handle_key("esc", pressed=True)
    assert got == [] and not button.recording
