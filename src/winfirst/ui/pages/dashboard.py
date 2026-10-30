"""Dashboard: machine identity, health at a glance and safe quick actions."""
from __future__ import annotations

from typing import Any

from PySide6.QtWidgets import QGridLayout, QHBoxLayout, QLabel, QVBoxLayout, QWidget

from winfirst.core import privilege_manager
from winfirst.ui.base_page import BasePage
from winfirst.ui.widgets import (
    Badge,
    Card,
    DataTable,
    KeyValueGrid,
    StatCard,
    format_datetime,
    hint,
    make_button,
)


class DashboardPage(BasePage):
    page_key = "dashboard"
    page_title = "Dashboard"
    page_subtitle = "A real-time summary of this Windows installation and recent WinFirst activity."

    def __init__(self, ctx: Any, *, parent: QWidget | None = None) -> None:
        super().__init__(ctx, parent=parent)
        self._built = False

    def load(self) -> None:
        if not self._built:
            self._build()
            self._built = True
        self.refresh()

    # -------------------------------------------------------------------- build
    def _build(self) -> None:
        stats_row = QHBoxLayout()
        stats_row.setSpacing(10)
        self.stat_operations = StatCard("Recorded operations", "—")
        self.stat_success = StatCard("Successful operations", "—")
        self.stat_attention = StatCard("Needs attention", "—")
        self.stat_profiles = StatCard("Saved profiles", "—")
        self.stat_reports = StatCard("Generated reports", "—")
        for card in (
            self.stat_operations,
            self.stat_success,
            self.stat_attention,
            self.stat_profiles,
            self.stat_reports,
        ):
            stats_row.addWidget(card, 1)
        self.add_layout(stats_row)

        columns = QHBoxLayout()
        columns.setSpacing(14)

        left = QVBoxLayout()
        left.setSpacing(14)
        system_card = Card("This computer", "Values read from Windows; unavailable items are labelled rather than guessed.")
        self.system_grid = KeyValueGrid()
        system_card.add(self.system_grid)
        self.system_badge = Badge("Checking…", "muted")
        system_card.body.addWidget(self.system_badge)
        left.addWidget(system_card)

        quick_card = Card("Quick actions", "Every action is previewed and verified before it is applied.")
        quick_layout = QGridLayout()
        quick_layout.setHorizontalSpacing(8)
        quick_layout.setVerticalSpacing(8)
        actions = [
            ("Run Quick Setup", "quick_setup", "primary"),
            ("Review configuration", "drift", "ghost"),
            ("Clean up storage", "storage", "ghost"),
            ("Update applications", "software", "ghost"),
            ("Inspect security", "privacy", "ghost"),
            ("Open change history", "history", "ghost"),
        ]
        for index, (label, key, variant) in enumerate(actions):
            button = make_button(
                label,
                variant=variant,
                slot=lambda _checked=False, target=key: self.navigate(target),
            )
            quick_layout.addWidget(button, index // 2, index % 2)
        quick_card.add_layout(quick_layout)
        left.addWidget(quick_card)
        columns.addLayout(left, 3)

        right = QVBoxLayout()
        right.setSpacing(14)
        recent_card = Card("Recent operations", "The latest entries from the local audit history.")
        self.recent_table = DataTable(
            ["Operation", "Status", "When", "Profile"], height=250, stretch_last=False
        )
        self.recent_table.resize_columns([260, 110, 140, 160])
        recent_card.add(self.recent_table)
        recent_buttons = QHBoxLayout()
        recent_buttons.addWidget(
            make_button(
                "Open history",
                variant="ghost",
                slot=lambda: self.navigate("history"),
            )
        )
        recent_buttons.addWidget(
            make_button(
                "Generate report",
                variant="ghost",
                slot=lambda: self.navigate("reports"),
            )
        )
        recent_buttons.addStretch(1)
        recent_card.add_layout(recent_buttons)
        right.addWidget(recent_card)

        readiness_card = Card("Readiness", "Things to check before handing the computer over.")
        self.readiness = QVBoxLayout()
        self.readiness.setSpacing(6)
        readiness_card.add_layout(self.readiness)
        right.addWidget(readiness_card)
        right.addStretch(1)
        columns.addLayout(right, 4)

        self.add_layout(columns)

    # ------------------------------------------------------------------ refresh
    def refresh(self) -> None:
        self.clear_error()

        def work() -> dict[str, Any]:
            stats = self.ctx.audit.stats()
            profiles = len(self.ctx.profiles.list_profiles())
            reports = len(self.ctx.reports.list_reports(limit=500))
            recent = self.ctx.audit.recent(7)
            return {
                "stats": stats,
                "profiles": profiles,
                "reports": reports,
                "recent": recent,
                "os": self.ctx.os_info,
                "elevated": self.ctx.elevated,
            }

        self.run_async(work, self._apply, busy="Reading system and history…")

    def _apply(self, data: dict[str, Any]) -> None:
        info = data["os"]
        stats = data["stats"]
        by_status = stats.get("by_status", {})
        attention = int(by_status.get("failed", 0)) + int(by_status.get("partial", 0)) + int(
            by_status.get("denied", 0)
        )
        self.stat_operations.set_value(stats.get("total", 0))
        self.stat_success.set_value(by_status.get("success", 0))
        self.stat_attention.set_value(attention)
        self.stat_profiles.set_value(data["profiles"])
        self.stat_reports.set_value(data["reports"])

        self.system_grid.set_rows(
            [
                ("Windows", info.version_label if info else "Unavailable"),
                ("Edition / build", f"{info.edition or 'Unknown'} · {info.build_label}" if info else "Unavailable"),
                ("Architecture", info.architecture_label if info else "Unavailable"),
                ("Computer name", info.hostname if info else "Unavailable"),
                ("Signed-in user", info.username if info else "Unavailable"),
                ("Uptime", info.uptime_label if info else "Unavailable"),
            ]
        )
        if info and info.pending_reboot:
            self.system_badge.setText("Restart pending")
            self.system_badge.set_status("warning")
            self.system_badge.setToolTip("; ".join(info.pending_reasons) or "Windows reports a pending restart.")
        elif data["elevated"]:
            self.system_badge.setText("Administrator session")
            self.system_badge.set_status("ok")
            self.system_badge.setToolTip(privilege_manager.integrity_note())
        else:
            self.system_badge.setText("Standard user session")
            self.system_badge.set_status("info")
            self.system_badge.setToolTip(privilege_manager.integrity_note())

        rows = []
        payloads = []
        for operation in data["recent"]:
            rows.append(
                [
                    operation.get("name") or operation.get("operation_id") or "",
                    str(operation.get("status") or "").title(),
                    format_datetime(str(operation.get("started_at") or "")),
                    operation.get("profile_name") or "—",
                ]
            )
            payloads.append(operation)
        self.recent_table.set_rows(rows, payloads=payloads)
        if not rows:
            self.recent_table.set_rows([["No operations recorded yet", "", "", ""]])

        while self.readiness.count():
            item = self.readiness.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        checks: list[tuple[str, str]] = []
        if info and info.pending_reboot:
            checks.append(("A restart is pending. Complete it before final verification.", "warning"))
        if not data["elevated"]:
            checks.append(("Changes that need administrator rights will request elevation.", "info"))
        if attention:
            checks.append((f"{attention} operation(s) need review in History & Rollback.", "warning"))
        try:
            if self.ctx.diff.latest_deviation_count() is None:
                checks.append(("No configuration drift snapshot has been saved yet.", "muted"))
        except Exception:
            pass
        if not checks:
            checks.append(("No immediate readiness issues were detected.", "ok"))
        for text, status in checks:
            row = QHBoxLayout()
            row.setSpacing(8)
            label = QLabel(text)
            label.setWordWrap(True)
            row.addWidget(label, 1)
            row.addWidget(Badge("Review" if status == "warning" else "OK", status), 0)
            wrapper = QWidget()
            wrapper.setLayout(row)
            self.readiness.addWidget(wrapper)
        if not data["recent"]:
            self.readiness.addWidget(hint("Run Quick Setup or a maintenance action to create history entries."))
