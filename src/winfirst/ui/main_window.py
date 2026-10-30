"""Main window: grouped navigation, lazy pages and shared status."""
from __future__ import annotations

import logging
from typing import Any

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QStackedWidget,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from winfirst import APP_NAME, __version__
from winfirst.core import paths, privilege_manager
from winfirst.ui.base_page import BasePage
from winfirst.ui.pages import PAGE_GROUPS, PAGE_TITLES, create_page
from winfirst.ui.theme import apply_theme
from winfirst.ui.widgets import Badge, make_button

_log = logging.getLogger("winfirst.ui.main")


class MainWindow(QMainWindow):
    def __init__(self, ctx: Any) -> None:
        super().__init__()
        self.ctx = ctx
        self.pages: dict[str, BasePage] = {}
        self.nav_items: dict[str, QTreeWidgetItem] = {}
        self._first_run_checked = False
        self.setWindowTitle(f"{APP_NAME} {__version__}")
        self.resize(1420, 900)
        self.setMinimumSize(1120, 700)

        central = QWidget()
        layout = QHBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        layout.addWidget(self._build_sidebar())

        content = QWidget()
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.setSpacing(0)
        content_layout.addWidget(self._build_topbar())
        self.stack = QStackedWidget()
        content_layout.addWidget(self.stack, 1)
        layout.addWidget(content, 1)
        self.setCentralWidget(central)

        self._build_statusbar()
        self.ctx.navigate = self.navigate
        self.ctx.bus.status_message.connect(self.show_status)
        self.ctx.bus.theme_changed.connect(self._theme_changed)
        self.ctx.bus.navigate_requested.connect(self.navigate)
        self.ctx.bus.history_changed.connect(self._history_changed)

        self.navigate("dashboard")
        QTimer.singleShot(0, self._show_startup_notes)
        QTimer.singleShot(120, self._maybe_first_run)

    # ---------------------------------------------------------------- sidebar
    def _build_sidebar(self) -> QWidget:
        sidebar = QFrame()
        sidebar.setObjectName("Sidebar")
        sidebar.setFixedWidth(252)
        layout = QVBoxLayout(sidebar)
        layout.setContentsMargins(0, 20, 0, 14)
        layout.setSpacing(4)

        brand = QWidget()
        brand_layout = QVBoxLayout(brand)
        brand_layout.setContentsMargins(18, 0, 16, 10)
        brand_layout.setSpacing(1)
        name = QLabel(APP_NAME)
        name.setObjectName("AppName")
        tagline = QLabel("Windows setup, verified")
        tagline.setObjectName("AppTagline")
        brand_layout.addWidget(name)
        brand_layout.addWidget(tagline)
        layout.addWidget(brand)

        self.nav = QTreeWidget()
        self.nav.setObjectName("NavTree")
        self.nav.setHeaderHidden(True)
        self.nav.setRootIsDecorated(False)
        self.nav.setIndentation(12)
        self.nav.setAnimated(True)
        self.nav.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        for group_name, keys in PAGE_GROUPS:
            group = QTreeWidgetItem([group_name.upper()])
            group.setFlags(Qt.ItemFlag.ItemIsEnabled)
            font = QFont(group.font(0))
            font.setPointSize(8)
            font.setBold(True)
            group.setFont(0, font)
            self.nav.addTopLevelItem(group)
            for key in keys:
                item = QTreeWidgetItem([PAGE_TITLES.get(key, key)])
                item.setData(0, Qt.ItemDataRole.UserRole, key)
                group.addChild(item)
                self.nav_items[key] = item
        self.nav.expandAll()
        self.nav.itemClicked.connect(self._nav_clicked)
        layout.addWidget(self.nav, 1)

        footer = QWidget()
        footer_layout = QVBoxLayout(footer)
        footer_layout.setContentsMargins(18, 8, 16, 0)
        footer_layout.setSpacing(5)
        self.mode_badge = Badge(
            "Portable data" if paths.is_portable() else "Local data",
            "info" if paths.is_portable() else "muted",
        )
        footer_layout.addWidget(self.mode_badge, 0, Qt.AlignmentFlag.AlignLeft)
        version = QLabel(f"Version {__version__} · Windows 10/11")
        version.setObjectName("SmallMuted")
        version.setWordWrap(True)
        footer_layout.addWidget(version)
        data_hint = QLabel(str(paths.data_dir()))
        data_hint.setObjectName("SmallMuted")
        data_hint.setWordWrap(True)
        data_hint.setToolTip("Data folder")
        footer_layout.addWidget(data_hint)
        layout.addWidget(footer)
        return sidebar

    # ----------------------------------------------------------------- top bar
    def _build_topbar(self) -> QWidget:
        bar = QFrame()
        bar.setObjectName("TopBar")
        bar.setFixedHeight(52)
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(18, 8, 20, 8)
        layout.setSpacing(8)

        self.section_label = QLabel("")
        self.section_label.setObjectName("SmallMuted")
        layout.addWidget(self.section_label, 1)

        elevated = self.ctx.elevated
        self.admin_badge = Badge(
            "Administrator" if elevated else "Standard user",
            "ok" if elevated else "muted",
        )
        self.admin_badge.setToolTip(privilege_manager.integrity_note())
        layout.addWidget(self.admin_badge)

        self.elevate_button = make_button(
            "Restart as administrator",
            variant="ghost",
            slot=self.request_elevation,
            tooltip="Relaunch WinFirst with administrator rights for changes that require them.",
        )
        self.elevate_button.setVisible(not elevated)
        layout.addWidget(self.elevate_button)

        self.theme_button = make_button(
            "Light mode" if self.ctx.current_theme() == "dark" else "Dark mode",
            variant="ghost",
            slot=self.toggle_theme,
        )
        layout.addWidget(self.theme_button)

        self.refresh_button = make_button(
            "Refresh", variant="ghost", slot=self.refresh_current
        )
        layout.addWidget(self.refresh_button)
        return bar

    def _build_statusbar(self) -> None:
        status = self.statusBar()
        status.setSizeGripEnabled(True)
        self.status_label = QLabel("Ready")
        status.addWidget(self.status_label, 1)
        self.history_label = QLabel("")
        self.history_label.setObjectName("SmallMuted")
        status.addPermanentWidget(self.history_label)
        self._update_history_label()

    # -------------------------------------------------------------- navigation
    def navigate(self, key: str) -> None:
        if key not in PAGE_TITLES:
            return
        page = self._ensure_page(key)
        if page is None:
            return
        self.stack.setCurrentWidget(page)
        item = self.nav_items.get(key)
        if item is not None and self.nav.currentItem() is not item:
            self.nav.blockSignals(True)
            self.nav.setCurrentItem(item)
            self.nav.scrollToItem(item)
            self.nav.blockSignals(False)
        self.section_label.setText(f"{PAGE_TITLES.get(key, key)}")
        page.ensure_loaded()
        self.show_status(f"{PAGE_TITLES.get(key, key)} ready")

    def _ensure_page(self, key: str) -> BasePage | None:
        if key in self.pages:
            return self.pages[key]
        try:
            page = create_page(key, self.ctx)
        except Exception as exc:
            _log.exception("Could not create page %s", key)
            placeholder = BasePage(self.ctx)
            placeholder.page_title = PAGE_TITLES.get(key, key)
            placeholder.page_subtitle = "This page could not be constructed."
            placeholder.show_error(f"The page could not be created: {exc}")
            page = placeholder
        if isinstance(page, BasePage):
            self.pages[key] = page
        self.stack.addWidget(page)
        return page

    def _nav_clicked(self, item: QTreeWidgetItem, _column: int) -> None:
        key = item.data(0, Qt.ItemDataRole.UserRole)
        if not key:
            return
        if self.stack.currentWidget() is self.pages.get(str(key)):
            self.refresh_current()
        else:
            self.navigate(str(key))

    def refresh_current(self) -> None:
        page = self.stack.currentWidget()
        if isinstance(page, BasePage):
            page.reload()
            self.show_status(f"{page.page_title or 'Page'} refreshed.")

    # ---------------------------------------------------------------- actions
    def toggle_theme(self) -> None:
        new = "light" if self.ctx.current_theme() == "dark" else "dark"
        self.ctx.set_theme(new)
        self.theme_button.setText("Dark mode" if new == "light" else "Light mode")

    def _theme_changed(self, name: str) -> None:
        app = QApplication.instance()
        if app is not None:
            apply_theme(app, name)
        self.theme_button.setText("Dark mode" if name == "light" else "Light mode")
        self.show_status(f"{name.title()} theme applied.")

    def request_elevation(self) -> None:
        answer = QMessageBox.question(
            self,
            "Restart as administrator?",
            "WinFirst will close and relaunch with administrator rights. The new instance asks for "
            "confirmation before any change, and every change is still previewed and verified.\n\n"
            "Unsaved text in this window will be lost. Continue?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        ok, message = self.ctx.request_elevation()
        if not ok:
            QMessageBox.warning(self, "Elevation not started", message)
            return
        self.show_status(message or "Elevated instance starting…")
        QTimer.singleShot(250, self.close)

    # ------------------------------------------------------------------ status
    def show_status(self, message: str) -> None:
        self.status_label.setText(message or "Ready")
        self._update_history_label()

    def _update_history_label(self) -> None:
        try:
            count = self.ctx.audit.count()
        except Exception:
            count = 0
        self.history_label.setText(f"{count} recorded operation(s)")

    def _history_changed(self) -> None:
        self._update_history_label()
        history = self.pages.get("history")
        if history is not None and history._loaded:
            history.reload()
        dashboard = self.pages.get("dashboard")
        if dashboard is not None and dashboard._loaded:
            dashboard.reload()

    # ------------------------------------------------------------------ startup
    def _show_startup_notes(self) -> None:
        errors = list(self.ctx.startup_errors)
        if not errors:
            return
        QMessageBox.warning(
            self,
            "Some local data could not be prepared",
            "WinFirst started, but these items need attention:\n\n" + "\n".join(f"• {error}" for error in errors),
        )

    def _maybe_first_run(self) -> None:
        if self._first_run_checked:
            return
        self._first_run_checked = True
        if getattr(self.ctx, "pending_elevation", False):
            return
        try:
            operations = self.ctx.audit.count()
        except Exception:
            return
        if operations:
            return
        box = QMessageBox(self)
        box.setWindowTitle(f"Welcome to {APP_NAME}")
        box.setIcon(QMessageBox.Icon.Information)
        box.setText(
            "WinFirst is ready. Nothing has been changed yet.\n\n"
            "Quick Setup compares a profile with this computer, shows every difference and applies "
            "only what you approve."
        )
        box.setInformativeText(
            "If you are preparing a computer for someone else, start with Quick Setup, then use the "
            "Deployment Checklist."
        )
        quick = box.addButton("Open Quick Setup", QMessageBox.ButtonRole.AcceptRole)
        dashboard = box.addButton("Stay on Dashboard", QMessageBox.ButtonRole.RejectRole)
        box.exec()
        if box.clickedButton() is quick:
            self.navigate("quick_setup")
        elif box.clickedButton() is dashboard:
            self.navigate("dashboard")

    # ------------------------------------------------------------------- close
    def closeEvent(self, event) -> None:  # noqa: N802 - Qt API
        if self.ctx.settings.get_bool("confirm_before_close", False):
            answer = QMessageBox.question(
                self,
                "Close WinFirst?",
                "Close WinFirst? Background work has finished or is not running from this window.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
        super().closeEvent(event)
