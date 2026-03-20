"""
SysControl GUI — Main window.

Assembles the chat widget, input widget, and toolbar, and wires all
signals between the AgentWorker thread and the UI components.
"""

from __future__ import annotations

import atexit

from PySide6.QtCore import Qt

from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QLabel,
    QMainWindow,
    QSizePolicy,
    QStatusBar,
    QToolBar,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from agent.gui.chat_widget import ChatWidget
from agent.gui.input_widget import InputWidget
from agent.gui.settings_dialog import SettingsDialog, save_config
from agent.gui.worker import AgentWorker, ProviderConfig


class MainWindow(QMainWindow):
    """Main application window — chat interface with toolbar and status bar."""

    def __init__(self, config: ProviderConfig, palette: dict[str, str], parent=None):
        super().__init__(parent)
        self._config = config
        self._palette = palette
        self._worker: AgentWorker | None = None

        self.setWindowTitle("SysControl")
        self.setMinimumSize(600, 500)
        self.resize(800, 650)

        # ── Toolbar ────────────────────────────────────────────────────────
        toolbar = QToolBar("Main")
        toolbar.setMovable(False)
        toolbar.setFloatable(False)
        toolbar.setFixedHeight(44)
        self.addToolBar(toolbar)

        self._model_label = QLabel("SysControl")
        self._model_label.setFont(QFont("-apple-system", 13, QFont.Weight.DemiBold))
        toolbar.addWidget(self._model_label)

        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        toolbar.addWidget(spacer)

        new_chat_btn = QToolButton()
        new_chat_btn.setText("+ New")
        new_chat_btn.clicked.connect(self._on_new_chat)
        toolbar.addWidget(new_chat_btn)

        settings_btn = QToolButton()
        settings_btn.setText("\u2699")
        settings_btn.clicked.connect(self._on_settings)
        toolbar.addWidget(settings_btn)

        # ── Central widget ─────────────────────────────────────────────────
        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self._chat = ChatWidget(palette, parent=central)
        layout.addWidget(self._chat, 1)

        self._input = InputWidget(palette, parent=central)
        layout.addWidget(self._input, 0)

        # ── Status bar ─────────────────────────────────────────────────────
        self._status = QStatusBar()
        self.setStatusBar(self._status)
        self._status_label = QLabel("Connecting\u2026")
        self._status.addPermanentWidget(self._status_label)

        # ── Wire input ─────────────────────────────────────────────────────
        self._input.message_submitted.connect(self._on_user_submit)

        # ── Start worker ───────────────────────────────────────────────────
        self._start_worker(config)

    # ── Worker lifecycle ───────────────────────────────────────────────────

    def _start_worker(self, config: ProviderConfig) -> None:
        """Create and start the agent worker thread."""
        if self._worker is not None:
            self._worker.shutdown()

        self._worker = AgentWorker(config, parent=self)
        self._worker.ready.connect(self._on_worker_ready)
        self._worker.token_received.connect(self._on_token)
        self._worker.tool_started.connect(self._on_tool_started)
        self._worker.tool_finished.connect(self._on_tool_finished)
        self._worker.turn_finished.connect(self._on_turn_finished)
        self._worker.error_occurred.connect(self._on_error)
        self._worker.start()

        # Safety net: clean up MCP subprocesses on exit
        atexit.register(self._cleanup)

    def _cleanup(self) -> None:
        if self._worker is not None:
            self._worker.shutdown()
            self._worker = None

    # ── Slots: user actions ────────────────────────────────────────────────

    def _on_user_submit(self, text: str) -> None:
        self._chat.add_user_message(text)
        self._chat.begin_assistant_message()
        self._input.set_enabled(False)
        self._worker.submit_message(text)

    def _on_new_chat(self) -> None:
        self._chat.clear_chat()
        if self._worker:
            self._worker.clear_session()

    def _on_settings(self) -> None:
        dialog = SettingsDialog(self._palette, parent=self)
        dialog.load_from_config(self._config)
        if dialog.exec():
            new_config = dialog.get_config()
            save_config(new_config)
            self._config = new_config
            self._chat.clear_chat()
            self._status_label.setText("Reconnecting\u2026")
            self._start_worker(new_config)

    # ── Slots: worker signals ──────────────────────────────────────────────

    def _on_worker_ready(self, tool_count: int, label: str, model: str) -> None:
        self._model_label.setText(model)
        self._status_label.setText(f"{tool_count} tools \u00b7 {label}")
        self._input.set_enabled(True)

    def _on_token(self, text: str) -> None:
        self._chat.append_to_current(text)

    def _on_tool_started(self, names: list[str]) -> None:
        self._chat.show_tool_indicator(names)

    def _on_tool_finished(self, name: str, result: str) -> None:
        self._chat.hide_tool_indicator()

    def _on_turn_finished(self, elapsed: float) -> None:
        self._chat.finalize_current(elapsed)
        self._input.set_enabled(True)

    def _on_error(self, category: str, message: str) -> None:
        self._chat.show_error(category, message)
        self._input.set_enabled(True)

    # ── Window lifecycle ───────────────────────────────────────────────────

    def closeEvent(self, event) -> None:
        self._cleanup()
        super().closeEvent(event)
