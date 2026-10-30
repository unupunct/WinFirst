"""Shared Qt theme tokens and stylesheet loading."""
from __future__ import annotations

from pathlib import Path

from PySide6.QtGui import QColor, QFont, QPalette
from PySide6.QtWidgets import QApplication

from winfirst.core.paths import resources_dir

PALETTES: dict[str, dict[str, str]] = {
    "dark": {
        "BG": "#0e1620",
        "PANEL": "#131d28",
        "CARD": "#182431",
        "CARD_ALT": "#1d2b39",
        "BORDER": "#2a3b4c",
        "BORDER_STRONG": "#3a5065",
        "TEXT": "#e8eef5",
        "MUTED": "#9aacbd",
        "FAINT": "#6d8093",
        "ACCENT": "#4aa3e8",
        "ACCENT_HOVER": "#67b5f2",
        "ACCENT_PRESSED": "#3189cb",
        "SELECTION": "#234967",
        "SUCCESS": "#45bf7c",
        "SUCCESS_BG": "#163526",
        "WARNING": "#e3b341",
        "WARNING_BG": "#3a2f13",
        "DANGER": "#ef6b6b",
        "DANGER_BG": "#3a1d21",
        "INFO_BG": "#173047",
        "INPUT": "#101a24",
        "DISABLED": "#5d6b78",
        "SIDEBAR": "#101923",
        "SIDEBAR_HOVER": "#1b2b3a",
    },
    "light": {
        "BG": "#eef2f6",
        "PANEL": "#f7f9fb",
        "CARD": "#ffffff",
        "CARD_ALT": "#f6f9fc",
        "BORDER": "#d8e0e8",
        "BORDER_STRONG": "#b9c6d3",
        "TEXT": "#17212b",
        "MUTED": "#5e6f80",
        "FAINT": "#8493a1",
        "ACCENT": "#1f6feb",
        "ACCENT_HOVER": "#2f7df0",
        "ACCENT_PRESSED": "#175bbf",
        "SELECTION": "#dce9fa",
        "SUCCESS": "#1a7f4b",
        "SUCCESS_BG": "#e6f6ed",
        "WARNING": "#8a6200",
        "WARNING_BG": "#fff6dc",
        "DANGER": "#b42318",
        "DANGER_BG": "#fdeceb",
        "INFO_BG": "#e8f2fd",
        "INPUT": "#ffffff",
        "DISABLED": "#9aa7b4",
        "SIDEBAR": "#ffffff",
        "SIDEBAR_HOVER": "#eef4fa",
    },
}


def palette(name: str) -> dict[str, str]:
    return PALETTES.get(name, PALETTES["dark"])


def _stylesheet_text() -> str:
    path: Path = resources_dir() / "styles" / "app.qss"
    if not path.is_file():
        return ""
    return path.read_text(encoding="utf-8")


def apply_theme(app: QApplication, name: str) -> None:
    """Apply the tokenized QSS and a matching Qt palette."""
    colors = palette(name)
    qpalette = QPalette()
    qpalette.setColor(QPalette.ColorRole.Window, QColor(colors["BG"]))
    qpalette.setColor(QPalette.ColorRole.WindowText, QColor(colors["TEXT"]))
    qpalette.setColor(QPalette.ColorRole.Base, QColor(colors["INPUT"]))
    qpalette.setColor(QPalette.ColorRole.AlternateBase, QColor(colors["CARD_ALT"]))
    qpalette.setColor(QPalette.ColorRole.ToolTipBase, QColor(colors["CARD"]))
    qpalette.setColor(QPalette.ColorRole.ToolTipText, QColor(colors["TEXT"]))
    qpalette.setColor(QPalette.ColorRole.Text, QColor(colors["TEXT"]))
    qpalette.setColor(QPalette.ColorRole.Button, QColor(colors["CARD"]))
    qpalette.setColor(QPalette.ColorRole.ButtonText, QColor(colors["TEXT"]))
    qpalette.setColor(QPalette.ColorRole.BrightText, QColor(colors["DANGER"]))
    qpalette.setColor(QPalette.ColorRole.Link, QColor(colors["ACCENT"]))
    qpalette.setColor(QPalette.ColorRole.Highlight, QColor(colors["ACCENT"]))
    qpalette.setColor(QPalette.ColorRole.HighlightedText, QColor("#ffffff" if name == "dark" else "#ffffff"))
    qpalette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Text, QColor(colors["DISABLED"]))
    qpalette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.ButtonText, QColor(colors["DISABLED"]))
    app.setPalette(qpalette)
    app.setFont(QFont("Segoe UI", 10))
    text = _stylesheet_text()
    for key, value in colors.items():
        text = text.replace(f"@{key}@", value)
    app.setStyleSheet(text)


def refresh_style(widget) -> None:
    style = widget.style()
    if style is not None:
        style.unpolish(widget)
        style.polish(widget)
    widget.update()
