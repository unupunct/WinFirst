"""Windows services and scheduled tasks with guarded changes."""
from __future__ import annotations

from typing import Any

from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLineEdit,
    QMessageBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from winfirst.services import scheduled_task_service, service_audit_service
from winfirst.ui.base_page import BasePage
from winfirst.ui.dialogs import run_operations
from winfirst.ui.widgets import Badge, Card, DataTable, hint, make_button


class ServicesPage(BasePage):
    page_key = "services"
    page_title = "Services & Tasks"
    page_subtitle = "Startup modes and scheduled tasks, with protected Windows components excluded."

    def __init__(self, ctx: Any, *, parent: QWidget | None = None) -> None:
        super().__init__(ctx, parent=parent)
        self.services = []
        self.service_snapshot = None
        self.tasks = []
        self.task_snapshot = None
        self._built = False

    def load(self) -> None:
        if not self._built:
            self._build()
            self._built = True
        self.refresh()

    def _build(self) -> None:
        self.add_action_button("Refresh", slot=self.refresh, variant="ghost")
        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        self.add_widget(self.tabs)

        services = QWidget()
        services_layout = QVBoxLayout(services)
        services_layout.setContentsMargins(0, 12, 0, 0)
        services_layout.setSpacing(9)
        service_toolbar = QHBoxLayout()
        service_toolbar.setSpacing(8)
        self.service_search = QLineEdit()
        self.service_search.setPlaceholderText("Filter services by name, display name or account…")
        self.service_search.setClearButtonEnabled(True)
        self.service_search.textChanged.connect(self._filter_services)
        service_toolbar.addWidget(self.service_search, 2)
        self.service_badge = Badge("Services: —", "muted")
        service_toolbar.addWidget(self.service_badge)
        service_toolbar.addWidget(make_button("Load services", variant="ghost", slot=self.load_services))
        services_layout.addLayout(service_toolbar)
        self.services_table = DataTable(
            ["Name", "Display name", "State", "Startup", "Account", "Notes"], height=320
        )
        self.services_table.resize_columns([160, 280, 100, 110, 190, 230])
        services_layout.addWidget(self.services_table)
        service_actions = QHBoxLayout()
        service_actions.setSpacing(8)
        self.startup_mode = QComboBox()
        self.startup_mode.addItem("Automatic", "auto")
        self.startup_mode.addItem("Manual", "demand")
        self.startup_mode.addItem("Disabled", "disabled")
        service_actions.addWidget(self.startup_mode)
        service_actions.addWidget(
            make_button("Set startup mode…", variant="danger", slot=self.set_startup_mode)
        )
        service_actions.addWidget(make_button("Start selected…", variant="ghost", slot=self.start_selected))
        service_actions.addWidget(make_button("Stop selected…", variant="ghost", slot=self.stop_selected))
        service_actions.addStretch(1)
        self.service_selection = hint("")
        service_actions.addWidget(self.service_selection)
        services_layout.addLayout(service_actions)
        services_layout.addWidget(
            hint(
                "WinFirst never mass-disables services. Defender, Firewall, Update, RPC and other critical "
                "services are protected and rejected even if selected."
            )
        )
        self.services_table.itemSelectionChanged.connect(self._service_selection_changed)
        self.tabs.addTab(services, "Services")

        tasks = QWidget()
        tasks_layout = QVBoxLayout(tasks)
        tasks_layout.setContentsMargins(0, 12, 0, 0)
        tasks_layout.setSpacing(9)
        task_toolbar = QHBoxLayout()
        task_toolbar.setSpacing(8)
        self.task_search = QLineEdit()
        self.task_search.setPlaceholderText("Filter tasks by name, path or author…")
        self.task_search.setClearButtonEnabled(True)
        self.task_search.textChanged.connect(self._filter_tasks)
        task_toolbar.addWidget(self.task_search, 2)
        self.task_badge = Badge("Tasks: —", "muted")
        task_toolbar.addWidget(self.task_badge)
        task_toolbar.addWidget(make_button("Load tasks", variant="ghost", slot=self.load_tasks))
        tasks_layout.addLayout(task_toolbar)
        self.tasks_table = DataTable(
            ["Task", "Path", "State", "Author", "Trigger", "Action"], height=360
        )
        self.tasks_table.resize_columns([220, 230, 90, 160, 180, 260])
        tasks_layout.addWidget(self.tasks_table)
        task_actions = QHBoxLayout()
        task_actions.setSpacing(8)
        task_actions.addWidget(make_button("Disable selected…", variant="danger", slot=self.disable_tasks))
        task_actions.addWidget(make_button("Enable selected…", variant="ghost", slot=self.enable_tasks))
        task_actions.addStretch(1)
        self.task_selection = hint("")
        task_actions.addWidget(self.task_selection)
        tasks_layout.addLayout(task_actions)
        tasks_layout.addWidget(
            hint("Protected Microsoft maintenance tasks are excluded from changes.")
        )
        self.tasks_table.itemSelectionChanged.connect(self._task_selection_changed)
        self.tabs.addTab(tasks, "Scheduled tasks")

    # --------------------------------------------------------------------- load
    def refresh(self) -> None:
        self.clear_error()
        self.load_services()
        self.load_tasks()

    def load_services(self) -> None:
        self.service_badge.setText("Loading…")
        self.run_async(
            lambda: service_audit_service.get_services(self.ctx.runner),
            self._show_services,
            busy="Reading Windows services…",
        )

    def _show_services(self, snapshot) -> None:
        self.service_snapshot = snapshot
        self.services = list(snapshot.services)
        self.service_badge.setText(f"Services: {len(self.services)}")
        self.service_badge.set_status("info")
        if snapshot.errors:
            self.show_error("Some services could not be read: " + "; ".join(snapshot.errors[:3]))
        self._filter_services()

    def load_tasks(self) -> None:
        self.task_badge.setText("Loading…")
        self.run_async(
            lambda: scheduled_task_service.get_scheduled_tasks(self.ctx.runner),
            self._show_tasks,
            busy="Reading scheduled tasks…",
        )

    def _show_tasks(self, snapshot) -> None:
        self.task_snapshot = snapshot
        self.tasks = list(snapshot.tasks)
        self.task_badge.setText(f"Tasks: {len(self.tasks)}")
        self.task_badge.set_status("info")
        if snapshot.errors:
            self.show_error("Some scheduled tasks could not be read: " + "; ".join(snapshot.errors[:3]))
        self._filter_tasks()

    def _filter_services(self) -> None:
        needle = self.service_search.text().strip().casefold()
        rows = []
        payloads = []
        for service in self.services:
            haystack = f"{service.name} {service.display_name} {service.account}".casefold()
            if needle and needle not in haystack:
                continue
            notes = service.review_note or (service.protection_reason if service.protected else "")
            rows.append(
                [
                    service.name,
                    service.display_name,
                    service.state,
                    service.start_mode_label,
                    service.account or "—",
                    notes,
                ]
            )
            payloads.append(service)
        self.services_table.set_rows(rows, payloads=payloads)
        self._service_selection_changed()

    def _filter_tasks(self) -> None:
        needle = self.task_search.text().strip().casefold()
        rows = []
        payloads = []
        for task in self.tasks:
            haystack = f"{task.name} {task.path} {task.author}".casefold()
            if needle and needle not in haystack:
                continue
            rows.append(
                [
                    task.name,
                    task.path,
                    task.state,
                    task.author or "—",
                    task.trigger_summary,
                    task.action_summary,
                ]
            )
            payloads.append(task)
        self.tasks_table.set_rows(rows, payloads=payloads)
        self._task_selection_changed()

    # ---------------------------------------------------------------- selection
    def _service_selection_changed(self) -> None:
        selected = self.services_table.selected_payloads()
        protected = sum(1 for item in selected if item.protected)
        text = f"{len(selected)} selected"
        if protected:
            text += f" · {protected} protected"
        self.service_selection.setText(text if selected else "Select services.")
        self.startup_mode.setEnabled(bool(selected))

    def _task_selection_changed(self) -> None:
        selected = self.tasks_table.selected_payloads()
        protected = sum(1 for item in selected if item.protected)
        text = f"{len(selected)} selected"
        if protected:
            text += f" · {protected} protected"
        self.task_selection.setText(text if selected else "Select tasks.")

    # ----------------------------------------------------------------- services
    def set_startup_mode(self) -> None:
        selected = self.services_table.selected_payloads()
        if not selected:
            return
        mode = self.startup_mode.currentData()
        try:
            service_audit_service.validate_startup_mode_batch(selected, mode)
        except ValueError as exc:
            QMessageBox.warning(self, "Batch change refused", str(exc))
            return
        specs = []
        errors = []
        for service in selected:
            try:
                specs.append(service_audit_service.build_startup_mode_spec(service, mode))
            except Exception as exc:
                errors.append(f"{service.name}: {exc}")
        self._run_service_specs(specs, errors, "Change service startup mode")

    def start_selected(self) -> None:
        self._start_stop(True)

    def stop_selected(self) -> None:
        self._start_stop(False)

    def _start_stop(self, start: bool) -> None:
        selected = self.services_table.selected_payloads()
        if not selected:
            return
        specs = []
        errors = []
        for service in selected:
            try:
                specs.append(service_audit_service.build_start_stop_spec(service, start))
            except Exception as exc:
                errors.append(f"{service.name}: {exc}")
        self._run_service_specs(specs, errors, "Start services" if start else "Stop services")

    def _run_service_specs(self, specs, errors: list[str], title: str) -> None:
        if errors:
            QMessageBox.warning(self, "Some services were skipped", "\n".join(errors[:20]))
        if not specs:
            return
        results = run_operations(
            self.ctx,
            self,
            specs,
            title=title,
            intro="Each service change is read back. Protected services are never included.",
        )
        if results:
            self.load_services()

    # -------------------------------------------------------------------- tasks
    def disable_tasks(self) -> None:
        self._toggle_tasks(False)

    def enable_tasks(self) -> None:
        self._toggle_tasks(True)

    def _toggle_tasks(self, enable: bool) -> None:
        selected = self.tasks_table.selected_payloads()
        if not selected:
            return
        specs = []
        errors = []
        for task in selected:
            try:
                specs.append(scheduled_task_service.build_toggle_spec(task, enable))
            except Exception as exc:
                errors.append(f"{task.full_name}: {exc}")
        if errors:
            QMessageBox.warning(self, "Some tasks were skipped", "\n".join(errors[:20]))
        if not specs:
            return
        results = run_operations(
            self.ctx,
            self,
            specs,
            title="Enable scheduled tasks" if enable else "Disable scheduled tasks",
            intro="Each task state is read back after the change.",
        )
        if results:
            self.load_tasks()
