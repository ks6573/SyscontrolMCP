"""
SysControl GUI — Theme detection and QSS stylesheets.

Detects macOS dark/light mode and provides matching Qt Style Sheets.
"""

import subprocess


def is_dark_mode() -> bool:
    """Detect macOS dark mode via AppleInterfaceStyle defaults."""
    try:
        result = subprocess.run(
            ["defaults", "read", "-g", "AppleInterfaceStyle"],
            capture_output=True, text=True, timeout=2,
        )
        return result.stdout.strip().lower() == "dark"
    except Exception:
        return True


# ── Color palettes ────────────────────────────────────────────────────────────

DARK = {
    "window_bg":        "#2b2b2b",
    "chat_bg":          "#2b2b2b",
    "user_bubble":      "#3a3a3a",
    "user_bubble_text": "#f0f0f0",
    "asst_bubble":      "transparent",
    "asst_bubble_text": "#e0ddd9",
    "input_bg":         "#333333",
    "input_border":     "#444444",
    "input_text":       "#e0ddd9",
    "placeholder":      "#706e6a",
    "status_bg":        "#2b2b2b",
    "status_text":      "#706e6a",
    "accent":           "#c4715b",
    "send_bg":          "#c4715b",
    "send_hover":       "#a85d4a",
    "send_text":        "#ffffff",
    "toolbar_bg":       "#252525",
    "toolbar_text":     "#9a9590",
    "tool_indicator":   "#353535",
    "tool_text":        "#8a8580",
    "error_bg":         "#3d2020",
    "error_text":       "#f87171",
    "scrollbar":        "#484440",
    "scrollbar_bg":     "transparent",
    "border":           "#3a3836",
    "code_bg":          "#222020",
    "avatar_bg":        "#c4715b",
}

LIGHT = {
    "window_bg":        "#f5f5f0",
    "chat_bg":          "#f5f5f0",
    "user_bubble":      "#e8e6e1",
    "user_bubble_text": "#1a1a1a",
    "asst_bubble":      "transparent",
    "asst_bubble_text": "#2d2a26",
    "input_bg":         "#ffffff",
    "input_border":     "#ddd9d4",
    "input_text":       "#2d2a26",
    "placeholder":      "#b0aa9f",
    "status_bg":        "#eeece7",
    "status_text":      "#9a9590",
    "accent":           "#c4715b",
    "send_bg":          "#c4715b",
    "send_hover":       "#a85d4a",
    "send_text":        "#ffffff",
    "toolbar_bg":       "#eeece7",
    "toolbar_text":     "#6a6560",
    "tool_indicator":   "#e8e6e1",
    "tool_text":        "#8a8580",
    "error_bg":         "#fff0f0",
    "error_text":       "#dc2626",
    "scrollbar":        "#ccc8c2",
    "scrollbar_bg":     "transparent",
    "border":           "#ddd9d4",
    "code_bg":          "#e8e6e1",
    "avatar_bg":        "#c4715b",
}


def get_palette(dark: bool = True) -> dict[str, str]:
    """Return the color palette dict for the given mode."""
    return DARK if dark else LIGHT


def load_stylesheet(dark: bool = True) -> str:
    """Return a QSS stylesheet string for the given mode."""
    c = get_palette(dark)
    return f"""
    QMainWindow, QDialog {{
        background-color: {c["window_bg"]};
    }}
    QWidget {{
        font-family: -apple-system, 'SF Pro Text', system-ui, sans-serif;
    }}

    /* ── Toolbar ─────────────────────────────────────── */
    QToolBar {{
        background-color: {c["toolbar_bg"]};
        border: none;
        spacing: 2px;
        padding: 0 12px;
    }}
    QToolBar QToolButton {{
        background: transparent;
        color: {c["toolbar_text"]};
        border: none;
        padding: 4px 10px;
        border-radius: 5px;
        font-size: 12px;
    }}
    QToolBar QToolButton:hover {{
        background-color: {c["input_bg"]};
        color: {c["input_text"]};
    }}
    QToolBar QLabel {{
        color: {c["toolbar_text"]};
        font-size: 13px;
        font-weight: 600;
    }}

    /* ── Status bar ──────────────────────────────────── */
    QStatusBar {{
        background-color: {c["status_bg"]};
        color: {c["status_text"]};
        border: none;
        font-size: 11px;
    }}
    QStatusBar QLabel {{
        color: {c["status_text"]};
        padding: 0 4px;
        font-size: 11px;
    }}

    /* ── Scroll area (chat) ──────────────────────────── */
    QScrollArea {{
        background-color: {c["chat_bg"]};
        border: none;
    }}
    QScrollArea > QWidget > QWidget {{
        background-color: {c["chat_bg"]};
    }}

    /* ── Scrollbar ───────────────────────────────────── */
    QScrollBar:vertical {{
        background: transparent;
        width: 6px;
        border: none;
        margin: 0;
    }}
    QScrollBar::handle:vertical {{
        background: {c["scrollbar"]};
        min-height: 24px;
        border-radius: 3px;
    }}
    QScrollBar::handle:vertical:hover {{
        background: {c["accent"]};
    }}
    QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
    QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background: none; }}
    """
