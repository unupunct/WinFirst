"""Shared application context.

The context owns the database, services and controllers used by every page.
Pages receive it instead of constructing services themselves, which keeps one
audit history, one settings store and one operation controller for the whole
application.
"""
from __future__ import annotations

import logging
import os
import sys
from typing import Callable

from PySide6.QtCore import QObject, Signal

from winfirst import APP_NAME, __version__
from winfirst.core.app_settings import AppSettings
from winfirst.core.audit_logger import AuditLogger
from winfirst.core.capability_detection import CapabilitySet, detect_capabilities
from winfirst.core.command_runner import CommandRunner
from winfirst.core.database import Database
from winfirst.core.diff_engine import DiffEngine
from winfirst.core.os_detection import WindowsInfo, detect_windows_info
from winfirst.core.operation import OperationController
from winfirst.core.paths import describe_locations
from winfirst.core.privilege_manager import is_elevated, relaunch_elevated
from winfirst.core.profile_manager import ProfileManager
from winfirst.core.report_manager import ReportManager
from winfirst.core.rollback_manager import RollbackManager
from winfirst.services.deployment_service import DeploymentService
from winfirst.services.repository_service import RepositoryService
from winfirst.services.restore_point_service import RestorePointService

_log = logging.getLogger("winfirst.ui.context")


class UiBus(QObject):
    history_changed = Signal()
    status_message = Signal(str)
    theme_changed = Signal(str)
    navigate_requested = Signal(str)


class AppContext:
    def __init__(self, *, database: Database | None = None) -> None:
        self.db = database or Database()
        self.settings = AppSettings(self.db)
        self.bus = UiBus()
        self.os_info: WindowsInfo = detect_windows_info()
        self.audit = AuditLogger(self.db, self._machine_info)
        self.runner = CommandRunner()
        self.controller = OperationController(self.audit)
        self.restore_points = RestorePointService(self.runner)
        self.profiles = ProfileManager(self.db)
        self.reports = ReportManager(self.db)
        self.rollback = RollbackManager(self.audit, self.controller)
        self.diff = DiffEngine(self.db)
        self.repository = RepositoryService(self.db)
        self.deployment = DeploymentService(self.db, self.profiles)
        self._capabilities: CapabilitySet | None = None
        self.navigate: Callable[[str], None] = lambda _key: None
        self._startup_errors: list[str] = []
        self._prepare_data()

    # ------------------------------------------------------------------ setup
    def _machine_info(self) -> dict[str, str]:
        info = self.os_info
        return {
            "hostname": info.hostname or os.environ.get("COMPUTERNAME", ""),
            "windows_version": info.version_label if info else "",
            "username": info.username if info else os.environ.get("USERNAME", ""),
        }

    def _prepare_data(self) -> None:
        try:
            self.profiles.ensure_builtin_profiles()
        except Exception as exc:
            _log.error("Could not import built-in profiles: %s", exc)
            self._startup_errors.append(f"Built-in profiles could not be imported: {exc}")
        try:
            self.deployment.ensure_templates()
        except Exception as exc:
            _log.error("Could not import checklist templates: %s", exc)
            self._startup_errors.append(f"Checklist templates could not be imported: {exc}")

    # ---------------------------------------------------------------- queries
    @property
    def elevated(self) -> bool:
        return is_elevated()

    @property
    def startup_errors(self) -> list[str]:
        return list(self._startup_errors)

    @property
    def app_title(self) -> str:
        return f"{APP_NAME} {__version__}"

    def locations(self) -> dict[str, str]:
        try:
            return describe_locations()
        except Exception as exc:
            return {"data": str(exc)}

    def capabilities(self, *, force: bool = False) -> CapabilitySet:
        if self._capabilities is None or force:
            self._capabilities = detect_capabilities(
                self.runner,
                is_elevated=self.elevated,
                os_build=self.os_info.build,
            )
        return self._capabilities

    # -------------------------------------------------------------- elevation
    def request_elevation(self) -> tuple[bool, str]:
        try:
            return relaunch_elevated()
        except Exception as exc:
            return False, str(exc)

    def status(self, message: str) -> None:
        self.bus.status_message.emit(message)

    def notify_history_changed(self) -> None:
        self.bus.history_changed.emit()

    # ------------------------------------------------------------------ theme
    def current_theme(self) -> str:
        name = self.settings.get("theme", "dark").strip().lower()
        return name if name in {"dark", "light"} else "dark"

    def set_theme(self, name: str) -> None:
        cleaned = name.strip().lower()
        if cleaned not in {"dark", "light"}:
            cleaned = "dark"
        self.settings.set("theme", cleaned)
        self.bus.theme_changed.emit(cleaned)

    # ------------------------------------------------------------------- misc
    def is_windows(self) -> bool:
        return sys.platform == "win32"
