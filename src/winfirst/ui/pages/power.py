"""Power management: plans, timeouts, hibernation and button actions."""
from __future__ import annotations

from typing import Any

from PySide6.QtWidgets import QComboBox, QHBoxLayout, QLabel, QWidget

from winfirst.services import power_service
from winfirst.ui.base_page import BasePage
from winfirst.ui.dialogs import run_operations
from winfirst.ui.settings_panel import SettingsPanel
from winfirst.ui.widgets import Badge, Card, make_button


class PowerPage(BasePage):
    page_key = "power"
    page_title = "Power Management"
    page_subtitle = "Active plan, display and sleep timeouts, hibernation and hardware button behaviour."

    def __init__(self, ctx: Any, *, parent: QWidget | None = None) -> None:
        super().__init__(ctx, parent=parent)
        self.service = power_service.PowerService(ctx.runner)
        self.plans: list[Any] = []
        self._built = False

    def load(self) -> None:
        if not self._built:
            self._build()
            self._built = True
        self.refresh_plans()
        self.panel.refresh()

    def _build(self) -> None:
        plan_card = Card(
            "Power plan",
            "Only plans that already exist on this computer can be activated; WinFirst does not create or import plans.",
        )
        row = QHBoxLayout()
        row.setSpacing(9)
        self.plan_combo = QComboBox()
        self.plan_combo.setMinimumWidth(300)
        row.addWidget(self.plan_combo, 1)
        self.activate_button = make_button(
            "Activate plan…", variant="primary", slot=self.activate_plan
        )
        row.addWidget(self.activate_button)
        self.refresh_plans_button = make_button(
            "Refresh", variant="ghost", slot=self.refresh_plans
        )
        row.addWidget(self.refresh_plans_button)
        plan_card.add_layout(row)
        self.active_label = QLabel("Reading active power plan…")
        self.active_label.setObjectName("SmallMuted")
        plan_card.add(self.active_label)
        self.add_widget(plan_card)

        self.panel = SettingsPanel(
            self.ctx,
            [
                "hibernate_enabled",
                "display_timeout_ac",
                "display_timeout_dc",
                "sleep_timeout_ac",
                "sleep_timeout_dc",
                "lid_close_action",
                "power_button_action",
                "sleep_button_action",
            ],
            owner=self,
        )
        self.add_widget(self.panel)

    # --------------------------------------------------------------------- plans
    def refresh_plans(self) -> None:
        self.clear_error()

        def work():
            return self.service.list_plans(), self.service.active_plan()

        self.run_async(work, self._show_plans, busy="Reading power plans…")

    def _show_plans(self, data) -> None:
        plans, active = data
        self.plans = list(plans)
        self.plan_combo.clear()
        for plan in plans:
            label = plan.name
            if plan.recommended:
                label += " (recommended)"
            self.plan_combo.addItem(label, plan.guid)
        index = self.plan_combo.findData(active.guid)
        if index >= 0:
            self.plan_combo.setCurrentIndex(index)
        self.active_label.setText(
            f"Active plan: {active.name} · {active.guid}"
        )
        self.activate_button.setEnabled(bool(plans) and index < 0 or bool(plans))

    def activate_plan(self) -> None:
        guid = self.plan_combo.currentData()
        plan = next((item for item in self.plans if item.guid == guid), None)
        if plan is None:
            return
        spec = self.service.build_plan_spec(plan)
        results = run_operations(
            self.ctx,
            self,
            [spec],
            title="Activate power plan",
            intro="The active power plan is read back from powercfg after the change.",
        )
        if results:
            self.refresh_plans()
