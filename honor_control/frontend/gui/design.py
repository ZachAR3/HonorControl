"""Honor Control visual tokens and application-wide Qt styling."""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import QEvent, QObject
from PySide6.QtGui import QColor, QFont, QPalette, QWheelEvent
from PySide6.QtWidgets import (
    QAbstractScrollArea,
    QAbstractSpinBox,
    QApplication,
    QComboBox,
    QSlider,
    QWidget,
)


@dataclass(frozen=True)
class Theme:
    """Resolved palette for one Honor Control color scheme."""

    canvas: str
    surface: str
    text: str
    muted: str
    rule: str
    accent: str
    accent_hover: str
    success: str
    warning: str
    danger: str
    control: str


LIGHT = Theme(
    canvas="#F2F0EA",
    surface="#FAF9F5",
    text="#24231F",
    muted="#6D6A63",
    rule="#D8D4CC",
    accent="#C85A38",
    accent_hover="#B94E30",
    success="#63B239",
    warning="#D8A52B",
    danger="#B74336",
    control="#F6F4EF",
)

DARK = Theme(
    canvas="#1D1E1C",
    surface="#242522",
    text="#ECE8DE",
    muted="#A5A39B",
    rule="#3B3C38",
    accent="#D45B37",
    accent_hover="#E16642",
    success="#7CC243",
    warning="#E0B33C",
    danger="#E06B5A",
    control="#20211F",
)


class WheelControlGuard(QObject):
    """Route wheel gestures over value controls to their containing page.

    Native Qt combo boxes, spin boxes, and sliders otherwise mutate as soon as
    the pointer happens to be over them. Honor Control reserves the wheel for
    page scrolling; values remain editable by click, drag, or keyboard.
    """

    _CONTROL_TYPES = (QComboBox, QAbstractSpinBox, QSlider)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:  # noqa: N802
        if event.type() != QEvent.Type.Wheel or not isinstance(
            watched, self._CONTROL_TYPES
        ):
            return False
        if not isinstance(event, QWheelEvent):
            return True
        scroll_area = self._containing_scroll_area(watched)
        if scroll_area is not None:
            self._scroll(scroll_area, event)
        event.accept()
        return True

    @staticmethod
    def _containing_scroll_area(widget: QWidget) -> QAbstractScrollArea | None:
        parent = widget.parentWidget()
        while parent is not None:
            if isinstance(parent, QAbstractScrollArea):
                return parent
            parent = parent.parentWidget()
        return None

    @staticmethod
    def _scroll(scroll_area: QAbstractScrollArea, event: QWheelEvent) -> None:
        pixel = event.pixelDelta()
        angle = event.angleDelta()
        vertical = scroll_area.verticalScrollBar()
        horizontal = scroll_area.horizontalScrollBar()
        if pixel.y():
            vertical.setValue(vertical.value() - pixel.y())
        elif angle.y():
            steps = angle.y() / 120
            vertical.setValue(
                vertical.value() - round(steps * vertical.singleStep() * 3)
            )
        elif pixel.x():
            horizontal.setValue(horizontal.value() - pixel.x())
        elif angle.x():
            steps = angle.x() / 120
            horizontal.setValue(
                horizontal.value() - round(steps * horizontal.singleStep() * 3)
            )


def system_prefers_dark(app: QApplication) -> bool:
    """Return whether the desktop palette currently uses a dark window color."""
    return app.palette().color(QPalette.ColorRole.Window).lightness() < 128


def apply_design_system(app: QApplication) -> Theme:
    """Apply the Honor Control palette, font, and shared component stylesheet."""
    theme = DARK if system_prefers_dark(app) else LIGHT
    palette = QPalette(app.palette())
    roles = {
        QPalette.ColorRole.Window: theme.canvas,
        QPalette.ColorRole.WindowText: theme.text,
        QPalette.ColorRole.Base: theme.surface,
        QPalette.ColorRole.AlternateBase: theme.canvas,
        QPalette.ColorRole.Text: theme.text,
        QPalette.ColorRole.Button: theme.control,
        QPalette.ColorRole.ButtonText: theme.text,
        QPalette.ColorRole.Highlight: theme.accent,
        QPalette.ColorRole.HighlightedText: "#FFFFFF",
        QPalette.ColorRole.Mid: theme.rule,
        QPalette.ColorRole.ToolTipBase: theme.surface,
        QPalette.ColorRole.ToolTipText: theme.text,
        QPalette.ColorRole.PlaceholderText: theme.muted,
    }
    for role, color in roles.items():
        palette.setColor(role, QColor(color))
    palette.setColor(
        QPalette.ColorGroup.Disabled,
        QPalette.ColorRole.Text,
        QColor(theme.muted),
    )
    palette.setColor(
        QPalette.ColorGroup.Disabled,
        QPalette.ColorRole.ButtonText,
        QColor(theme.muted),
    )
    app.setPalette(palette)

    font = QFont("Inter")
    if not font.exactMatch():
        font = QFont("Noto Sans")
    font.setPixelSize(14)
    app.setFont(font)
    app.setStyleSheet(_style_sheet(theme))
    app.setProperty("honorTheme", "dark" if theme is DARK else "light")
    if not hasattr(app, "_honor_wheel_guard"):
        app._honor_wheel_guard = WheelControlGuard(app)  # type: ignore[attr-defined]
        app.installEventFilter(app._honor_wheel_guard)  # type: ignore[attr-defined]
    return theme


