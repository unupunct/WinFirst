"""Users, environment variables and optional Windows features."""
from __future__ import annotations

from typing import Any

from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMessageBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from winfirst.services import environment_service, features_service, users_service
from winfirst.ui.base_page import BasePage
from winfirst.ui.dialogs import run_operations
from winfirst.ui.widgets import Badge, Card, DataTable, KeyValueGrid, hint, make_button


class UsersPage(BasePage):
    page_key = "users"
    page_title = "Users, Environment & Features"
    page_subtitle = "Local accounts, environment variables and optional Windows components."

    def __init__(self, ctx: Any, *, parent: QWidget | None = None) -> None:
        super().__init__(ctx, parent=parent)
        self.user_snapshot = None
        self.environment_snapshot = None
        self.feature_snapshot = None
        self._built = False

    def load(self) -> None:
        if not self._built:
            self._build()
            self._built = True
        self.load_users()
        self.load_environment()
        self.load_features(force=False)

    def _build(self) -> None:
        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        self.add_widget(self.tabs)

        users_tab = QWidget()
        users_layout = QVBoxLayout(users_tab)
        users_layout.setContentsMargins(0, 12, 0, 0)
        users_layout.setSpacing(9)
        self.system_summary = KeyValueGrid()
        summary_card = Card("System summary")
        summary_card.add(self.system_summary)
        users_layout.addWidget(summary_card)
        self.users_badge = Badge("Users: —", "muted")
        users_toolbar = QHBoxLayout()
        users_toolbar.addWidget(self.users_badge)
        users_toolbar.addStretch(1)
        users_toolbar.addWidget(make_button("Refresh users", variant="ghost", slot=self.load_users))
        users_layout.addLayout(users_toolbar)
        self.users_table = DataTable(
            ["User", "State", "Administrator", "Current", "Last sign-in", "Protection"], height=210
        )
        self.users_table.resize_columns([190, 100, 130, 90, 170, 280])
        users_layout.addWidget(self.users_table)
        user_actions = QHBoxLayout()
        user_actions.setSpacing(8)
        user_actions.addWidget(make_button("Disable selected…", variant="danger", slot=self.disable_user))
        user_actions.addWidget(make_button("Enable selected…", variant="ghost", slot=self.enable_user))
        user_actions.addStretch(1)
        users_layout.addLayout(user_actions)
        users_layout.addWidget(
            hint(
                "WinFirst never reads or changes passwords and never creates hidden accounts. The signed-in "
                "account, built-in accounts and the last enabled administrator are protected."
            )
        )

        groups_card = Card("Local groups")
        self.groups_table = DataTable(["Group", "Members", "SID", "Description"], height=160)
        groups_card.add(self.groups_table)
        groups_row = QHBoxLayout()
        groups_row.addWidget(make_button("Show members…", variant="ghost", slot=self.show_members))
        groups_row.addStretch(1)
        groups_card.add_layout(groups_row)
        users_layout.addWidget(groups_card)
        self.tabs.addTab(users_tab, "Users & groups")

        env_tab = QWidget()
        env_layout = QVBoxLayout(env_tab)
        env_layout.setContentsMargins(0, 12, 0, 0)
        env_layout.setSpacing(9)
        env_toolbar = QHBoxLayout()
        env_toolbar.setSpacing(8)
        self.env_search = QLineEdit()
        self.env_search.setPlaceholderText("Filter environment variables…")
        self.env_search.setClearButtonEnabled(True)
        self.env_search.textChanged.connect(self._filter_environment)
        env_toolbar.addWidget(self.env_search, 2)
        self.env_scope = QComboBox()
        self.env_scope.addItem("All scopes", "")
        self.env_scope.addItem("Current user", "user")
        self.env_scope.addItem("Machine", "machine")
        self.env_scope.currentIndexChanged.connect(self._filter_environment)
        env_toolbar.addWidget(self.env_scope)
        env_toolbar.addWidget(make_button("Refresh", variant="ghost", slot=self.load_environment))
        env_layout.addLayout(env_toolbar)
        self.env_table = DataTable(["Name", "Scope", "Type", "Value"], height=360)
        self.env_table.resize_columns([250, 100, 100, 520])
        env_layout.addWidget(self.env_table)
        env_actions = QHBoxLayout()
        env_actions.setSpacing(8)
        env_actions.addWidget(make_button("New variable…", variant="primary", slot=self.new_variable))
        env_actions.addWidget(make_button("Edit selected…", variant="ghost", slot=self.edit_variable))
        env_actions.addWidget(make_button("Delete selected…", variant="danger", slot=self.delete_variable))
        env_actions.addStretch(1)
        self.env_note = hint("")
        env_actions.addWidget(self.env_note)
        env_layout.addLayout(env_actions)
        env_layout.addWidget(
            hint(
                "Variables whose names look like secrets are masked in the table and in history. "
                "PATH changes are high risk and protected from deletion."
            )
        )
        self.tabs.addTab(env_tab, "Environment")

        features_tab = QWidget()
        features_layout = QVBoxLayout(features_tab)
        features_layout.setContentsMargins(0, 12, 0, 0)
        features_layout.setSpacing(9)
        features_toolbar = QHBoxLayout()
        self.features_badge = Badge("Features: —", "muted")
        features_toolbar.addWidget(self.features_badge)
        features_toolbar.addStretch(1)
        features_toolbar.addWidget(make_button("Refresh", variant="ghost", slot=lambda: self.load_features(force=True)))
        features_layout.addLayout(features_toolbar)
        self.features_table = DataTable(["Component", "Kind", "State", "Detail"], height=380)
        self.features_table.resize_columns([340, 130, 160, 350])
        features_layout.addWidget(self.features_table)
        feature_actions = QHBoxLayout()
        feature_actions.setSpacing(8)
        feature_actions.addWidget(make_button("Enable selected…", variant="ghost", slot=self.enable_feature))
        feature_actions.addWidget(make_button("Disable selected…", variant="danger", slot=self.disable_feature))
        feature_actions.addStretch(1)
        features_layout.addLayout(feature_actions)
        features_layout.addWidget(
            hint(
                "Feature changes are applied one at a time with -NoRestart. A pending restart is reported as "
                "pending, not as a completed change. No bulk feature changes are offered."
            )
        )
        self.tabs.addTab(features_tab, "Optional features")

    # -------------------------------------------------------------------- users
    def load_users(self) -> None:
        self.run_async(
            lambda: (
                users_service.get_system_summary(self.ctx.runner),
                users_service.get_local_users(self.ctx.runner),
                users_service.get_local_groups(self.ctx.runner),
            ),
            self._show_users,
            busy="Reading local users and groups…",
        )

    def _show_users(self, data) -> None:
        summary, snapshot, group_data = data
        groups, group_errors = group_data
        self.user_snapshot = snapshot
        self.system_summary.set_rows(
            [
                ("Computer", summary.computer_name or "Unknown"),
                ("Domain", summary.domain or "Workgroup"),
                ("Windows", f"{summary.os_name} {summary.os_build}".strip() or "Unknown"),
                ("Manufacturer / model", f"{summary.manufacturer} {summary.model}".strip() or "Unknown"),
                ("Architecture", summary.architecture or "Unknown"),
                ("Last boot", summary.last_boot or "Unknown"),
            ]
        )
        self.users_badge.setText(f"Users: {len(snapshot.users)}")
        self.users_badge.set_status("info")
        if snapshot.errors:
            self.show_warning("Some user details could not be read: " + "; ".join(snapshot.errors[:3]))
        if group_errors:
            self.show_warning("Some group details could not be read: " + "; ".join(group_errors[:3]))
        self.users_table.set_rows(
            [
                [
                    user.name,
                    user.status_label,
                    "Yes" if user.is_admin else "No",
                    "Yes" if user.is_current else "No",
                    user.last_logon or "—",
                    user.protection_reason or ("Protected" if not user.can_toggle else ""),
                ]
                for user in snapshot.users
            ],
            payloads=snapshot.users,
        )
        self.groups_table.set_rows(
            [
                [group.name, group.member_count if group.member_count is not None else "—", group.sid or "—", group.description or group.error or "—"]
                for group in groups
            ],
            payloads=groups,
        )

    def _toggle_user(self, enable: bool) -> None:
        selected = self.users_table.selected_payloads()
        if not selected:
            return
        try:
            users_service.validate_user_toggle_batch(selected, enable, self.user_snapshot)
        except ValueError as exc:
            QMessageBox.warning(self, "Batch change refused", str(exc))
            return
        specs = []
        errors = []
        for user in selected:
            try:
                specs.append(users_service.build_toggle_user_spec(user, enable, self.user_snapshot))
            except Exception as exc:
                errors.append(f"{user.name}: {exc}")
        if errors:
            QMessageBox.warning(self, "Some accounts were skipped", "\n".join(errors[:20]))
        if not specs:
            return
        results = run_operations(
            self.ctx,
            self,
            specs,
            title="Enable local accounts" if enable else "Disable local accounts",
            intro="Each account state is read back from Windows. Protection rules are re-checked during apply.",
        )
        if results:
            self.load_users()

    def enable_user(self) -> None:
        self._toggle_user(True)

    def disable_user(self) -> None:
        self._toggle_user(False)

    def show_members(self) -> None:
        groups = self.groups_table.selected_payloads()
        if not groups:
            QMessageBox.information(self, "No group selected", "Select a local group first.")
            return
        group = groups[0]

        def work():
            return group.name, users_service.get_group_members(group.name, self.ctx.runner)

        self.run_async(work, self._show_members, busy=f"Reading members of {group.name}…")

    def _show_members(self, data) -> None:
        name, members = data
        text = "\n".join(
            f"{member.get('name', '')} · {member.get('class', '')} · {member.get('source', '')}"
            for member in members
        )
        QMessageBox.information(
            self,
            f"Members of {name}",
            text or "Windows returned no members for this group.",
        )

    # -------------------------------------------------------------- environment
    def load_environment(self) -> None:
        self.run_async(
            environment_service.get_environment_variables,
            self._show_environment,
            busy="Reading environment variables…",
        )

    def _show_environment(self, snapshot) -> None:
        self.environment_snapshot = snapshot
        self._filter_environment()
        if snapshot.errors:
            self.show_warning("Some environment variables could not be read: " + "; ".join(snapshot.errors[:3]))
        self.env_note.setText(f"{len(snapshot.variables)} variables")

    def _filter_environment(self) -> None:
        if self.environment_snapshot is None:
            return
        needle = self.env_search.text().strip().casefold()
        scope = self.env_scope.currentData() or ""
        rows = []
        payloads = []
        for variable in self.environment_snapshot.variables:
            if scope and variable.scope != scope:
                continue
            if needle and needle not in variable.name.casefold():
                continue
            value = "•••••• (masked)" if variable.secret else str(variable.value or "")
            rows.append(
                [
                    variable.name,
                    variable.scope,
                    variable.value_type,
                    value,
                ]
            )
            payloads.append(variable)
        self.env_table.set_rows(rows, payloads=payloads)

    def new_variable(self) -> None:
        name, ok = QInputDialog.getText(self, "New environment variable", "Name:")
        if not ok or not name.strip():
            return
        multiline = name.strip().casefold() in {"path", "pathext", "psmodulepath"}
        if multiline:
            value, ok = QInputDialog.getMultiLineText(self, "Variable value", f"Value for {name}:")
        else:
            value, ok = QInputDialog.getText(self, "Variable value", f"Value for {name}:")
        if not ok:
            return
        scope = self.env_scope.currentData() or "user"
        try:
            spec = environment_service.build_set_spec(scope, name.strip(), value)
        except Exception as exc:
            QMessageBox.warning(self, "Variable rejected", str(exc))
            return
        results = run_operations(
            self.ctx,
            self,
            [spec],
            title="Set environment variable",
            intro="The value is written to the registry and read back for verification.",
        )
        if results:
            self.load_environment()

    def edit_variable(self) -> None:
        selected = self.env_table.selected_payloads()
        if not selected:
            return
        variable = selected[0]
        if variable.secret:
            QMessageBox.warning(
                self,
                "Secret-like variable",
                f"{variable.name} looks like it contains credentials. WinFirst will not display its value. "
                "Use the Windows environment variable editor for this entry.",
            )
            return
        multiline = variable.multi_value or variable.name.casefold() in {"path", "pathext", "psmodulepath"}
        if multiline:
            value, ok = QInputDialog.getMultiLineText(
                self, "Edit variable", f"{variable.name} ({variable.scope}):", str(variable.value or "")
            )
        else:
            value, ok = QInputDialog.getText(
                self, "Edit variable", f"{variable.name} ({variable.scope}):", text=str(variable.value or "")
            )
        if not ok:
            return
        try:
            spec = environment_service.build_set_spec(variable.scope, variable.name, value)
        except Exception as exc:
            QMessageBox.warning(self, "Variable rejected", str(exc))
            return
        results = run_operations(self.ctx, self, [spec], title="Edit environment variable")
        if results:
            self.load_environment()

    def delete_variable(self) -> None:
        selected = self.env_table.selected_payloads()
        if not selected:
            return
        variable = selected[0]
        try:
            spec = environment_service.build_delete_spec(variable.scope, variable.name)
        except Exception as exc:
            QMessageBox.warning(self, "Variable cannot be deleted", str(exc))
            return
        results = run_operations(
            self.ctx,
            self,
            [spec],
            title="Delete environment variable",
            intro="The previous value is saved for rollback where Windows exposes it safely.",
        )
        if results:
            self.load_environment()

    # ----------------------------------------------------------------- features
    def load_features(self, *, force: bool = False) -> None:
        self.run_async(
            lambda: features_service.get_feature_snapshot(force=force),
            self._show_features,
            busy="Reading optional Windows features…",
        )

    def _show_features(self, snapshot) -> None:
        self.feature_snapshot = snapshot
        items = list(snapshot.features) + list(snapshot.capabilities)
        self.features_badge.setText(f"Components: {len(items)}")
        self.features_badge.set_status("info")
        self.features_table.set_rows(
            [
                [
                    item.display_name,
                    item.kind,
                    item.state_label,
                    item.error or "",
                ]
                for item in items
            ],
            payloads=items,
        )
        if snapshot.errors:
            self.show_warning("Feature inventory partially unavailable: " + "; ".join(snapshot.errors[:3]))

    def _change_feature(self, enable: bool) -> None:
        selected = self.features_table.selected_payloads()
        if not selected:
            return
        if len(selected) > 1:
            QMessageBox.information(
                self,
                "Select one component",
                "Optional features are changed one at a time so each result can be verified. Select a single row.",
            )
            return
        feature = selected[0]
        try:
            spec = features_service.build_change_spec(feature, enable)
        except Exception as exc:
            QMessageBox.warning(self, "Feature cannot be changed", str(exc))
            return
        results = run_operations(
            self.ctx,
            self,
            [spec],
            title=("Enable " if enable else "Disable ") + feature.display_name,
            intro="DISM runs with -NoRestart, then the component state is read back. A pending restart is reported as pending.",
        )
        if results:
            self.load_features(force=True)

    def enable_feature(self) -> None:
        self._change_feature(True)

    def disable_feature(self) -> None:
        self._change_feature(False)
