"""Reusable widgets and small formatting helpers for the WinFirst UI."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Iterable, Sequence

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QSizePolicy,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from winfirst.services.storage_health_service import format_bytes as _format_bytes


# --------------------------------------------------------------------------- helpers
def format_bytes(value: float | int | None) -> str:
    return _format_bytes(value)


def format_datetime(value: str, *, local: bool = True) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if local:
            parsed = parsed.astimezone()
        return parsed.strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError):
        return text.replace("T", " ")[:19]


def format_yes_no(value: Any) -> str:
    return "Yes" if bool(value) else "No"


def elide(text: str, limit: int = 120) -> str:
    text = str(text or "").replace("\r", " ").replace("\n", " ").strip()
    return text if len(text) <= limit else text[: limit - 1] + "\u2026"


def clear_layout(layout) -> None:
    if layout is None:
        return
    while layout.count():
        item = layout.takeAt(0)
        widget = item.widget()
        if widget is not None:
            widget.setParent(None)
            widget.deleteLater()
        child = item.layout()
        if child is not None:
            clear_layout(child)


def make_button(
    text: str,
    *,
    variant: str | None = None,
    tooltip: str = "",
    slot=None,
) -> QPushButton:
    button = QPushButton(text)
    if variant:
        button.setProperty("variant", variant)
    if tooltip:
        button.setToolTip(tooltip)
    if slot is not None:
        button.clicked.connect(slot)
    return button


# --------------------------------------------------------------------------- basics
class Card(QFrame):
    """A bordered panel with an optional title and subtitle."""

    def __init__(
        self,
        title: str = "",
        subtitle: str = "",
        *,
        parent: QWidget | None = None,
        object_name: str = "Card",
    ) -> None:
        super().__init__(parent)
        self.setObjectName(object_name)
        self.outer = QVBoxLayout(self)
        self.outer.setContentsMargins(16, 15, 16, 15)
        self.outer.setSpacing(9)
        self.title_label: QLabel | None = None
        if title:
            self.title_label = QLabel(title)
            self.title_label.setObjectName("CardTitle")
            self.outer.addWidget(self.title_label)
        if subtitle:
            sub = QLabel(subtitle)
            sub.setObjectName("SmallMuted")
            sub.setWordWrap(True)
            self.outer.addWidget(sub)
        self.body = QVBoxLayout()
        self.body.setSpacing(8)
        self.outer.addLayout(self.body)

    def add(self, widget: QWidget) -> QWidget:
        self.body.addWidget(widget)
        return widget

    def add_layout(self, layout) -> None:
        self.body.addLayout(layout)


class StatCard(QFrame):
    def __init__(self, caption: str, value: str = "—", *, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("StatCard")
        self.setMinimumWidth(150)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(15, 12, 15, 12)
        layout.setSpacing(3)
        self.value_label = QLabel(value)
        self.value_label.setObjectName("StatValue")
        self.caption_label = QLabel(caption)
        self.caption_label.setObjectName("StatCaption")
        self.caption_label.setWordWrap(True)
        layout.addWidget(self.value_label)
        layout.addWidget(self.caption_label)

    def set_value(self, value: Any, detail: str = "") -> None:
        self.value_label.setText(str(value))
        if detail:
            self.caption_label.setText(detail)


class Badge(QLabel):
    def __init__(self, text: str = "", status: str = "muted", *, parent: QWidget | None = None) -> None:
        super().__init__(text, parent)
        self.setObjectName("Badge")
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.set_status(status)

    def set_status(self, status: str) -> None:
        self.setProperty("status", status or "muted")
        self.style().unpolish(self)
        self.style().polish(self)
        self.update()


class Banner(QLabel):
    def __init__(self, text: str = "", level: str = "info", *, parent: QWidget | None = None) -> None:
        super().__init__(text, parent)
        self.setObjectName("Banner")
        self.setWordWrap(True)
        self.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.set_level(level)
        self.setVisible(bool(text))

    def set_level(self, level: str) -> None:
        self.setProperty("level", level or "info")
        self.style().unpolish(self)
        self.style().polish(self)
        self.update()

    def show_message(self, text: str, level: str = "info") -> None:
        self.setText(text)
        self.set_level(level)
        self.setVisible(bool(text))


class PageHeader(QWidget):
    def __init__(self, title: str, subtitle: str = "", *, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(4)
        row = QHBoxLayout()
        row.setSpacing(12)
        text_column = QVBoxLayout()
        text_column.setSpacing(3)
        self.title_label = QLabel(title)
        self.title_label.setObjectName("PageTitle")
        self.subtitle_label = QLabel(subtitle)
        self.subtitle_label.setObjectName("PageSubtitle")
        self.subtitle_label.setWordWrap(True)
        if not subtitle:
            self.subtitle_label.hide()
        text_column.addWidget(self.title_label)
        text_column.addWidget(self.subtitle_label)
        row.addLayout(text_column, 1)
        self.actions = QHBoxLayout()
        self.actions.setSpacing(8)
        row.addLayout(self.actions, 0)
        outer.addLayout(row)

    def add_action(self, widget: QWidget) -> None:
        self.actions.addWidget(widget)

    def set_subtitle(self, text: str) -> None:
        self.subtitle_label.setText(text)
        self.subtitle_label.setVisible(bool(text))


class Separator(QFrame):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("Separator")
        self.setFrameShape(QFrame.Shape.HLine)


class KeyValueGrid(QWidget):
    """Compact two-column key/value list used on summary cards."""

    def __init__(self, rows: Iterable[tuple[str, Any]] = (), *, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(5)
        self.set_rows(rows)

    def set_rows(self, rows: Iterable[tuple[str, Any]]) -> None:
        clear_layout(self._layout)
        for key, value in rows:
            row = QHBoxLayout()
            row.setSpacing(10)
            label = QLabel(str(key))
            label.setObjectName("SmallMuted")
            label.setMinimumWidth(140)
            value_label = QLabel(str(value) if value not in (None, "") else "Unavailable")
            value_label.setWordWrap(True)
            value_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            row.addWidget(label, 0)
            row.addWidget(value_label, 1)
            self._layout.addLayout(row)


class DataTable(QTableWidget):
    """Read-only table with helpers for payload-bearing rows."""

    def __init__(
        self,
        headers: Sequence[str],
        *,
        parent: QWidget | None = None,
        height: int = 320,
        stretch_last: bool = True,
    ) -> None:
        super().__init__(0, len(headers), parent)
        self.setHorizontalHeaderLabels([str(header) for header in headers])
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.setAlternatingRowColors(True)
        self.verticalHeader().setVisible(False)
        self.verticalHeader().setDefaultSectionSize(27)
        self.setMinimumHeight(height)
        self.setSortingEnabled(False)
        header = self.horizontalHeader()
        header.setHighlightSections(False)
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        if stretch_last:
            header.setStretchLastSection(True)
        self._payloads: list[Any] = []

    def set_rows(
        self,
        rows: Iterable[Sequence[Any]],
        *,
        payloads: Sequence[Any] | None = None,
        tooltips: Sequence[str] | None = None,
    ) -> None:
        values = [list(row) for row in rows]
        self.setUpdatesEnabled(False)
        try:
            self.setRowCount(0)
            self.setRowCount(len(values))
            self._payloads = list(payloads) if payloads is not None else [None] * len(values)
            for row_index, row in enumerate(values):
                for column, value in enumerate(row):
                    if column >= self.columnCount():
                        break
                    if isinstance(value, QTableWidgetItem):
                        self.setItem(row_index, column, value)
                        continue
                    text = "" if value is None else str(value)
                    item = QTableWidgetItem(text)
                    if tooltips and row_index < len(tooltips) and tooltips[row_index] and column == 0:
                        item.setToolTip(tooltips[row_index])
                    if column == 0 and row_index < len(self._payloads):
                        item.setData(Qt.ItemDataRole.UserRole, self._payloads[row_index])
                    self.setItem(row_index, column, item)
        finally:
            self.setUpdatesEnabled(True)

    def selected_payloads(self) -> list[Any]:
        rows = sorted({index.row() for index in self.selectionModel().selectedRows()})
        result: list[Any] = []
        for row in rows:
            if 0 <= row < len(self._payloads):
                payload = self._payloads[row]
                if payload is not None:
                    result.append(payload)
        return result

    def selected_row(self) -> int:
        rows = self.selectionModel().selectedRows()
        return rows[0].row() if rows else -1

    def payload_at(self, row: int) -> Any:
        if 0 <= row < len(self._payloads):
            return self._payloads[row]
        return None

    def resize_columns(self, widths: Sequence[int]) -> None:
        for index, width in enumerate(widths):
            if index < self.columnCount():
                self.setColumnWidth(index, width)

    def select_all_rows(self) -> None:
        self.selectAll()


def status_color(status: str) -> QColor:
    lowered = (status or "").casefold()
    if lowered in {"success", "passed", "good", "enabled", "running", "match", "installed", "ok"}:
        return QColor("#45bf7c")
    if lowered in {"failed", "critical", "blocked", "denied", "unsupported", "error"}:
        return QColor("#ef6b6b")
    if lowered in {"partial", "warning", "pending", "different", "not configured"}:
        return QColor("#e3b341")
    return QColor("#9aacbd")


def color_item(item: QTableWidgetItem, status: str) -> None:
    item.setForeground(status_color(status))


def section_label(text: str) -> QLabel:
    label = QLabel(text.upper())
    label.setObjectName("SectionLabel")
    return label


def hint(text: str) -> QLabel:
    label = QLabel(text)
    label.setObjectName("SmallMuted")
    label.setWordWrap(True)
    return label


def strong_label(text: str) -> QLabel:
    label = QLabel(text)
    font = QFont(label.font())
    font.setBold(True)
    label.setFont(font)
    return label


def scrollable_widget(parent: QWidget | None = None) -> tuple[QWidget, QVBoxLayout]:
    widget = QWidget(parent)
    layout = QVBoxLayout(widget)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(14)
    return widget, layout