def _style_sheet(t: Theme) -> str:
    return f"""
        * {{
            selection-background-color: {t.accent};
            selection-color: #FFFFFF;
        }}
        QWidget {{
            color: {t.text};
            background: transparent;
        }}
        QMainWindow, QWidget#HonorWindow {{
            background: {t.canvas};
        }}
        QFrame#HonorHeader {{
            background: {t.canvas};
            border-bottom: 1px solid {t.rule};
        }}
        QLabel#HonorBrand {{
            font-size: 16px;
            font-weight: 600;
        }}
        QLabel#HonorDevice, QLabel#HonorMuted, QLabel[muted="true"] {{
            color: {t.muted};
        }}
        QFrame#HonorSidebar {{
            background: {t.canvas};
            border-right: 1px solid {t.rule};
        }}
        QListWidget#HonorSidebarList {{
            background: transparent;
            border: none;
            outline: none;
        }}
        QListWidget#HonorSidebarList::item {{
            color: {t.text};
            min-height: 36px;
            padding: 0 14px;
            border-left: 3px solid transparent;
        }}
        QListWidget#HonorSidebarList::item:hover {{
            color: {t.accent};
            background: transparent;
        }}
        QListWidget#HonorSidebarList::item:selected {{
            color: {t.accent};
            background: transparent;
            border-left: 3px solid {t.accent};
        }}
        QWidget#HonorContent, QScrollArea, QScrollArea > QWidget > QWidget {{
            background: {t.surface};
        }}
        QScrollBar:vertical {{
            width: 8px;
            margin: 0;
            background: {t.surface};
        }}
        QScrollBar::handle:vertical {{
            min-height: 28px;
            background: {t.rule};
            border-radius: 3px;
        }}
        QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
            height: 0;
        }}
        QLabel#HonorPageTitle {{
            font-size: 20px;
            font-weight: 600;
        }}
        QLabel[editHint="true"] {{
            color: {t.accent};
            font-size: 12px;
        }}
        QFrame#HonorCard {{
            background: transparent;
            border: none;
            border-bottom: 1px solid {t.rule};
            border-radius: 0;
        }}
        QLabel#HonorCardTitle {{
            font-size: 15px;
            font-weight: 500;
        }}
        QLabel#HonorPrimaryValue {{
            font-size: 32px;
            font-weight: 600;
        }}
        QWidget#HonorInfoRow {{
            border-bottom: 1px solid {t.rule};
        }}
        QPushButton, QToolButton, QComboBox, QLineEdit, QSpinBox,
        QDoubleSpinBox, QTextEdit {{
            color: {t.text};
            background: {t.control};
            border: 1px solid {t.rule};
            border-radius: 3px;
            min-height: 28px;
            padding: 2px 10px;
        }}
        QPushButton:hover, QToolButton:hover, QComboBox:hover, QLineEdit:hover,
        QSpinBox:hover, QDoubleSpinBox:hover {{
            border-color: {t.accent};
        }}
        QPushButton:focus, QToolButton:focus, QComboBox:focus, QLineEdit:focus,
        QSpinBox:focus, QDoubleSpinBox:focus, QSlider:focus {{
            border: 1px solid {t.accent};
        }}
        QPushButton:checked, QPushButton[accent="true"] {{
            color: #FFFFFF;
            background: {t.accent};
            border-color: {t.accent};
        }}
        QPushButton:checked:hover, QPushButton[accent="true"]:hover {{
            background: {t.accent_hover};
        }}
        QPushButton[quiet="true"], QToolButton[quiet="true"] {{
            color: {t.accent};
            background: transparent;
            border: none;
            padding-left: 4px;
            padding-right: 4px;
        }}
        QPushButton[quiet="true"]:hover, QToolButton[quiet="true"]:hover {{
            color: {t.accent_hover};
            text-decoration: underline;
        }}
        QComboBox[rowControl="true"], QLineEdit[rowControl="true"],
        QSpinBox[rowControl="true"], QDoubleSpinBox[rowControl="true"] {{
            background: {t.control};
            border: 1px solid {t.rule};
            border-radius: 3px;
            min-height: 26px;
            padding: 0 8px;
        }}
        QComboBox[rowControl="true"]:focus, QLineEdit[rowControl="true"]:focus,
        QSpinBox[rowControl="true"]:focus, QDoubleSpinBox[rowControl="true"]:focus {{
            background: {t.surface};
            border-color: {t.accent};
        }}
        QPushButton:disabled, QToolButton:disabled, QComboBox:disabled,
        QLineEdit:disabled, QSpinBox:disabled, QDoubleSpinBox:disabled {{
            color: {t.muted};
            background: transparent;
            border-color: {t.rule};
        }}
        QComboBox::drop-down {{
            width: 22px;
            border: none;
        }}
        QSlider::groove:horizontal {{
            height: 3px;
            background: {t.rule};
        }}
        QSlider::sub-page:horizontal {{
            background: {t.accent};
        }}
        QSlider::handle:horizontal {{
            width: 12px;
            height: 12px;
            margin: -5px 0;
            border-radius: 6px;
            background: {t.accent};
        }}
        QCheckBox, QRadioButton {{
            spacing: 8px;
        }}
        QCheckBox::indicator, QRadioButton::indicator {{
            width: 15px;
            height: 15px;
        }}
        QGroupBox {{
            border: 1px solid {t.rule};
            margin-top: 10px;
            padding: 12px;
        }}
        QStatusBar {{
            color: {t.muted};
            background: {t.canvas};
            border-top: 1px solid {t.rule};
            font-size: 12px;
        }}
        QFrame#HonorBanner {{
            background: {t.warning};
            border-bottom: 1px solid {t.rule};
        }}
        QFrame#HonorBanner QLabel {{
            color: #24231F;
        }}
        QToolTip {{
            color: {t.text};
            background: {t.surface};
            border: 1px solid {t.rule};
            padding: 4px;
        }}
    """
