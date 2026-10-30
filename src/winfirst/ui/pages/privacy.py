"""Privacy settings and read-only security posture."""
from __future__ import annotations

import os
from typing import Any

from PySide6.QtWidgets import (
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QVBoxLayout,
    QWidget,
)

from winfirst.core import settings_registry
from winfirst.services import security_service
from winfirst.ui.base_page import BasePage
from winfirst.ui.settings_panel import SettingsPanel
from winfirst.ui.widgets import Badge, Banner, Card, DataTable, KeyValueGrid, hint, make_button


class PrivacyPage(BasePage):
    page_key = "privacy"
    page_title = "Privacy & Security"
    page_subtitle = "Privacy settings you can change, and security controls WinFirst inspects but never weakens."

    def __init__(self, ctx: Any, *, parent: QWidget | None = None) -> None:
        super().__init__(ctx, parent=parent)
        self._built = False

    def load(self) -> None:
        if not self._built:
            self._build()
            self._built = True
        self.refresh()
        self.panel.refresh()

    def _build(self) -> None:
        self.add_action_button("Refresh posture", slot=self.refresh, variant="ghost")
        self.add_action_button("Open Windows Security", slot=self.open_security_center, variant="ghost")
        self.add_widget(
            Banner(
                "WinFirst does not disable Microsoft Defender, Windows Firewall, UAC or Windows Update. "
                "These cards are read-only so a mistaken profile cannot silently weaken protection.",
                "info",
            )
        )

        grid = QGridLayout()
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(12)

        self.defender_card = Card("Microsoft Defender")
        self.defender_grid = KeyValueGrid()
        self.defender_card.add(self.defender_grid)
        grid.addWidget(self.defender_card, 0, 0)

        self.firewall_card = Card("Windows Firewall")
        self.firewall_table = DataTable(["Profile", "Enabled", "Inbound", "Outbound"], height=130)
        self.firewall_card.add(self.firewall_table)
        grid.addWidget(self.firewall_card, 0, 1)

        self.uac_card = Card("User Account Control")
        self.uac_grid = KeyValueGrid()
        self.uac_card.add(self.uac_grid)
        grid.addWidget(self.uac_card, 1, 0)

        self.platform_card = Card("Platform security")
        self.platform_grid = KeyValueGrid()
        self.platform_card.add(self.platform_grid)
        grid.addWidget(self.platform_card, 1, 1)

        self.updates_card = Card("Windows Update")
        self.updates_grid = KeyValueGrid()
        self.updates_card.add(self.updates_grid)
        grid.addWidget(self.updates_card, 2, 0)

        self.bitlocker_card = Card("BitLocker")
        self.bitlocker_table = DataTable(["Volume", "Status", "Protection", "Encrypted"], height=130)
        self.bitlocker_card.add(self.bitlocker_table)
        grid.addWidget(self.bitlocker_card, 2, 1)
        self.add_layout(grid)

        findings_card = Card(
            "Findings",
            "Observations from the read-only inspection. They are not automatic recommendations.",
        )
        self.findings_table = DataTable(["Finding", "Status", "Detail"], height=190)
        findings_card.add(self.findings_table)
        self.add_widget(findings_card)

        privacy_keys = [
            definition.key
            for definition in settings_registry.all_definitions()
            if definition.group == "Privacy"
        ]
        self.panel = SettingsPanel(self.ctx, privacy_keys, owner=self)
        self.add_widget(self.panel)
        self.add_widget(
            hint(
                "Privacy settings change Windows telemetry and advertising behaviour. Policy-managed "
                "values may ignore local changes; the read-back result will show that."
            )
        )

    # ------------------------------------------------------------------ posture
    def refresh(self) -> None:
        self.clear_error()

        def work():
            return {
                "defender": security_service.get_defender_summary(),
                "firewall": security_service.get_firewall_profiles(),
                "uac": security_service.get_uac_settings(),
                "secure_boot": security_service.get_secure_boot_status(),
                "tpm": security_service.get_tpm_status(),
                "activation": security_service.get_activation_status(),
                "bitlocker": security_service.get_bitlocker_volumes(),
                "updates": security_service.get_update_status(),
                "findings": security_service.security_findings(),
            }

        self.run_async(work, self._show, busy="Inspecting security posture…")

    def _show(self, data: dict[str, Any]) -> None:
        defender = data["defender"]
        if defender.available:
            self.defender_grid.set_rows(
                [
                    ("Real-time protection", self._on_off(defender.realtime_enabled)),
                    ("Antivirus", self._on_off(defender.antivirus_enabled)),
                    ("Tamper protection", self._on_off(defender.tamper_protected)),
                    ("Signature age", f"{defender.signature_age_days} days" if defender.signature_age_days is not None else "Unknown"),
                    ("Last quick scan", defender.last_quick_scan or "Unknown"),
                ]
            )
        else:
            self.defender_grid.set_rows([("Status", defender.error or "Not reported")])

        self.firewall_table.set_rows(
            [
                [
                    profile.name,
                    "Yes" if profile.enabled else "No",
                    profile.default_inbound,
                    profile.default_outbound,
                ]
                for profile in data["firewall"]
            ]
        )
        uac = data["uac"]
        self.uac_grid.set_rows(
            [
                ("UAC", "Enabled" if uac.enabled else "Disabled"),
                ("Prompt", uac.consent_prompt or "Unknown"),
                ("Secure desktop", self._on_off(uac.secure_desktop)),
                ("Admin approval mode", self._on_off(uac.admin_approval_mode)),
            ]
        )
        secure_boot = data["secure_boot"]
        tpm = data["tpm"]
        activation = data["activation"]
        self.platform_grid.set_rows(
            [
                (
                    "Secure Boot",
                    ("Enabled" if secure_boot.enabled else "Disabled") if secure_boot.available else (secure_boot.reason or "Unavailable"),
                ),
                (
                    "TPM",
                    (
                        f"Present · {tpm.version or 'version unknown'} · ready={tpm.ready}"
                        if tpm.present
                        else (tpm.reason or "Not present")
                    ),
                ),
                (
                    "Activation",
                    activation.status_text if activation.available else (activation.reason or "Unavailable"),
                ),
            ]
        )
        updates = data["updates"]
        self.updates_grid.set_rows(
            [
                ("Service", updates.service_state or "Unknown"),
                ("Start mode", updates.service_start_mode or "Unknown"),
                ("Hotfixes", str(len(updates.hotfixes))),
                ("Pending restart", "Yes" if updates.pending_reboot else "No"),
                ("Policy managed", "Yes" if updates.policy_managed else "No"),
            ]
        )
        self.bitlocker_table.set_rows(
            [
                [
                    volume.mount_point,
                    volume.volume_status,
                    volume.protection_status,
                    f"{volume.encryption_percentage}%" if volume.encryption_percentage is not None else "—",
                ]
                for volume in data["bitlocker"]
            ]
        )
        if not data["bitlocker"]:
            self.bitlocker_table.set_rows([["No volumes reported", "", "", ""]])

        self.findings_table.set_rows(
            [
                [
                    finding.get("title", ""),
                    str(finding.get("status", "")).title(),
                    finding.get("detail", ""),
                ]
                for finding in data["findings"]
            ]
        )

    @staticmethod
    def _on_off(value: Any) -> str:
        if value is None:
            return "Unknown"
        return "Enabled" if value else "Disabled"

    def open_security_center(self) -> None:
        try:
            os.startfile("windowsdefender://")  # type: ignore[attr-defined]
        except Exception as exc:
            self.show_error(f"Windows Security could not be opened: {exc}")
