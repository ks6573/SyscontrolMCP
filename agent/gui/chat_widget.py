"""
SysControl GUI — Scrollable chat area with message bubbles.

Manages the list of MessageBubble widgets, streaming updates,
tool-call indicators, and auto-scrolling behaviour.
"""

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from agent.gui.message_bubble import MessageBubble


class _ToolIndicator(QFrame):
    """Subtle inline row showing which tool is running."""

    def __init__(self, names: list[str], palette: dict[str, str], parent: QWidget | None = None):
        super().__init__(parent)
        self.setStyleSheet("background: transparent; border: none;")

        label_text = names[0] + (f" +{len(names) - 1} more" if len(names) > 1 else "")
        label = QLabel(f"\u25cf  {label_text}\u2026")
        label.setFont(QFont("-apple-system", 12))
        label.setStyleSheet(f"color: {palette['tool_text']}; background: transparent;")

        layout = QHBoxLayout(self)
        layout.setContentsMargins(4, 2, 8, 2)
        layout.addWidget(label)
        layout.addStretch()


class ChatWidget(QScrollArea):
    """Scrollable message area containing chat bubbles and tool indicators."""

    def __init__(self, palette: dict[str, str], parent: QWidget | None = None):
        super().__init__(parent)
        self._palette = palette
        self._current_bubble: MessageBubble | None = None
        self._tool_indicator: _ToolIndicator | None = None
        self._auto_scroll = True

        # Inner container widget
        self._container = QWidget()
        self._layout = QVBoxLayout(self._container)
        self._layout.setContentsMargins(48, 20, 48, 24)
        self._layout.setSpacing(16)
        self._layout.addStretch()  # push bubbles to the top initially

        self.setWidget(self._container)
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        # Track scroll position for auto-scroll logic
        vbar = self.verticalScrollBar()
        vbar.rangeChanged.connect(self._on_range_changed)
        vbar.valueChanged.connect(self._on_scroll)

    # ── Public API ─────────────────────────────────────────────────────────

    def add_user_message(self, text: str) -> None:
        """Add a completed user message bubble."""
        bubble = MessageBubble("user", self._palette, parent=self._container)
        bubble.set_text(text)
        self._insert_widget(bubble)
        self._current_bubble = None

    def begin_assistant_message(self) -> MessageBubble:
        """Create a new empty assistant bubble for streaming into."""
        bubble = MessageBubble("assistant", self._palette, parent=self._container)
        self._insert_widget(bubble)
        self._current_bubble = bubble
        return bubble

    def append_to_current(self, text: str) -> None:
        """Append streaming text to the current assistant bubble."""
        if self._current_bubble is None:
            self.begin_assistant_message()
        self._current_bubble.append_text(text)

    def finalize_current(self, elapsed: float) -> None:
        """Finalize the current assistant bubble — re-render as Markdown."""
        if self._current_bubble is not None:
            self._remove_tool_indicator()
            self._current_bubble.finalize()
            self._current_bubble = None

    def show_tool_indicator(self, names: list[str]) -> None:
        """Show a 'Running tool...' indicator row."""
        self._remove_tool_indicator()
        self._tool_indicator = _ToolIndicator(names, self._palette, parent=self._container)
        self._insert_widget(self._tool_indicator)

    def hide_tool_indicator(self) -> None:
        """Remove the tool indicator."""
        self._remove_tool_indicator()

    def show_error(self, category: str, message: str) -> None:
        """Show an error message as a tinted bubble."""
        frame = QFrame(self._container)
        frame.setStyleSheet(f"""
            QFrame {{
                background-color: {self._palette["error_bg"]};
                border-radius: 10px;
                border: none;
            }}
        """)
        label = QLabel(f"{category}: {message}")
        label.setWordWrap(True)
        label.setFont(QFont("-apple-system", 13))
        label.setStyleSheet(f"color: {self._palette['error_text']}; background: transparent;")

        layout = QHBoxLayout(frame)
        layout.setContentsMargins(14, 10, 14, 10)
        layout.addWidget(label)

        self._insert_widget(frame)

    def clear_chat(self) -> None:
        """Remove all messages and indicators."""
        # Remove everything except the stretch
        while self._layout.count() > 1:
            item = self._layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self._current_bubble = None
        self._tool_indicator = None

    # ── Internal ───────────────────────────────────────────────────────────

    def _insert_widget(self, widget: QWidget) -> None:
        """Insert a widget before the trailing stretch."""
        idx = self._layout.count() - 1  # before the stretch
        self._layout.insertWidget(idx, widget)

    def _remove_tool_indicator(self) -> None:
        if self._tool_indicator is not None:
            self._layout.removeWidget(self._tool_indicator)
            self._tool_indicator.deleteLater()
            self._tool_indicator = None

    def _on_scroll(self, value: int) -> None:
        """Track whether the user has scrolled away from the bottom."""
        vbar = self.verticalScrollBar()
        self._auto_scroll = (vbar.maximum() - value) < 50

    def _on_range_changed(self, _min: int, _max: int) -> None:
        """Auto-scroll to bottom when new content is added (if user is near bottom)."""
        if self._auto_scroll:
            QTimer.singleShot(0, self._scroll_to_bottom)

    def _scroll_to_bottom(self) -> None:
        vbar = self.verticalScrollBar()
        vbar.setValue(vbar.maximum())
