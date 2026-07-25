"""Small reusable Qt widgets shared across the GUI pages.

Keeping these in one place keeps page code declarative: cards, status
dots, toggle switches, section labels, info rows and a collapsible
"Advanced" group. None of them touch D-Bus — they're pure presentation.
"""

from __future__ import annotations

import os

from PySide6.QtCore import (
    Property,
    QEasingCurve,
    QPoint,
    QPropertyAnimation,
    QRect,
    Qt,
    Signal,
)
from PySide6.QtGui import QColor, QKeyEvent, QMouseEvent, QPainter, QPaintEvent, QPen
from PySide6.QtWidgets import (
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)


class Card(QFrame):
    """A flat, ruled content section with a title and vertical body layout.

    Use :attr:`layout` (a :class:`QVBoxLayout`) to add child widgets; the
    built-in title label renders as a bold heading.
    """

    def __init__(self, title: str = "", parent: QWidget | None = None) -> None:
        """Create the card with an optional title."""
        super().__init__(parent)
        self.setObjectName("HonorCard")
        self.setFrameShape(QFrame.Shape.NoFrame)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 12, 0, 16)
        outer.setSpacing(8)
        self.title_label = QLabel(title)
        self.title_label.setObjectName("HonorCardTitle")
        self.title_label.setVisible(bool(title))
        outer.addWidget(self.title_label)
        self._body = QVBoxLayout()
        self._body.setSpacing(6)
        outer.addLayout(self._body)

    @property
    def layout(self) -> QVBoxLayout:  # type: ignore[override]
        """Return the body layout callers add widgets to."""
        return self._body


class StatusDot(QWidget):
    """A circular status indicator (green / red / amber / grey)."""

    GREEN = "#63B239"
    RED = "#B74336"
    AMBER = "#D8A52B"
    GREY = "#7D7B74"
    _NAMES = {
        GREEN: "healthy",
        RED: "error",
        AMBER: "warning",
        GREY: "unknown",
    }

    def __init__(self, color: str = GREY, parent: QWidget | None = None) -> None:
        """Create the dot with an initial colour."""
        super().__init__(parent)
        self._color = color
        self.setFixedSize(12, 12)
        self.setAccessibleName(f"Status: {self._NAMES.get(color, 'unknown')}")

    def set_color(self, color: str) -> None:
        """Change the dot colour and repaint."""
        self._color = color
        self.setAccessibleName(f"Status: {self._NAMES.get(color, 'unknown')}")
        self.update()

    def paintEvent(self, event: QPaintEvent) -> None:  # noqa: N802 - Qt API
        """Draw a filled circle with a thin contrasting ring."""
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRect(2, 2, 8, 8)
        painter.setBrush(QColor(self._color))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawEllipse(rect)


