"""Base page with lazy loading, a scrolling body and async helpers."""
from __future__ import annotations

import logging
from typing import Any, Callable

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from winfirst.ui.widgets import Banner, PageHeader, make_button
from winfirst.ui.workers import run_async

_log = logging.getLogger("winfirst.ui.page")


class BasePage(QWidget):
    page_key: str = ""
    page_title: str = ""
    page_subtitle: str = ""
    scrollable: bool = True

    def __init__(self, ctx: Any, *, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.ctx = ctx
        self._loaded = False
        self._workers: set[Any] = set()

        root = QVBoxLayout(self)
        root.setContentsMargins(26, 22, 26, 18)
        root.setSpacing(12)

        self.header = PageHeader(self.page_title, self.page_subtitle)
        root.addWidget(self.header)

        self.error_banner = Banner("", "critical")
        self.error_banner.hide()
        root.addWidget(self.error_banner)

        self.busy_banner = Banner("", "info")
        self.busy_banner.hide()
        root.addWidget(self.busy_banner)

        if self.scrollable:
            self.scroll = QScrollArea()
            self.scroll.setWidgetResizable(True)
            self.scroll.setFrameShape(QFrame.Shape.NoFrame)
            self.body_widget = QWidget()
            self.body_widget.setObjectName("PageContent")
            self.body = QVBoxLayout(self.body_widget)
            self.body.setContentsMargins(0, 0, 4, 0)
            self.body.setSpacing(14)
            self.body.addStretch(1)
            self.scroll.setWidget(self.body_widget)
            root.addWidget(self.scroll, 1)
        else:
            self.scroll = None
            self.body_widget = QWidget()
            self.body = QVBoxLayout(self.body_widget)
            self.body.setContentsMargins(0, 0, 0, 0)
            self.body.setSpacing(14)
            self.body.addStretch(1)
            root.addWidget(self.body_widget, 1)

    # ------------------------------------------------------------------ content
    def add_widget(self, widget: QWidget, stretch: int = 0) -> QWidget:
        self.body.insertWidget(self.body.count() - 1, widget, stretch)
        return widget

    def add_layout(self, layout) -> None:
        self.body.insertLayout(self.body.count() - 1, layout)

    def add_action(self, widget: QWidget) -> QWidget:
        self.header.add_action(widget)
        return widget

    def add_action_button(self, text: str, *, slot, variant: str = "ghost", tooltip: str = ""):
        button = make_button(text, variant=variant, slot=slot, tooltip=tooltip)
        self.header.add_action(button)
        return button

    def add_separator(self) -> None:
        from winfirst.ui.widgets import Separator

        self.add_widget(Separator())

    def add_heading(self, text: str, detail: str = "") -> QLabel:
        label = QLabel(text)
        label.setObjectName("SectionLabel")
        if detail:
            label.setToolTip(detail)
        self.add_widget(label)
        return label

    # ------------------------------------------------------------------- states
    def show_error(self, message: str) -> None:
        self.error_banner.show_message(message, "critical")

    def show_warning(self, message: str) -> None:
        self.error_banner.show_message(message, "warning")

    def clear_error(self) -> None:
        self.error_banner.hide()

    def show_busy(self, message: str) -> None:
        self.busy_banner.show_message(message, "info")

    def clear_busy(self) -> None:
        self.busy_banner.hide()

    def clear_banners(self) -> None:
        self.error_banner.hide()
        self.busy_banner.hide()

    # ------------------------------------------------------------------ loading
    def ensure_loaded(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        try:
            self.load()
        except Exception as exc:
            _log.exception("Page failed to load")
            self.show_error(f"This page could not be loaded: {exc}")

    def reload(self) -> None:
        self._loaded = True
        self.clear_error()
        try:
            self.load()
        except Exception as exc:
            _log.exception("Page failed to reload")
            self.show_error(f"This page could not be refreshed: {exc}")

    def load(self) -> None:
        """Pages override this to start their initial background load."""

    # -------------------------------------------------------------------- async
    def run_async(
        self,
        work: Callable[[], Any],
        on_done: Callable[[Any], None],
        *,
        on_error: Callable[[str], None] | None = None,
        busy: str = "",
        done_message: str = "",
    ):
        if busy:
            self.show_busy(busy)
        self.ctx.status(busy or "Working…")

        def wrapped_done(result: Any) -> None:
            if busy:
                self.clear_busy()
            self.ctx.status(done_message or "Ready")
            try:
                on_done(result)
            except Exception as exc:
                _log.exception("Page result handler failed")
                self.show_error(f"The result could not be displayed: {exc}")

        def wrapped_error(message: str) -> None:
            if busy:
                self.clear_busy()
            self.ctx.status("Ready")
            if on_error is not None:
                try:
                    on_error(message)
                    return
                except Exception:
                    _log.exception("Page error handler failed")
            self.show_error(message)

        return run_async(self, work, wrapped_done, on_error=wrapped_error)

    # -------------------------------------------------------------------- misc
    def navigate(self, key: str) -> None:
        self.ctx.navigate(key)
