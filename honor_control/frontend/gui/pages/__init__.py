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
    show_page_title: bool = True
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
        content.setObjectName("HonorContent")
        self._root = QVBoxLayout(content)
        self._root.setContentsMargins(32, 24, 32, 24)
        self._root.setSpacing(0)
        self._scroll.setWidget(content)
        self._content = content
        outer.addWidget(self._scroll)
        self._controls: list[QWidget] = []
        self._pending_controls: dict[str, list[QWidget]] = {}
        self._pending_operations: set[str] = set()
        # Disable the content container rather than each child independently.
        # Pages use ``setEnabled`` for semantic states too (for example a
        # built-in profile name is read-only, and a curve point editor is
        # disabled until a point is selected).  A container-level gate keeps
        # those intrinsic child states intact across disconnect/reconnect.
        self._backend_available = True
        self._pending_enabled: dict[QWidget, bool] = {}
        if self.show_page_title:
            heading = QLabel(self.title)
            heading.setObjectName("HonorPageTitle")
            heading.setContentsMargins(0, 0, 0, 14)
            self._root.addWidget(heading)
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
        while True:
            try:
                self._controls.remove(widget)
            except ValueError:
                break
        for method, widgets in tuple(self._pending_controls.items()):
            remaining = [item for item in widgets if item is not widget]
            if remaining:
                self._pending_controls[method] = remaining
            else:
                del self._pending_controls[method]
        self._pending_enabled.pop(widget, None)

    def set_backend_available(self, available: bool) -> None:
        self._backend_available = available
        # Pages without service controls (such as local application settings)
        # remain usable while disconnected.
        self._content.setEnabled(available or not self._controls)
        self._reapply_pending_controls()

    def _reapply_pending_controls(self) -> None:
        """Keep controls disabled for operations that are still in flight."""
        for operation_id in self._pending_operations:
            method = operation_id.split(":", 1)[0]
            for widget in self._pending_controls.get(method, ()):
                widget.setEnabled(False)

    def _update_pending_started(self, operation_id: str) -> None:
        self._pending_operations.add(operation_id)
        method = operation_id.split(":", 1)[0]
        for widget in self._pending_controls.get(method, ()):
            self._pending_enabled.setdefault(
                widget, widget.isEnabledTo(self._content)
            )
            widget.setEnabled(False)

    def _update_pending_completed(self, operation_id: str, _result: object) -> None:
        self._pending_operations.discard(operation_id)
        method = operation_id.split(":", 1)[0]
        for widget in self._pending_controls.get(method, ()):
            still_pending = any(
                widget in self._pending_controls.get(pending.split(":", 1)[0], ())
                for pending in self._pending_operations
            )
            if still_pending:
                widget.setEnabled(False)
                continue
            enabled = self._pending_enabled.pop(widget, True)
            widget.setEnabled(enabled)

    def add_heading(self, text: str) -> QLabel:
        label = QLabel(text)
        label.setStyleSheet("font-size: 15px; font-weight: 600;")
        self.add_widget(label)
        return label

    def add_stretch(self) -> None:
        self._root.addStretch(1)

    def iter_control_widgets(self) -> Iterable[QWidget]:
        return iter(self._controls)