class ToggleSwitch(QWidget):
    """A pill-shaped ON/OFF switch emitting :attr:`toggled`.

    Mouse-clickable; the knob animates between the two sides.
    """

    toggled = Signal(bool)

    def __init__(self, checked: bool = False, parent: QWidget | None = None) -> None:
        """Create the switch in the given initial state."""
        super().__init__(parent)
        # Initialise BEFORE creating the QPropertyAnimation so the property
        # getter (which PySide may call during construction) never sees an
        # AttributeError.
        self._checked = checked
        self._knob_pos = 18.0 if checked else 2.0
        self.setFixedSize(36, 20)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAccessibleName("On/off switch")
        self._knob = QPropertyAnimation(self, b"knob_pos")
        reduce_motion = os.environ.get("HONOR_CONTROL_REDUCE_MOTION", "").lower()
        self._knob.setDuration(0 if reduce_motion in {"1", "true", "yes"} else 120)
        self._knob.setEasingCurve(QEasingCurve.Type.OutCubic)

    def isChecked(self) -> bool:  # noqa: N802 - Qt API naming
        """Return the current on/off state."""
        return self._checked

    def setChecked(self, checked: bool) -> None:  # noqa: N802 - Qt API naming
        """Programmatically set the state without emitting ``toggled``."""
        if checked == self._checked:
            return
        self._checked = checked
        self._animate_to(18.0 if checked else 2.0)
        self.update()

    def _animate_to(self, target: float) -> None:
        """Animate the knob to ``target`` x-position."""
        self._knob.stop()
        self._knob.setStartValue(self._knob_pos)
        self._knob.setEndValue(target)
        self._knob.start()
        self._knob_pos = target

    # Qt property plumbing (used by the knob-position QPropertyAnimation).
    def _get_knob(self) -> float:
        # getattr-default so the property is safe to read before __init__
        # finishes initialising (PySide performs early metacall reads).
        return float(getattr(self, "_knob_pos", 0.0))

    def _set_knob(self, value: float) -> None:
        self._knob_pos = float(value)
        self.update()

    knob_pos = Property(float, _get_knob, _set_knob)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802 - Qt API
        """Flip state on click."""
        if event.button() == Qt.MouseButton.LeftButton:
            self._checked = not self._checked
            self._animate_to(18.0 if self._checked else 2.0)
            self.toggled.emit(self._checked)
        super().mouseReleaseEvent(event)

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802 - Qt API
        """Support standard keyboard activation."""
        if event.key() in (Qt.Key.Key_Space, Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self._checked = not self._checked
            self._animate_to(18.0 if self._checked else 2.0)
            self.toggled.emit(self._checked)
            event.accept()
            return
        super().keyPressEvent(event)

    def paintEvent(self, event: QPaintEvent) -> None:  # noqa: N802 - Qt API
        """Draw the pill track and the circular knob."""
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        # Track
        track = (
            self.palette().color(self.palette().ColorRole.Highlight)
            if self._checked
            else self.palette().color(self.palette().ColorRole.Mid)
        )
        painter.setBrush(track)
        painter.setPen(QPen(QColor(0, 0, 0, 0), 0))
        painter.drawRoundedRect(self.rect().adjusted(0, 1, -1, -2), 9, 9)
        # Knob
        painter.setBrush(QColor("#ffffff"))
        painter.drawEllipse(QPoint(int(self._knob_pos) + 8, 10), 7, 7)


class SectionLabel(QLabel):
    """A bold, slightly larger label used as a sub-section heading."""

    def __init__(self, text: str, parent: QWidget | None = None) -> None:
        """Create the heading label."""
        super().__init__(text, parent)
        self.setStyleSheet("font-weight: 600; font-size: 13px;")


class InfoRow(QWidget):
    """A two-column label/value row used in status grids.

    The label uses the ``Mid`` palette role (a dimmer text colour) which
    adapts to both light and dark themes; the value uses ``WindowText``
    so it is always readable.
    """

    def __init__(
        self,
        label: str,
        value: str = "—",
        parent: QWidget | None = None,
    ) -> None:
        """Create the row; call :meth:`set_value` to update."""
        super().__init__(parent)
        self.setObjectName("HonorInfoRow")
        h = QHBoxLayout(self)
        h.setContentsMargins(0, 6, 0, 6)
        h.setSpacing(12)
        self._label = QLabel(label)
        self._label.setProperty("muted", True)
        self._value = QLabel(value)
        self._value.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        self._value.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        # Value uses the default WindowText role — always readable.
        h.addWidget(self._label)
        h.addStretch(1)
        h.addWidget(self._value)

    def set_value(self, value: str) -> None:
        """Update the right-hand value cell."""
        self._value.setText(str(value))


class CollapsibleSection(QGroupBox):
    """A titled group that can collapse to just its header.

    Start collapsed by passing ``collapsed=True``; click the title to
    toggle. Used for "Advanced" sections.
    """

    def __init__(
        self, title: str, collapsed: bool = False, parent: QWidget | None = None
    ) -> None:
        """Create the collapsible group with a header toggle."""
        super().__init__(title, parent)
        self.setCheckable(True)
        self.setChecked(not collapsed)
        self._collapsed = collapsed
        self.toggled.connect(self._on_toggled)
        self._on_toggled(not collapsed)

    def _on_toggled(self, checked: bool) -> None:
        """Show/hide the body when the checkbox header is toggled."""
        self._collapsed = not checked
        for child in self.findChildren(
            QWidget, options=Qt.FindChildOption.FindDirectChildrenOnly
        ):
            child.setVisible(checked)


class PillBadge(QPushButton):
    """A small rounded badge (e.g. "Root service")."""

    def __init__(
        self, text: str, color: str = "#3daee9", parent: QWidget | None = None
    ) -> None:
        """Create a non-focusable badge with the given colour."""
        super().__init__(text, parent)
        self.setEnabled(False)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setCursor(Qt.CursorShape.ArrowCursor)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self.setStyleSheet(
            f"QPushButton {{ background: {color}; color: white; border: none;"
            " border-radius: 8px; padding: 2px 10px; font-size: 11px;"
            " font-weight: 600; }}"
        )
