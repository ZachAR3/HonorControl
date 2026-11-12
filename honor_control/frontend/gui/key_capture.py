"""Record a keyboard shortcut as a Linux key-name chord (``leftmeta,v``).

The desktop gesture dispatcher emits key chords by Linux key name, the same
names the text field accepts.  ``ShortcutRecorder`` holds the pure chord logic
(testable without a display) and ``RecordButton`` wires it to Qt key events.

Recording only sees keys the compositor delivers to the window: shortcuts the
desktop reserves for itself (some Meta combinations) never arrive, so typing
the chord by hand always remains available.
"""

from __future__ import annotations

from PySide6.QtCore import QEvent, Qt, Signal
from PySide6.QtGui import QKeyEvent
from PySide6.QtWidgets import QPushButton

from honor_control.core.gestures import KEY_CODES

MODIFIERS = frozenset(
    {
        "leftctrl",
        "rightctrl",
        "leftshift",
        "rightshift",
        "leftalt",
        "rightalt",
        "leftmeta",
        "rightmeta",
    }
)

#: evdev code -> key name (first name wins when several share a code).
_NAME_FOR_CODE: dict[int, str] = {}
for _name, _code in KEY_CODES.items():
    _NAME_FOR_CODE.setdefault(_code, _name)

#: X11/Wayland (xkb) keycodes are the evdev code plus this offset.
XKB_OFFSET = 8

_QT_FALLBACK: dict[int, str] = {
    int(Qt.Key.Key_Control): "leftctrl",
    int(Qt.Key.Key_Shift): "leftshift",
    int(Qt.Key.Key_Alt): "leftalt",
    int(Qt.Key.Key_Meta): "leftmeta",
    int(Qt.Key.Key_Super_L): "leftmeta",
    int(Qt.Key.Key_Super_R): "rightmeta",
    int(Qt.Key.Key_AltGr): "rightalt",
    int(Qt.Key.Key_Escape): "esc",
    int(Qt.Key.Key_Return): "enter",
    int(Qt.Key.Key_Enter): "enter",
    int(Qt.Key.Key_Tab): "tab",
    int(Qt.Key.Key_Backspace): "backspace",
    int(Qt.Key.Key_Space): "space",
    int(Qt.Key.Key_Print): "print",
    int(Qt.Key.Key_PageUp): "pageup",
    int(Qt.Key.Key_PageDown): "pagedown",
    int(Qt.Key.Key_Home): "home",
    int(Qt.Key.Key_End): "end",
    int(Qt.Key.Key_Left): "left",
    int(Qt.Key.Key_Right): "right",
    int(Qt.Key.Key_Up): "up",
    int(Qt.Key.Key_Down): "down",
    int(Qt.Key.Key_Delete): "delete",
    int(Qt.Key.Key_Insert): "insert",
}


def key_name(scan_code: int, qt_key: int) -> str | None:
    """Return the Linux key name for a key event, or ``None`` if unknown.

    ``scan_code`` is Qt's native scan code (xkb keycode, evdev + 8).  It is
    exact and distinguishes left from right modifiers; the Qt key is only a
    fallback for platforms that do not report one.
    """
    name = _NAME_FOR_CODE.get(scan_code - XKB_OFFSET) if scan_code > 0 else None
    if name is not None:
        return name
    if qt_key in _QT_FALLBACK:
        return _QT_FALLBACK[qt_key]
    if int(Qt.Key.Key_A) <= qt_key <= int(Qt.Key.Key_Z):
        return chr(qt_key).lower()
    if int(Qt.Key.Key_0) <= qt_key <= int(Qt.Key.Key_9):
        return chr(qt_key)
    if int(Qt.Key.Key_F1) <= qt_key <= int(Qt.Key.Key_F12):
        return f"f{qt_key - int(Qt.Key.Key_F1) + 1}"
    return None


class ShortcutRecorder:
    """Accumulate key presses into a chord: modifiers first, one final key.

    ``press`` / ``release`` return the finished chord string, ``CANCELLED`` or
    ``None`` while recording continues.
    """

    CANCELLED = ""

    def __init__(self) -> None:
        self._order: list[str] = []
        self._held: set[str] = set()

    def reset(self) -> None:
        self._order.clear()
        self._held.clear()

    def press(self, name: str) -> str | None:
        if name == "esc" and not self._order:
            return self.CANCELLED
        if name not in self._order:
            self._order.append(name)
        self._held.add(name)
        if name not in MODIFIERS:
            return self._finish()
        return None

    def release(self, name: str) -> str | None:
        self._held.discard(name)
        if self._order and not self._held:
            # Only modifiers were pressed: a modifier-only chord is wanted.
            return self._finish()
        return None

    def _finish(self) -> str:
        chord = ",".join(self._order)
        self.reset()
        return chord


class RecordButton(QPushButton):
    """A button that captures the next key chord the user presses."""

    recorded = Signal(str)

    IDLE_TEXT = "Record"
    ACTIVE_TEXT = "Press keys…"

    def __init__(self, parent=None) -> None:
        super().__init__(self.IDLE_TEXT, parent)
        self._recorder = ShortcutRecorder()
        self._recording = False
        self.setToolTip(
            "Click, then press the key combination to use. Esc cancels. "
            "Shortcuts the desktop reserves for itself may not be seen; "
            "type those by hand."
        )
        self.clicked.connect(self._toggle)

    @property
    def recording(self) -> bool:
        return self._recording

    def _toggle(self) -> None:
        if self._recording:
            self.stop()
        else:
            self.start()

    def start(self) -> None:
        self._recorder.reset()
        self._recording = True
        self.setText(self.ACTIVE_TEXT)
        self.grabKeyboard()

    def stop(self) -> None:
        if self._recording:
            self.releaseKeyboard()
        self._recording = False
        self._recorder.reset()
        self.setText(self.IDLE_TEXT)

    def handle_key(self, name: str | None, *, pressed: bool) -> None:
        """Feed one key event (separate from Qt so it can be tested)."""
        if not self._recording or name is None:
            return
        result = self._recorder.press(name) if pressed else self._recorder.release(name)
        if result is None:
            return
        self.stop()
        if result != ShortcutRecorder.CANCELLED:
            self.recorded.emit(result)

    def event(self, event: QEvent) -> bool:  # noqa: N802 - Qt override
        if self._recording and event.type() in (
            QEvent.Type.KeyPress,
            QEvent.Type.KeyRelease,
        ):
            if isinstance(event, QKeyEvent) and not event.isAutoRepeat():
                self.handle_key(
                    key_name(event.nativeScanCode(), event.key()),
                    pressed=event.type() == QEvent.Type.KeyPress,
                )
            event.accept()
            return True  # swallow Tab/Space/Enter while recording
        return super().event(event)

    def focusOutEvent(self, event) -> None:  # noqa: N802
        self.stop()
        super().focusOutEvent(event)

    def hideEvent(self, event) -> None:  # noqa: N802
        self.stop()
        super().hideEvent(event)
