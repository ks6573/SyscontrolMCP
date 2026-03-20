"""
SysControl GUI — Message bubble widget.

User messages render as a clean neutral bubble (right-aligned, dynamically sized to text).
Assistant messages stream text progressively with periodic Markdown re-rendering,
so the user sees tokens as they arrive — raw markdown characters are never visible
because a debounced renderer converts to HTML every 150ms.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QFont, QTextCursor
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QSizePolicy,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

try:
    import markdown as md
    _HAS_MARKDOWN = True
except ImportError:
    _HAS_MARKDOWN = False


class MessageBubble(QFrame):
    """
    A single chat message.

    - User:      right-aligned neutral bubble, sized to content width.
    - Assistant: avatar + progressive streaming text with debounced Markdown rendering.
    """

    def __init__(
        self,
        role: str,          # "user" or "assistant"
        palette: dict[str, str],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._role = role
        self._palette = palette
        self._raw_text = ""
        self._is_user = role == "user"
        self._is_finalized = False

        self.setStyleSheet("background: transparent; border: none;")
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)

        # ── Text browser ───────────────────────────────────────────────────
        self._browser = QTextBrowser()
        self._browser.setOpenExternalLinks(True)
        self._browser.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._browser.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._browser.setFrameShape(QFrame.Shape.NoFrame)
        self._browser.setFont(QFont("-apple-system", 15))
        self._browser.document().contentsChanged.connect(self._adjust_height)

        if self._is_user:
            self._browser.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Minimum)
            self._browser.setStyleSheet(f"""
                QTextBrowser {{
                    background-color: {palette["user_bubble"]};
                    color: {palette["user_bubble_text"]};
                    border: none;
                    padding: 8px 13px;
                    border-radius: 16px;
                    selection-background-color: rgba(255,255,255,0.25);
                }}
            """)
        else:
            self._browser.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
            self._browser.setStyleSheet(f"""
                QTextBrowser {{
                    background: transparent;
                    color: {palette["asst_bubble_text"]};
                    border: none;
                    padding: 2px 0px;
                    selection-background-color: {palette["accent"]};
                }}
            """)

        # ── Avatar (assistant only) ───────────────────────────────────────
        self._avatar: QLabel | None = None
        if not self._is_user:
            self._avatar = QLabel("S")
            self._avatar.setFixedSize(28, 28)
            self._avatar.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self._avatar.setFont(QFont("-apple-system", 13, QFont.Weight.Bold))
            self._avatar.setStyleSheet(f"""
                QLabel {{
                    background-color: {palette["avatar_bg"]};
                    color: #ffffff;
                    border-radius: 14px;
                }}
            """)

        # ── Debounced Markdown renderer (assistant only) ──────────────────
        self._render_timer: QTimer | None = None
        if not self._is_user:
            self._render_timer = QTimer(self)
            self._render_timer.setSingleShot(True)
            self._render_timer.setInterval(150)
            self._render_timer.timeout.connect(self._render_markdown)

        # ── Layout ─────────────────────────────────────────────────────────
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(0)

        if self._is_user:
            row.addStretch(1)
            row.addWidget(self._browser)
        else:
            row.setSpacing(10)
            row.addWidget(self._avatar, 0, Qt.AlignmentFlag.AlignTop)
            row.addWidget(self._browser, 1)

        outer.addLayout(row)

    # ── Public API ─────────────────────────────────────────────────────────

    def append_text(self, text: str) -> None:
        """Append streaming text — shown progressively via cursor insert."""
        self._raw_text += text
        if not self._is_user:
            cursor = self._browser.textCursor()
            cursor.movePosition(QTextCursor.MoveOperation.End)
            cursor.insertText(text)
            self._browser.setTextCursor(cursor)
            # Restart the debounce timer for a markdown re-render
            if self._render_timer is not None:
                self._render_timer.start()

    def set_text(self, text: str) -> None:
        """Set complete text for a user message bubble."""
        self._raw_text = text
        self._browser.setPlainText(text)
        self._adjust_width()   # size bubble to text content

    def finalize(self) -> None:
        """Final authoritative Markdown render of the accumulated text."""
        self._is_finalized = True
        if self._render_timer is not None:
            self._render_timer.stop()

        if not self._raw_text.strip():
            return

        if _HAS_MARKDOWN and self._role == "assistant":
            html = md.markdown(
                self._raw_text,
                extensions=["fenced_code", "tables", "nl2br"],
            )
            self._browser.setHtml(self._wrap_html(html))
        else:
            self._browser.setPlainText(self._raw_text)

    def raw_text(self) -> str:
        return self._raw_text

    # ── Internal ───────────────────────────────────────────────────────────

    def _render_markdown(self) -> None:
        """Debounced: re-render accumulated text as Markdown HTML."""
        if self._is_finalized or not self._raw_text.strip():
            return
        if _HAS_MARKDOWN:
            html = md.markdown(
                self._raw_text,
                extensions=["fenced_code", "tables", "nl2br"],
            )
            self._browser.setHtml(self._wrap_html(html))

    def _adjust_width(self) -> None:
        """For user bubbles: shrink to natural text width, capped at 75% of parent."""
        if not self._is_user:
            return
        doc = self._browser.document()
        doc.setTextWidth(-1)          # disable wrapping to measure natural width
        ideal_w = doc.idealWidth()
        parent = self.parentWidget()
        max_w = min(620, int(parent.width() * 0.75)) if parent else 620
        target_w = min(int(ideal_w) + 28, max_w)  # 28 = 14px padding x 2
        self._browser.setFixedWidth(max(target_w, 48))

    def _adjust_height(self) -> None:
        """Resize browser to fit its document content exactly."""
        if self._is_user:
            self._adjust_width()
        doc_height = self._browser.document().size().height()
        self._browser.setFixedHeight(int(doc_height) + 6)

    def _wrap_html(self, body: str) -> str:
        """Wrap markdown-generated HTML with inline CSS styled for the current palette."""
        fg = self._palette["asst_bubble_text"]
        code_bg = self._palette.get("code_bg", "#222020")
        accent = self._palette["accent"]
        border = self._palette["border"]
        return f"""
        <style>
            body {{
                color: {fg};
                font-family: -apple-system, 'SF Pro Text', system-ui, sans-serif;
                font-size: 15px;
                line-height: 1.7;
                margin: 0; padding: 0;
            }}
            p {{ margin: 6px 0; }}
            strong {{ font-weight: 650; }}
            em {{ font-style: italic; }}
            code {{
                background: {code_bg};
                padding: 2px 6px;
                border-radius: 5px;
                font-family: 'SF Mono', 'Menlo', 'Courier New', monospace;
                font-size: 13px;
            }}
            pre {{
                background: {code_bg};
                padding: 14px 16px;
                border-radius: 8px;
                margin: 8px 0;
                overflow-x: auto;
            }}
            pre code {{
                background: transparent;
                padding: 0;
                border-radius: 0;
                font-size: 13px;
            }}
            a {{ color: {accent}; text-decoration: none; }}
            a:hover {{ text-decoration: underline; }}
            table {{ border-collapse: collapse; margin: 10px 0; width: 100%; }}
            th, td {{ border: 1px solid {border}; padding: 7px 12px; text-align: left; }}
            th {{ font-weight: 600; }}
            h1 {{ font-size: 20px; font-weight: 700; margin: 14px 0 6px; }}
            h2 {{ font-size: 17px; font-weight: 650; margin: 12px 0 5px; }}
            h3 {{ font-size: 15px; font-weight: 650; margin: 10px 0 4px; }}
            ul, ol {{ margin: 6px 0; padding-left: 24px; }}
            li {{ margin: 3px 0; }}
            blockquote {{
                margin: 8px 0 8px 4px;
                padding-left: 12px;
                border-left: 3px solid {accent};
                opacity: 0.8;
            }}
            hr {{ border: none; border-top: 1px solid {border}; margin: 12px 0; }}
        </style>
        {body}
        """

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if self._is_user:
            self._adjust_width()
