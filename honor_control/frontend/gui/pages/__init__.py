"""GUI content pages — one module per sidebar entry.

Each module exposes a ``*Page`` class that subclasses :class:`PageBase`.
Pages receive a shared :class:`GuiState` via their constructor so they
can read snapshots and emit user intents.  Pages never receive the
client object or perform D-Bus calls.
"""

from __future__ import annotations

from collections.abc import Iterable

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QLabel, QScrollArea, QVBoxLayout, QWidget

from honor_control.frontend.gui.state import GuiState


class PageBase(QWidget):
    """Base class for every content page.

    Subclasses set the :attr:`title` / :attr:`icon` class attributes and
    implement :meth:`_build` (construct UI) and :meth:`refresh` (load data
    into the UI).  Content is wrapped in a ``QScrollArea`` so tall pages
    don't clip.
    """

    title: str = "Page"
    icon: str = ""
    intent = Signal(str, object)

    def __init__(
        self,
        state: GuiState,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.state = state
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        self._scroll = QScrollArea(self)
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        content = QWidget()
        self._root = QVBoxLayout(content)
        self._root.setContentsMargins(20, 20, 20, 20)
        self._root.setSpacing(14)
        self._scroll.setWidget(content)
        outer.addWidget(self._scroll)
        self._controls: list[QWidget] = []
        self._pending_controls: dict[str, list[QWidget]] = {}
        self._pending_operations: set[str] = set()
        self._build()
        self.state.operation_pending.connect(self._update_pending_started)
        self.state.operation_completed.connect(self._update_pending_completed)

    def _build(self) -> None:
        """Construct the page's widgets. Override in subclasses."""

    def refresh(self) -> None:
        """Pull fresh data from the state into the widgets. Override."""

    def add_widget(self, widget: QWidget) -> QWidget:
        self._root.addWidget(widget)
        return widget

    def register_control(self, widget: QWidget) -> QWidget:
        self._controls.append(widget)
        return widget

    def register_pending_control(self, method: str, widget: QWidget) -> QWidget:
        """Disable ``widget`` while an operation for ``method`` is pending."""
        if widget not in self._controls:
            self.register_control(widget)
        self._pending_controls.setdefault(method, []).append(widget)
        return widget

    def unregister_control(self, widget: QWidget) -> None:
        """Forget a dynamically removed control."""
        try:
            self._controls.remove(widget)
        except ValueError:
            pass

    def set_backend_available(self, available: bool) -> None:
        for w in self._controls:
            pending = any(
                operation.split(":", 1)[0] == method
                and w in self._pending_controls.get(method, ())
                for operation in self._pending_operations
                for method in self._pending_controls
            )
            w.setEnabled(available and not pending)

    def _update_pending_started(self, operation_id: str) -> None:
        self._pending_operations.add(operation_id)
        method = operation_id.split(":", 1)[0]
        for widget in self._pending_controls.get(method, ()):
            widget.setEnabled(False)

    def _update_pending_completed(self, operation_id: str, _result: object) -> None:
        self._pending_operations.discard(operation_id)
        method = operation_id.split(":", 1)[0]
        for widget in self._pending_controls.get(method, ()):
            still_pending = any(
                pending.split(":", 1)[0] == method
                for pending in self._pending_operations
            )
            widget.setEnabled(self.state.connected and not still_pending)

    def add_heading(self, text: str) -> QLabel:
        label = QLabel(text)
        label.setStyleSheet("font-size: 15px; font-weight: 600;")
        self.add_widget(label)
        return label

    def add_stretch(self) -> None:
        self._root.addStretch(1)

    def iter_control_widgets(self) -> Iterable[QWidget]:
        return iter(self._controls)
