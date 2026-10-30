"""Operation lifecycle dialogs.

The same preview -> confirm -> progress -> result flow is used by every page.
Nothing is applied until the preview is accepted, and the result dialog always
shows the independently verified state rather than the command exit code alone.
"""
from __future__ import annotations

import html
import os
import platform
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Sequence

from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QTableWidget,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from winfirst.core.operation import OperationSpec
from winfirst.core.operation_result import OperationResult, OperationStatus, utc_now
from winfirst.core.paths import logs_dir
from winfirst.core.plan import build_document, build_plan
from winfirst.ui.widgets import Banner, DataTable, make_button, status_color


def _open_folder(path: str) -> None:
    try:
        if os.path.isdir(path):
            os.startfile(path)  # type: ignore[attr-defined]
    except Exception:
        pass


class SpecPreviewDialog(QDialog):
    """Show exactly what will change, with explicit confirmation."""

    def __init__(
        self,
        specs: Sequence[OperationSpec],
        *,
        title: str = "Confirm operation",
        intro: str = "",
        elevated: bool = True,
        restore_prompt: bool = False,
        restore_default: bool = True,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.specs = list(specs)
        self.setWindowTitle(title)
        self.resize(780, 640)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 16)
        layout.setSpacing(10)

        heading = QLabel(title)
        heading.setObjectName("PageTitle")
        layout.addWidget(heading)
        if intro:
            label = QLabel(intro)
            label.setWordWrap(True)
            label.setObjectName("SmallMuted")
            layout.addWidget(label)

        needs_admin = [spec for spec in self.specs if spec.requires_admin]
        if needs_admin and not elevated:
            banner = Banner(
                "One or more operations require administrator rights. "
                "Restart WinFirst as administrator, then run the operation again.",
                "warning",
            )
            layout.addWidget(banner)

        self.summary = QLabel(self._summary_text())
        self.summary.setWordWrap(True)
        layout.addWidget(self.summary)

        self.browser = QTextBrowser()
        self.browser.setHtml(self._html())
        layout.addWidget(self.browser, 1)

        self._restore_prompt = restore_prompt
        self.restore_check = QCheckBox(
            "Create a system restore point before applying (recommended)"
        )
        self.restore_check.setChecked(bool(restore_default))
        self.restore_check.setVisible(restore_prompt)
        self.restore_check.setToolTip(
            "A restore point created recently is reused; WinFirst never changes the "
            "Windows restore-point frequency."
        )
        layout.addWidget(self.restore_check)

        plan_row = QHBoxLayout()
        plan_row.addWidget(make_button("Save plan...", variant="ghost", slot=self._save_plan))
        plan_row.addStretch(1)
        layout.addLayout(plan_row)

        self.confirm_check = QCheckBox(
            "I reviewed the changes, warnings and verification method above."
        )
        self.confirm_check.setVisible(self._requires_ack())
        layout.addWidget(self.confirm_check)

        typed_row = QHBoxLayout()
        typed_row.setSpacing(8)
        self.typed_label = QLabel('Type CONFIRM to continue:')
        self.typed_input = QLineEdit()
        self.typed_input.setPlaceholderText("CONFIRM")
        self.typed_input.setMaximumWidth(180)
        self.typed_label.setVisible(self._requires_typed())
        self.typed_input.setVisible(self._requires_typed())
        typed_row.addWidget(self.typed_label)
        typed_row.addWidget(self.typed_input)
        typed_row.addStretch(1)
        layout.addLayout(typed_row)

        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Cancel | QDialogButtonBox.StandardButton.Ok
        )
        self.apply_button = self.buttons.button(QDialogButtonBox.StandardButton.Ok)
        self.apply_button.setText("Apply changes")
        self.apply_button.setProperty("variant", "primary")
        self.buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("Cancel")
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)

        self.confirm_check.toggled.connect(self._update_enabled)
        self.typed_input.textChanged.connect(self._update_enabled)
        self._admin_blocked = bool(needs_admin) and not elevated
        self._update_enabled()

    # ------------------------------------------------------------------ state
    def _requires_ack(self) -> bool:
        return any(spec.risk.rank >= 2 for spec in self.specs)

    def _requires_typed(self) -> bool:
        return any(spec.requires_typed_confirmation for spec in self.specs)

    def _update_enabled(self) -> None:
        enabled = not self._admin_blocked
        if self._requires_ack() and not self.confirm_check.isChecked():
            enabled = False
        if self._requires_typed() and self.typed_input.text().strip() != "CONFIRM":
            enabled = False
        self.apply_button.setEnabled(enabled)

    @property
    def restore_requested(self) -> bool:
        return bool(self._restore_prompt and self.restore_check.isChecked())

    # ------------------------------------------------------------------- plan
    def _plan_document(self):
        plans = []
        for spec in self.specs:
            try:
                steps = spec.plan_steps() if spec.plan_steps is not None else []
            except Exception:
                steps = []
            plans.append(
                build_plan(
                    spec,
                    steps=steps,
                    notes=[
                        "Support and the current state are checked again when the operation runs; "
                        "this export is for review and approval."
                    ],
                )
            )
        return build_document(
            plans,
            title=f"{self.windowTitle()} - change plan",
            host=platform.node(),
        )

    def _save_plan(self) -> None:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        path, _selected = QFileDialog.getSaveFileName(
            self,
            "Save change plan",
            f"winfirst-plan-{stamp}.md",
            "Markdown (*.md);;JSON (*.json)",
        )
        if not path:
            return
        try:
            document = self._plan_document()
            text = document.to_json() if path.lower().endswith(".json") else document.to_markdown()
            Path(path).write_text(text, encoding="utf-8")
        except Exception as exc:
            QMessageBox.critical(self, "Plan not saved", f"The plan could not be written:\n\n{exc}")
            return
        QMessageBox.information(
            self,
            "Plan saved",
            f"The change plan was written to:\n{path}\n\nNo changes were applied.",
        )

    def _summary_text(self) -> str:
        if not self.specs:
            return "No operations were selected."
        risks = [spec.risk for spec in self.specs]
        highest = max(risks, key=lambda item: item.rank)
        admin = any(spec.requires_admin for spec in self.specs)
        restart = any(spec.restart_required for spec in self.specs)
        bits = [f"{len(self.specs)} operation(s)"]
        bits.append(f"highest risk: {highest.label}")
        if admin:
            bits.append("administrator required")
        if restart:
            bits.append("restart may be required")
        if any(spec.reversible for spec in self.specs):
            bits.append("some changes are reversible")
        return " | ".join(bits)

    def _html(self) -> str:
        parts = [
            "<style>"
            "body{font-family:'Segoe UI',Arial,sans-serif;font-size:10pt;color:#e8eef5;}"
            "h3{margin:14px 0 6px;font-size:11pt;} .muted{color:#9aacbd;}"
            ".spec{border:1px solid #2a3b4c;border-radius:6px;padding:9px;margin:8px 0;}"
            ".warn{color:#e3b341;} ul{margin:4px 0 4px 18px;padding:0;}"
            "</style>"
        ]
        for index, spec in enumerate(self.specs, start=1):
            parts.append('<div class="spec">')
            parts.append(
                f"<h3>{index}. {html.escape(spec.name)}</h3>"
                f'<div class="muted">{html.escape(spec.category)} &middot; '
                f"{html.escape(spec.risk.label)} risk"
                + (" &middot; administrator required" if spec.requires_admin else "")
                + (" &middot; reversible" if spec.reversible else "")
                + "</div>"
            )
            if spec.description:
                parts.append(f"<p>{html.escape(spec.description)}</p>")
            if spec.preview_text:
                parts.append(f"<p><b>Planned:</b> {html.escape(spec.preview_text)}</p>")
            steps_html = self._steps_html(spec)
            if steps_html:
                parts.append(steps_html)
            if spec.verification_method:
                parts.append(
                    f'<p><b>Verification:</b> {html.escape(spec.verification_method)}</p>'
                )
            if spec.warnings:
                parts.append('<p class="warn"><b>Warnings</b></p><ul>')
                for warning in spec.warnings:
                    parts.append(f'<li class="warn">{html.escape(str(warning))}</li>')
                parts.append("</ul>")
            parts.append("</div>")
        if not self.specs:
            parts.append("<p>No operations were selected.</p>")
        return "".join(parts)

    def _steps_html(self, spec: OperationSpec) -> str:
        if spec.plan_steps is None:
            return ""
        try:
            steps = spec.plan_steps()
        except Exception as exc:
            return f'<p class="muted">Exact steps unavailable: {html.escape(str(exc))}</p>'
        if not steps:
            return ""
        rows = ["<p><b>Exact steps</b></p><ul>"]
        for step in steps:
            if isinstance(step, str):
                rows.append(f"<li>{html.escape(step)}</li>")
                continue
            text = html.escape(step.title)
            if step.kind == "command" and step.argv:
                text += f": <code>{html.escape(step.command)}</code>"
            if step.detail:
                text += f'<br><span class="muted">{html.escape(step.detail)}</span>'
            if step.destructive:
                text += ' <span class="warn">(permanent)</span>'
            rows.append(f"<li>{text}</li>")
        rows.append("</ul>")
        return "".join(rows)


