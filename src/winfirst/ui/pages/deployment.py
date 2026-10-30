"""Deployment checklist sessions with automatic and manual verification."""
from __future__ import annotations

from typing import Any

from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QMessageBox,
    QPlainTextEdit,
    QVBoxLayout,
    QWidget,
)

from winfirst.core.report_manager import build_deployment_report
from winfirst.ui.base_page import BasePage
from winfirst.ui.widgets import Badge, Card, DataTable, hint, make_button


_TASK_STATUS = {
    "passed": "Passed",
    "failed": "Failed",
    "not_applicable": "Not applicable",
    "pending": "Pending",
}


class DeploymentPage(BasePage):
    page_key = "deployment"
    page_title = "Deployment Checklist"
    page_subtitle = "Track a hand-over checklist with real automatic checks where Windows can answer."

    def __init__(self, ctx: Any, *, parent: QWidget | None = None) -> None:
        super().__init__(ctx, parent=parent)
        self.sessions: list[dict[str, Any]] = []
        self.session = None
        self._built = False

    def load(self) -> None:
        if not self._built:
            self._build()
            self._built = True
        self.refresh()

    def _build(self) -> None:
        self.add_action_button("New session…", slot=self.create_session, variant="primary")

        top = QHBoxLayout()
        top.setSpacing(14)

        sessions_card = Card(
            "Checklist sessions",
            "Each session keeps its own task states, notes and optional profile attachment.",
        )
        self.sessions_table = DataTable(
            ["Template", "Profile", "Status", "Progress", "Updated"], height=220
        )
        self.sessions_table.resize_columns([220, 170, 100, 200, 150])
        self.sessions_table.itemSelectionChanged.connect(self._session_selected)
        sessions_card.add(self.sessions_table)
        top.addWidget(sessions_card, 3)

        self.detail_card = Card("Selected session")
        self.detail_title = QLabel("Select or create a session.")
        self.detail_title.setObjectName("StrongLabel")
        self.detail_title.setWordWrap(True)
        self.detail_card.add(self.detail_title)
        self.detail_badge = Badge("No session", "muted")
        self.detail_card.body.addWidget(self.detail_badge)
        self.tasks_table = DataTable(
            ["Task", "Required", "Status", "Detail"], height=250
        )
        self.tasks_table.resize_columns([260, 80, 110, 400])
        self.detail_card.add(self.tasks_table)
        top.addWidget(self.detail_card, 4)
        self.add_layout(top)

        task_actions = QHBoxLayout()
        task_actions.setSpacing(8)
        task_actions.addWidget(make_button("Auto-check…", variant="primary", slot=self.auto_check))
        task_actions.addWidget(make_button("Mark passed", variant="ghost", slot=lambda: self._set_status("passed")))
        task_actions.addWidget(make_button("Mark failed", variant="ghost", slot=lambda: self._set_status("failed")))
        task_actions.addWidget(make_button("Not applicable", variant="ghost", slot=lambda: self._set_status("not_applicable")))
        task_actions.addWidget(make_button("Reset", variant="ghost", slot=lambda: self._set_status("pending")))
        task_actions.addStretch(1)
        self.add_layout(task_actions)

        notes_card = Card("Session notes", "Notes are stored locally with the session.")
        self.notes_edit = QPlainTextEdit()
        self.notes_edit.setMaximumHeight(110)
        self.notes_edit.setPlaceholderText("Record hand-over details, exceptions or follow-up work…")
        notes_card.add(self.notes_edit)
        notes_actions = QHBoxLayout()
        notes_actions.addWidget(make_button("Save notes", variant="ghost", slot=self.save_notes))
        notes_actions.addStretch(1)
        notes_actions.addWidget(make_button("Generate report", variant="ghost", slot=self.generate_report))
        notes_actions.addWidget(make_button("Complete session", variant="primary", slot=self.complete_session))
        notes_actions.addWidget(make_button("Delete session…", variant="danger", slot=self.delete_session))
        notes_card.add_layout(notes_actions)
        self.add_widget(notes_card)
        self.add_widget(
            hint(
                "Automatic checks read live Windows state. A manual task passes only when a person marks it. "
                "Required tasks must be passed or marked not applicable before the session can be completed."
            )
        )

    # --------------------------------------------------------------------- data
    def refresh(self) -> None:
        try:
            self.ctx.deployment.ensure_templates()
            self.sessions = self.ctx.deployment.list_sessions()
        except Exception as exc:
            self.show_error(f"Deployment sessions could not be loaded: {exc}")
            return
        self.sessions_table.set_rows(
            [
                [
                    row.get("template_name") or "",
                    row.get("profile_name") or "—",
                    str(row.get("status") or "").title(),
                    "",
                    str(row.get("updated_at") or "").replace("T", " ")[:16],
                ]
                for row in self.sessions
            ],
            payloads=self.sessions,
        )
        for index, row in enumerate(self.sessions):
            session = self.ctx.deployment.get_session(int(row["id"]))
            progress = session.progress_label if session else ""
            item = self.sessions_table.item(index, 3)
            if item is not None:
                item.setText(progress)
        if self.sessions:
            self.sessions_table.selectRow(0)
        else:
            self.session = None
            self.detail_title.setText("No sessions yet. Create one with “New session…”.")
            self.detail_badge.setText("No session")
            self.detail_badge.set_status("muted")
            self.tasks_table.set_rows([])
            self.notes_edit.setPlainText("")

    def create_session(self) -> None:
        try:
            self.ctx.deployment.ensure_templates()
            templates = self.ctx.deployment.list_templates()
        except Exception as exc:
            self.show_error(f"Checklist templates could not be loaded: {exc}")
            return
        if not templates:
            QMessageBox.warning(self, "No templates", "No checklist templates are available.")
            return
        names = [template.name for template in templates]
        name, ok = QInputDialog.getItem(
            self, "New checklist session", "Template:", names, 0, False
        )
        if not ok:
            return
        template = next((item for item in templates if item.name == name), None)
        profile_names = ["No profile attached"] + [p.name for p in self.ctx.profiles.list_profiles()]
        profile_choice, ok = QInputDialog.getItem(
            self, "Attach a profile", "Profile for automatic settings checks:", profile_names, 0, False
        )
        if not ok:
            return
        profile_name = "" if profile_choice == profile_names[0] else profile_choice
        try:
            session = self.ctx.deployment.create_session(name, profile_name)
        except Exception as exc:
            QMessageBox.critical(self, "Session not created", str(exc))
            return
        if template is not None:
            QMessageBox.information(
                self,
                "Session created",
                f"{template.name}\n\n{template.description}\n\n"
                f"{len(template.tasks)} task(s) were added to the checklist.",
            )
        self.refresh()
        self._select_session(session.id)

    def _select_session(self, session_id: int) -> None:
        for row, payload in enumerate(self.sessions):
            if int(payload.get("id", 0)) == session_id:
                self.sessions_table.selectRow(row)
                return

    def _session_selected(self) -> None:
        selected = self.sessions_table.selected_payloads()
        if not selected:
            return
        try:
            self.session = self.ctx.deployment.get_session(int(selected[0]["id"]))
        except Exception as exc:
            self.show_error(f"The session could not be loaded: {exc}")
            return
        if self.session is None:
            return
        self.detail_title.setText(
            f"{self.session.template_name} · {self.session.progress_label}"
            + (f" · profile {self.session.profile_name}" if self.session.profile_name else "")
        )
        status = self.session.status
        self.detail_badge.setText("Completed" if status == "completed" else "Active")
        self.detail_badge.set_status("ok" if status == "completed" else "info")
        self.tasks_table.set_rows(
            [
                [
                    task.title,
                    "Required" if task.required else "Optional",
                    _TASK_STATUS.get(task.status, task.status),
                    task.detail or "",
                ]
                for task in self.session.tasks
            ],
            payloads=self.session.tasks,
        )
        self.notes_edit.setPlainText(self.session.notes)
        if self.session.data.get("template") is None:
            pass

    # -------------------------------------------------------------------- tasks
    def auto_check(self) -> None:
        if self.session is None:
            return
        session_id = self.session.id
        self.run_async(
            lambda: self.ctx.deployment.auto_check_session(session_id),
            self._show_auto_check,
            busy="Running automatic checklist checks…",
        )

    def _show_auto_check(self, results) -> None:
        checked = [result for result in results if result.checked]
        passed = [result for result in checked if result.passed]
        QMessageBox.information(
            self,
            "Automatic checks finished",
            f"{len(checked)} task(s) could be checked automatically; {len(passed)} passed.\n\n"
            "Tasks without an automatic check remain unchanged and require review.",
        )
        if self.session is not None:
            self._reload_current()

    def _set_status(self, status: str) -> None:
        if self.session is None:
            return
        selected = self.tasks_table.selected_payloads()
        if not selected:
            QMessageBox.information(self, "No tasks selected", "Select one or more checklist tasks.")
            return
        detail = ""
        if status == "failed":
            detail, ok = QInputDialog.getText(
                self, "Failure detail", "Describe what failed (optional):"
            )
            if not ok:
                return
        try:
            for task in selected:
                self.ctx.deployment.update_task(self.session.id, task.key, status, detail)
        except Exception as exc:
            QMessageBox.critical(self, "Task not updated", str(exc))
            return
        self._reload_current()

    def _reload_current(self) -> None:
        if self.session is None:
            return
        session_id = self.session.id
        self.ctx.deployment.get_session(session_id)
        self.session = self.ctx.deployment.get_session(session_id)
        if self.session is not None:
            self.detail_title.setText(
                f"{self.session.template_name} · {self.session.progress_label}"
            )
            self.tasks_table.set_rows(
                [
                    [
                        task.title,
                        "Required" if task.required else "Optional",
                        _TASK_STATUS.get(task.status, task.status),
                        task.detail or "",
                    ]
                    for task in self.session.tasks
                ],
                payloads=self.session.tasks,
            )

    # ------------------------------------------------------------------- finish
    def save_notes(self) -> None:
        if self.session is None:
            return
        try:
            self.ctx.deployment.update_notes(self.session.id, self.notes_edit.toPlainText())
        except Exception as exc:
            self.show_error(f"Notes could not be saved: {exc}")
            return
        self.ctx.status("Session notes saved.")

    def complete_session(self) -> None:
        if self.session is None:
            return
        try:
            session = self.ctx.deployment.complete_session(self.session.id)
        except ValueError as exc:
            answer = QMessageBox.question(
                self,
                "Required tasks are incomplete",
                f"{exc}\n\nComplete the session anyway? This is recorded as a forced completion.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
            try:
                session = self.ctx.deployment.complete_session(self.session.id, force=True)
            except Exception as inner:
                QMessageBox.critical(self, "Session not completed", str(inner))
                return
        except Exception as exc:
            QMessageBox.critical(self, "Session not completed", str(exc))
            return
        self.session = session
        self.refresh()
        self._select_session(session.id)

    def delete_session(self) -> None:
        if self.session is None:
            return
        answer = QMessageBox.question(
            self,
            "Delete checklist session?",
            f"Permanently delete the session “{self.session.template_name}” and its task history?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            self.ctx.deployment.delete_session(self.session.id)
        except Exception as exc:
            QMessageBox.critical(self, "Session not deleted", str(exc))
            return
        self.session = None
        self.refresh()

    def generate_report(self) -> None:
        if self.session is None:
            return
        session = self.session

        def work():
            return self.ctx.reports.save(build_deployment_report(session), "html")

        def done(path) -> None:
            QMessageBox.information(self, "Report generated", f"Saved to:\n{path}")

        self.run_async(
            work,
            done,
            busy="Saving the deployment report…",
            done_message="Deployment report saved.",
        )
