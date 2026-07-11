"""Interactive temperature/fan-speed curve graph."""

from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QMouseEvent, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QWidget

from honor_control.core.models import FanCurvePoint
from honor_control.core.validation import validate_curve_points


class FanCurveGraph(QWidget):
    """Single-source curve editor with add, select, drag and remove gestures."""

    points_changed = Signal(object)  # tuple[FanCurvePoint, ...]
    selection_changed = Signal(int)  # selected index, -1 for none

    TEMP_MIN = 40_000
    TEMP_MAX = 100_000
    MARGIN_LEFT = 48
    MARGIN_RIGHT = 18
    MARGIN_TOP = 18
    MARGIN_BOTTOM = 34

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setMinimumHeight(260)
        self.setMouseTracking(True)
        self._points = [
            FanCurvePoint(40_000, 0),
            FanCurvePoint(60_000, 30),
            FanCurvePoint(80_000, 65),
            FanCurvePoint(95_000, 100),
        ]
        self._selected = -1
        self._dragging = False

    @property
    def points(self) -> tuple[FanCurvePoint, ...]:
        return tuple(self._points)

    @property
    def selected_index(self) -> int:
        return self._selected

    def set_points(self, points: list[FanCurvePoint]) -> None:
        validated = validate_curve_points([(p.temp_mc, p.speed) for p in points])
        self._points = list(validated)
        self._selected = min(self._selected, len(self._points) - 1)
        self.update()
        self.selection_changed.emit(self._selected)

    def select(self, index: int) -> None:
        self._selected = index if 0 <= index < len(self._points) else -1
        self.selection_changed.emit(self._selected)
        self.update()

    def add_point(self) -> None:
        if len(self._points) >= 12:
            return
        largest_gap = max(
            range(len(self._points) - 1),
            key=lambda i: self._points[i + 1].temp_mc - self._points[i].temp_mc,
        )
        left, right = self._points[largest_gap], self._points[largest_gap + 1]
        temp = ((left.temp_mc + right.temp_mc) // 2 // 1000) * 1000
        speed = round((left.speed + right.speed) / 2 / 5) * 5
        self._points.insert(largest_gap + 1, FanCurvePoint(temp, speed))
        self._selected = largest_gap + 1
        self._emit_change()

    def remove_selected(self) -> None:
        if self._selected < 0 or len(self._points) <= 2:
            return
        self._points.pop(self._selected)
        self._selected = min(self._selected, len(self._points) - 1)
        self._emit_change()

    def update_selected(self, temp_mc: int, speed: int) -> None:
        if self._selected < 0:
            return
        index = self._selected
        minimum = (
            self.TEMP_MIN if index == 0 else self._points[index - 1].temp_mc + 1000
        )
        maximum = (
            self.TEMP_MAX
            if index == len(self._points) - 1
            else self._points[index + 1].temp_mc - 1000
        )
        temp_mc = max(minimum, min(maximum, (temp_mc // 1000) * 1000))
        speed = max(0, min(100, speed))
        if index > 0:
            speed = max(speed, self._points[index - 1].speed)
        if index < len(self._points) - 1:
            speed = min(speed, self._points[index + 1].speed)
        if temp_mc >= 95_000:
            speed = 100
        self._points[index] = FanCurvePoint(temp_mc, speed)
        self._emit_change()

    def _plot_rect(self) -> QRectF:
        return QRectF(
            self.MARGIN_LEFT,
            self.MARGIN_TOP,
            max(1, self.width() - self.MARGIN_LEFT - self.MARGIN_RIGHT),
            max(1, self.height() - self.MARGIN_TOP - self.MARGIN_BOTTOM),
        )

    def _to_position(self, point: FanCurvePoint) -> QPointF:
        rect = self._plot_rect()
        x = (
            rect.left()
            + (point.temp_mc - self.TEMP_MIN)
            / (self.TEMP_MAX - self.TEMP_MIN)
            * rect.width()
        )
        y = rect.bottom() - point.speed / 100 * rect.height()
        return QPointF(x, y)

    def _from_position(self, position: QPointF) -> tuple[int, int]:
        rect = self._plot_rect()
        x = max(rect.left(), min(rect.right(), position.x()))
        y = max(rect.top(), min(rect.bottom(), position.y()))
        temp = self.TEMP_MIN + round(
            (x - rect.left()) / rect.width() * (self.TEMP_MAX - self.TEMP_MIN)
        )
        speed = round((rect.bottom() - y) / rect.height() * 100)
        return round(temp / 1000) * 1000, round(speed / 5) * 5

    def _nearest(self, position: QPointF) -> int:
        distances = [
            (self._to_position(point) - position).manhattanLength()
            for point in self._points
        ]
        nearest = min(range(len(distances)), key=distances.__getitem__)
        return nearest if distances[nearest] <= 18 else -1

    def paintEvent(self, _event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = self._plot_rect()
        grid = QColor(128, 128, 128, 75)
        text = self.palette().color(self.foregroundRole())
        for speed in range(0, 101, 20):
            y = rect.bottom() - speed / 100 * rect.height()
            painter.setPen(QPen(grid, 1))
            painter.drawLine(QPointF(rect.left(), y), QPointF(rect.right(), y))
            painter.setPen(text)
            painter.drawText(4, round(y + 4), f"{speed}%")
        for temp_c in range(40, 101, 10):
            x = rect.left() + (temp_c - 40) / 60 * rect.width()
            painter.setPen(QPen(grid, 1))
            painter.drawLine(QPointF(x, rect.top()), QPointF(x, rect.bottom()))
            painter.setPen(text)
            painter.drawText(round(x - 9), self.height() - 8, f"{temp_c}°")
        path = QPainterPath()
        for index, point in enumerate(self._points):
            position = self._to_position(point)
            if index == 0:
                path.moveTo(position)
            else:
                path.lineTo(position)
        accent = self.palette().color(self.palette().ColorRole.Highlight)
        painter.setPen(QPen(accent, 3))
        painter.drawPath(path)
        for index, point in enumerate(self._points):
            position = self._to_position(point)
            radius = 7 if index == self._selected else 5
            painter.setPen(QPen(accent, 2))
            painter.setBrush(
                self.palette().color(self.palette().ColorRole.Base)
                if index != self._selected
                else accent
            )
            painter.drawEllipse(position, radius, radius)

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.RightButton:
            self.select(self._nearest(event.position()))
            self.remove_selected()
            return
        if event.button() != Qt.MouseButton.LeftButton:
            return
        nearest = self._nearest(event.position())
        if nearest >= 0:
            self.select(nearest)
            self._dragging = True
            return
        if len(self._points) >= 12:
            return
        temp, speed = self._from_position(event.position())
        insert_at = next(
            (i for i, point in enumerate(self._points) if point.temp_mc > temp),
            len(self._points),
        )
        minimum = (
            self.TEMP_MIN
            if insert_at == 0
            else self._points[insert_at - 1].temp_mc + 1000
        )
        maximum = (
            self.TEMP_MAX
            if insert_at == len(self._points)
            else self._points[insert_at].temp_mc - 1000
        )
        if minimum > maximum:
            return
        temp = max(minimum, min(maximum, temp))
        if insert_at > 0:
            speed = max(speed, self._points[insert_at - 1].speed)
        if insert_at < len(self._points):
            speed = min(speed, self._points[insert_at].speed)
        if temp >= 95_000:
            speed = 100
        self._points.insert(insert_at, FanCurvePoint(temp, speed))
        self._selected = insert_at
        self._dragging = True
        self._emit_change()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if self._dragging and self._selected >= 0:
            self.update_selected(*self._from_position(event.position()))

    def mouseReleaseEvent(self, _event: QMouseEvent) -> None:  # noqa: N802
        self._dragging = False

    def _emit_change(self) -> None:
        self.points_changed.emit(self.points)
        self.selection_changed.emit(self._selected)
        self.update()