class OperationProgressDialog(QDialog):
    """Run a batch in a worker thread with a cancel switch."""

    def __init__(self, *, title: str = "Applying changes", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(760, 470)
        self.setModal(True)
        self.results: list[OperationResult] = []
        self.error = ""
        self._running = False
        self.cancel_event = threading.Event()
        self._worker = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 14)
        layout.setSpacing(10)
        self.heading = QLabel(title)
        self.heading.setObjectName("CardTitle")
        layout.addWidget(self.heading)
        self.status = QLabel("Startingâ€¦")
        self.status.setObjectName("SmallMuted")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)

        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        layout.addWidget(self.progress)

        self.log = QPlainTextEdit()
        self.log.setObjectName("LogView")
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(4000)
        layout.addWidget(self.log, 1)

        row = QHBoxLayout()
        row.addStretch(1)
        self.cancel_button = make_button("Cancel", variant="danger", slot=self.cancel)
        row.addWidget(self.cancel_button)
        layout.addLayout(row)

    # ------------------------------------------------------------------- run
    def start(self, worker_fn) -> None:
        from winfirst.ui.workers import Worker

        self._running = True
        # The worker runs off the GUI thread.  It must not touch widgets
        # directly, so hand it a signal emitter instead of _on_progress.  Qt
        # delivers the signal back to this dialog's thread.
        self._worker = Worker(lambda: worker_fn(self._worker.signals.progress.emit))
        self._worker.signals.progress.connect(self._on_progress)
        self._worker.signals.finished.connect(self._on_finished)
        self._worker.signals.failed.connect(self._on_failed)
        from winfirst.ui.workers import pool

        pool().start(self._worker)

    def cancel(self) -> None:
        if not self._running:
            return
        self.cancel_event.set()
        self.cancel_button.setEnabled(False)
        self.cancel_button.setText("Cancellingâ€¦")
        self.status.setText(
            "Cancellation requested. A command already in progress may take a moment to stop; "
            "the final state is verified before the result is shown."
        )

    def _on_progress(self, text: str) -> None:
        self.status.setText(text)
        self.log.appendPlainText(text)
        bar = self.log.verticalScrollBar()
        bar.setValue(bar.maximum())

    def _on_finished(self, results: Any) -> None:
        self._running = False
        self.results = list(results or [])
        self.accept()

    def _on_failed(self, message: str) -> None:
        self._running = False
        self.error = message
        self.reject()

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt API
        if self._running:
            answer = QMessageBox.question(
                self,
                "Cancel the running operation?",
                "The operation is still running. Cancel it and wait for verification?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer == QMessageBox.StandardButton.Yes:
                self.cancel()
            event.ignore()
            return
        super().closeEvent(event)


class OperationResultDialog(QDialog):
    """Verified outcome of one or more operations."""

    def __init__(self, results: Sequence[OperationResult], *, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.results = list(results)
        self.setWindowTitle("Operation results")
        self.resize(900, 620)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 14)
        layout.setSpacing(10)

        heading = QLabel("Operation results")
        heading.setObjectName("PageTitle")
        layout.addWidget(heading)

        successes = sum(1 for item in self.results if item.status.is_success)
        failures = len([item for item in self.results if item.status.is_failure])
        summary = QLabel(
            f"{successes} succeeded, {failures} need attention, {len(self.results)} total. "
            "Success means the operation completed and any available read-back verification passed. "
            "Operations without an independent read-back say so in their details."
        )
        summary.setWordWrap(True)
        summary.setObjectName("SmallMuted")
        layout.addWidget(summary)

        self.table = DataTable(["Operation", "Status", "Verified state", "Notes"], height=230)
        self.table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        layout.addWidget(self.table, 1)

        self.details = QTextBrowser()
        self.details.setMinimumHeight(180)
        layout.addWidget(self.details, 1)

        row = QHBoxLayout()
        row.addWidget(make_button("Copy summary", variant="ghost", slot=self._copy_summary))
        row.addWidget(make_button("Open log folder", variant="ghost", slot=lambda: _open_folder(str(logs_dir()))))
        row.addStretch(1)
        close = make_button("Close", variant="primary", slot=self.accept)
        row.addWidget(close)
        layout.addLayout(row)

        self._populate()
        self.table.itemSelectionChanged.connect(self._show_selected)
        if self.results:
            self.table.selectRow(0)
            self._show_selected()

    def _populate(self) -> None:
        rows = []
        for result in self.results:
            notes = result.error or "; ".join(result.warnings[:2])
            rows.append([result.name, result.status.label, result.actual_state or "â€”", notes])
        self.table.set_rows(rows, payloads=self.results)
        for index, result in enumerate(self.results):
            item = self.table.item(index, 1)
            if item is not None:
                item.setForeground(status_color(result.status.value))

    def _show_selected(self) -> None:
        selected = self.table.selected_row()
        if selected < 0 or selected >= len(self.results):
            self.details.clear()
            return
        result = self.results[selected]
        parts = [
            f"<h3>{html.escape(result.name)}</h3>",
            f"<p><b>Status:</b> {html.escape(result.status.label)}<br>",
            f"<b>Operation:</b> {html.escape(result.operation_id)}<br>",
            f"<b>Risk:</b> {html.escape(result.risk)} &middot; "
            f"<b>Elevated:</b> {'yes' if result.elevated else 'no'}<br>",
            f"<b>Started:</b> {html.escape(result.started_at)} &middot; "
            f"<b>Duration:</b> {result.duration_ms} ms</p>",
        ]
        if result.previous_state:
            parts.append(f"<p><b>Previous:</b> {html.escape(result.previous_state)}</p>")
        if result.requested_state:
            parts.append(f"<p><b>Requested:</b> {html.escape(result.requested_state)}</p>")
        if result.actual_state:
            parts.append(f"<p><b>Verified state:</b> {html.escape(result.actual_state)}</p>")
        if result.verification:
            parts.append(f"<p><b>Verification:</b> {html.escape(result.verification)}</p>")
        if result.error:
            parts.append(f"<p style='color:#ef6b6b'><b>Error:</b> {html.escape(result.error)}</p>")
        if result.warnings:
            parts.append("<p><b>Warnings</b></p><ul>")
            for warning in result.warnings:
                parts.append(f"<li>{html.escape(str(warning))}</li>")
            parts.append("</ul>")
        if result.restart_required:
            parts.append("<p><b>A restart is required before the change is fully active.</b></p>")
        if result.rollback_available:
            parts.append("<p>Rollback is available from History &amp; Rollback.</p>")
        details = dict(result.details or {})
        if details:
            parts.append("<p><b>Details</b></p><table>")
            for key, value in list(details.items())[:30]:
                parts.append(
                    f"<tr><td>{html.escape(str(key))}</td><td>{html.escape(str(value))[:1500]}</td></tr>"
                )
            parts.append("</table>")
        if result.stdout:
            parts.append("<p><b>Output tail</b></p><pre>")
            parts.append(html.escape(result.stdout[-6000:]))
            parts.append("</pre>")
        if result.stderr:
            parts.append("<p><b>Error output tail</b></p><pre>")
            parts.append(html.escape(result.stderr[-4000:]))
            parts.append("</pre>")
        self.details.setHtml("".join(parts))

    def _copy_summary(self) -> None:
        from PySide6.QtWidgets import QApplication

        lines = [result.summary() for result in self.results]
        QApplication.clipboard().setText("\n".join(lines))


def run_operations(
    ctx: Any,
    parent: QWidget | None,
    specs: Sequence[OperationSpec],
    *,
    title: str = "Confirm operation",
    profile_name: str = "",
    intro: str = "",
    show_results: bool = True,
    offer_restore_point: bool = True,
) -> list[OperationResult]:
    """Preview, confirm, apply, verify and display a batch of operations."""
    specs = [spec for spec in specs if spec is not None]
    if not specs:
        QMessageBox.information(parent, "Nothing to apply", "No applicable operations were selected.")
        return []

    restore_supported = False
    if offer_restore_point:
        try:
            restore_supported = ctx.restore_points.support().can_create
        except Exception:
            restore_supported = False

    preview = SpecPreviewDialog(
        specs,
        title=title,
        intro=intro,
        elevated=bool(ctx.elevated),
        restore_prompt=restore_supported,
        restore_default=ctx.settings.get_bool("restore_point_before_apply", True),
        parent=parent,
    )
    if preview.exec() != QDialog.DialogCode.Accepted:
        return []
    restore_requested = preview.restore_requested

    progress = OperationProgressDialog(title=title, parent=parent)
    cancel_event = progress.cancel_event

    def work(progress_callback, event):
        results: list[OperationResult] = []
        if restore_requested:
            description = f"WinFirst: {title}"[:200]
            try:
                restore_spec = ctx.restore_points.build_spec(description)
            except Exception as exc:
                results.append(
                    OperationResult(
                        operation_id="safety.restore_point",
                        name="Create a system restore point",
                        status=OperationStatus.FAILED,
                        error=f"The restore point could not be prepared: {exc}",
                        finished_at=utc_now(),
                    )
                )
                return results
            restored = ctx.controller.execute(
                restore_spec,
                progress=progress_callback,
                cancel_event=event,
                profile_name=profile_name,
            )
            results.append(restored)
            if not restored.ok:
                progress_callback(
                    "The restore point could not be confirmed; the requested changes were not applied."
                )
                return results
        results.extend(
            ctx.controller.execute_many(
                specs,
                progress=progress_callback,
                cancel_event=event,
                profile_name=profile_name,
            )
        )
        return results

    progress.start(lambda emit: work(emit, cancel_event))
    if progress.exec() != QDialog.DialogCode.Accepted:
        if progress.error:
            QMessageBox.critical(
                parent,
                "Operation could not run",
                f"The operation failed before returning results:\n\n{progress.error}",
            )
        return []

    ctx.notify_history_changed()
    if show_results and progress.results:
        result_dialog = OperationResultDialog(progress.results, parent=parent)
        result_dialog.exec()
    return progress.results


class TextDialog(QDialog):
    """Simple read-only text viewer for service output."""

    def __init__(self, title: str, text: str, *, parent: QWidget | None = None, html_text: bool = False) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(820, 600)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 12)
        layout.setSpacing(9)
        heading = QLabel(title)
        heading.setObjectName("CardTitle")
        layout.addWidget(heading)
        if html_text:
            viewer = QTextBrowser()
            viewer.setHtml(text)
        else:
            viewer = QPlainTextEdit()
            viewer.setObjectName("LogView")
            viewer.setReadOnly(True)
            viewer.setPlainText(text)
        layout.addWidget(viewer, 1)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.accept)
        layout.addWidget(buttons)
