"""Configuration drift detection and targeted remediation."""
from __future__ import annotations

from typing import Any

from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QWidget,
)

from winfirst.core.report_manager import build_drift_report
from winfirst.ui.base_page import BasePage
from winfirst.ui.dialogs import run_operations
from winfirst.ui.widgets import Badge, Card, DataTable, hint, make_button


class DriftPage(BasePage):
    page_key = "drift"
    page_title = "Configuration Drift"
    page_subtitle = "Compare this computer against a profile and remediate only the deviations that support it."

    def __init__(self, ctx: Any, *, parent: QWidget | None = None) -> None:
        super().__init__(ctx, parent=parent)
        self.profiles = []
        self.result = None
        self._built = False

    def load(self) -> None:
        if not self._built:
            self._build()
            self._built = True
        self.refresh_profiles()
        self.load_snapshots()

    def _build(self) -> None:
        self.add_action_button("Save snapshot", slot=self.save_snapshot, variant="ghost")
        self.add_action_button("Generate report", slot=self.generate_report, variant="ghost")

        selector = Card("Comparison target")
        row = QHBoxLayout()
        row.setSpacing(8)
        self.profile_combo = QComboBox()
        self.profile_combo.setMinimumWidth(320)
        row.addWidget(self.profile_combo, 1)
        row.addWidget(make_button("Compare", variant="primary", slot=self.compare))
        row.addWidget(make_button("Reload profiles", variant="ghost", slot=self.refresh_profiles))
        selector.add_layout(row)
        self.summary_label = QLabel("")
        self.summary_label.setWordWrap(True)
        self.summary_label.setObjectName("SmallMuted")
        selector.add(self.summary_label)
        self.add_widget(selector)

        stats = QHBoxLayout()
        stats.setSpacing(8)
        self.match_badge = Badge("Matches: —", "muted")
        self.diff_badge = Badge("Differences: —", "muted")
        self.unsupported_badge = Badge("Unsupported: —", "muted")
        for badge in (self.match_badge, self.diff_badge, self.unsupported_badge):
            stats.addWidget(badge)
        stats.addStretch(1)
        self.add_layout(stats)

        self.table = DataTable(
            ["Setting", "Group", "Profile value", "Current value", "Status", "Severity", "Remediation"],
            height=380,
        )
        self.table.resize_columns([240, 170, 160, 180, 120, 90, 260])
        self.table.itemSelectionChanged.connect(self._selection_changed)
        self.add_widget(self.table)

        actions = QHBoxLayout()
        actions.setSpacing(8)
        actions.addWidget(make_button("Remediate selected…", variant="primary", slot=self.remediate_selected))
        actions.addWidget(make_button("Remediate all supported…", variant="ghost", slot=self.remediate_all))
        actions.addStretch(1)
        self.selection_label = hint("Select deviations to remediate.")
        actions.addWidget(self.selection_label)
        self.add_layout(actions)
        self.add_widget(
            hint(
                "Unsupported settings and missing software are visible but are never counted as failures. "
                "Each remediated setting is verified independently."
            )
        )

        snapshot_card = Card("Recent snapshots", "Drift snapshots are local records of what was compared.")
        self.snapshots_table = DataTable(["When", "Profile", "Compared", "Differences"], height=150)
        snapshot_card.add(self.snapshots_table)
        self.add_widget(snapshot_card)

    # --------------------------------------------------------------------- data
    def refresh_profiles(self) -> None:
        try:
            self.profiles = self.ctx.profiles.list_profiles()
        except Exception as exc:
            self.show_error(f"Profiles could not be loaded: {exc}")
            return
        current = self.profile_combo.currentText()
        self.profile_combo.clear()
        for profile in self.profiles:
            self.profile_combo.addItem(profile.name)
        if current:
            index = self.profile_combo.findText(current)
            if index >= 0:
                self.profile_combo.setCurrentIndex(index)

    def load_snapshots(self) -> None:
        try:
            snapshots = self.ctx.diff.recent_snapshots(12)
        except Exception:
            snapshots = []
        self.snapshots_table.set_rows(
            [
                [
                    str(row.get("created_at") or "").replace("T", " ")[:16],
                    row.get("profile_name") or "",
                    row.get("total", ""),
                    row.get("differences", ""),
                ]
                for row in snapshots
            ]
        )

    def compare(self) -> None:
        index = self.profile_combo.currentIndex()
        if index < 0 or index >= len(self.profiles):
            return
        profile = self.profiles[index]
        self.clear_error()
        self.run_async(
            lambda: self.ctx.diff.compare(profile),
            self._show_result,
            busy=f"Comparing {profile.name} with this computer…",
        )

    def _show_result(self, result) -> None:
        self.result = result
        self.summary_label.setText(
            f"{result.profile_name}: {result.matches} match · {result.deviations} deviation(s) · "
            f"{result.unsupported} unsupported · {result.unconfigured} not configured."
        )
        self.match_badge.setText(f"Matches: {result.matches}")
        self.match_badge.set_status("ok")
        self.diff_badge.setText(f"Differences: {result.deviations}")
        self.diff_badge.set_status("warning" if result.deviations else "ok")
        self.unsupported_badge.setText(f"Unsupported: {result.unsupported}")
        self.table.set_rows(
            [
                [
                    item.label,
                    item.group,
                    item.expected_display,
                    item.actual_display,
                    item.status.replace("_", " ").title(),
                    item.severity.label,
                    "Auto-remediate" if item.remediation_available else item.note or "Manual review",
                ]
                for item in result.sorted_items()
            ],
            payloads=result.sorted_items(),
        )
        try:
            self.ctx.diff.save_snapshot(result)
        except Exception:
            pass
        self.load_snapshots()
        self._selection_changed()

    # ------------------------------------------------------------------ actions
    def _selection_changed(self) -> None:
        selected = self.table.selected_payloads()
        remediation = [item for item in selected if item.remediation_available]
        self.selection_label.setText(
            f"{len(remediation)} remediable selected" if selected else "Select deviations to remediate."
        )

    def remediate_selected(self) -> None:
        if self.result is None:
            return
        selected = self.table.selected_payloads()
        self._remediate(selected, "Remediate selected deviations")

    def remediate_all(self) -> None:
        if self.result is None:
            return
        items = [item for item in self.result.attention_items() if item.remediation_available]
        self._remediate(items, "Remediate all supported deviations")

    def _remediate(self, items, title: str) -> None:
        if self.result is None or not items:
            QMessageBox.information(self, "Nothing to remediate", "No remediable deviations were selected.")
            return
        profile = next(
            (profile for profile in self.profiles if profile.name == self.result.profile_name), None
        )
        if profile is None:
            return
        keys = [item.key for item in items if item.remediation_available]
        try:
            specs, skipped = self.ctx.profiles.build_apply_specs(profile, keys)
        except Exception as exc:
            self.show_error(str(exc))
            return
        if skipped:
            QMessageBox.information(self, "Some settings were skipped", "\n".join(skipped[:30]))
        if not specs:
            return
        results = run_operations(
            self.ctx,
            self,
            specs,
            title=title,
            profile_name=profile.name,
            intro=f"{len(specs)} setting(s) will be applied and read back.",
        )
        if results:
            self.compare()

    def save_snapshot(self) -> None:
        if self.result is None:
            QMessageBox.information(self, "No comparison", "Run a comparison before saving a snapshot.")
            return
        try:
            self.ctx.diff.save_snapshot(self.result)
        except Exception as exc:
            self.show_error(f"The snapshot could not be saved: {exc}")
            return
        self.load_snapshots()
        self.ctx.status("Drift snapshot saved.")

    def generate_report(self) -> None:
        if self.result is None:
            QMessageBox.information(self, "No comparison", "Run a comparison before generating a report.")
            return
        result = self.result

        def work():
            return self.ctx.reports.save(build_drift_report(result), "html")

        def done(path) -> None:
            QMessageBox.information(self, "Report generated", f"Saved to:\n{path}")

        self.run_async(
            work,
            done,
            busy="Saving the drift report…",
            done_message="Drift report saved.",
        )
