"""
SysControl GUI — User input widget.

Multi-line text input (1–5 lines, auto-grows) with a Send button.
Enter sends, Shift+Enter inserts a newline.
"""

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFont, QKeyEvent
from PySide6.QtWidgets import (
    QHBoxLayout,
    QPushButton,
    QSizePolicy,
    QTextEdit,
    QWidget,
)


class _AutoGrowTextEdit(QTextEdit):
    """QTextEdit that grows from 1 to 5 lines based on content."""

    submit_requested = Signal()

    def __init__(self, palette: dict[str, str], parent: QWidget | None = None):
        super().__init__(parent)
        self._palette = palette
        self._line_height = 20
        self._min_lines = 1
        self._max_lines = 5

        self.setFont(QFont("SF Pro Text", 15))
        self.setPlaceholderText("Message SysControl\u2026")
        self.setAcceptRichText(False)

        self.setStyleSheet(f"""
            QTextEdit {{
                background-color: {palette["input_bg"]};
                color: {palette["input_text"]};
                border: 1px solid {palette["input_border"]};
                border-radius: 20px;
                padding: 10px 14px;
                selection-background-color: {palette["accent"]};
                font-size: 14px;
            }}
            QTextEdit:focus {{
                border: 1px solid {palette["accent"]};
                outline: none;
            }}
        """)

        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.document().contentsChanged.connect(self._adjust_height)
        self._adjust_height()

    def keyPressEvent(self, event: QKeyEvent) -> None:
        """Enter sends; Shift+Enter inserts newline."""
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            if event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
                super().keyPressEvent(event)
            else:
                self.submit_requested.emit()
            return
        super().keyPressEvent(event)

    def _adjust_height(self) -> None:
        doc_height = self.document().size().height()
        fm = self.fontMetrics()
        self._line_height = fm.lineSpacing()
        min_h = self._line_height * self._min_lines + 24  # padding
        max_h = self._line_height * self._max_lines + 24
        target = max(min_h, min(max_h, int(doc_height) + 24))
        self.setFixedHeight(target)


class InputWidget(QWidget):
    """Text input area with send button."""

    message_submitted = Signal(str)

    def __init__(self, palette: dict[str, str], parent: QWidget | None = None):
        super().__init__(parent)
        self._palette = palette

        self.setStyleSheet(f"""
            InputWidget {{
                background-color: {palette["window_bg"]};
            }}
        """)

        # Text input
        self._text_edit = _AutoGrowTextEdit(palette, self)
        self._text_edit.submit_requested.connect(self._on_submit)

        # Send button
        self._send_btn = QPushButton("\u2191")  # up arrow
        self._send_btn.setFixedSize(36, 36)
        self._send_btn.setFont(QFont("SF Pro Text", 16, QFont.Weight.Bold))
        self._send_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._send_btn.setStyleSheet(f"""
            QPushButton {{
                background-color: {palette["send_bg"]};
                color: {palette["send_text"]};
                border: none;
                border-radius: 18px;
            }}
            QPushButton:hover {{
                background-color: {palette["send_hover"]};
            }}
            QPushButton:disabled {{
                background-color: {palette["input_border"]};
                color: {palette["placeholder"]};
            }}
        """)
        self._send_btn.clicked.connect(self._on_submit)

        # Layout
        layout = QHBoxLayout(self)
        layout.setContentsMargins(48, 10, 48, 16)
        layout.setSpacing(8)
        layout.addWidget(self._text_edit, 1)
        layout.addWidget(self._send_btn, 0, Qt.AlignmentFlag.AlignBottom)

    # ── Public API ─────────────────────────────────────────────────────────

    def set_enabled(self, enabled: bool) -> None:
        """Enable or disable the input area."""
        self._text_edit.setEnabled(enabled)
        self._send_btn.setEnabled(enabled)
        if enabled:
            self._text_edit.setFocus()

    def clear_input(self) -> None:
        """Clear the text field."""
        self._text_edit.clear()

    # ── Internal ───────────────────────────────────────────────────────────

    def _on_submit(self) -> None:
        text = self._text_edit.toPlainText().strip()
        if text:
            self.message_submitted.emit(text)
            self._text_edit.clear()
