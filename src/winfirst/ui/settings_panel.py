"""Reusable settings browser backed by the central settings registry.

Every row shows the current value, the editor for a new value and the
verification method.  Detection and application both run through the shared
operation controller, so a row can never report success without a read-back.
"""
from __future__ import annotations

from typing import Any, Iterable, Sequence

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from winfirst.core import settings_registry
from winfirst.models.setting_definition import DetectedSetting, SettingDefinition
from winfirst.ui.dialogs import run_operations
from winfirst.ui.widgets import Badge, make_button


def _status_for(detected: DetectedSetting) -> str:
    if detected.error:
        return "critical"
    if not detected.supported:
        return "muted"
    if not detected.exists:
        return "warning"
    if detected.policy_managed:
        return "info"
    return "ok"


class SettingRow(QWidget):
    def __init__(self, definition: SettingDefinition, panel: "SettingsPanel") -> None:
        super().__init__()
        self.definition = definition
        self.panel = panel
        self.detected: DetectedSetting | None = None
        self.setObjectName("SettingRow")

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 4, 0, 10)
        outer.setSpacing(5)

        top = QHBoxLayout()
        top.setSpacing(10)
        text_column = QVBoxLayout()
        text_column.setSpacing(2)
        label = QLabel(definition.label)
        label.setObjectName("StrongLabel")
        label.setWordWrap(True)
        description = QLabel(definition.description)
        description.setObjectName("SmallMuted")
        description.setWordWrap(True)
        text_column.addWidget(label)
        text_column.addWidget(description)
        top.addLayout(text_column, 1)

        self.value_badge = Badge("Reading…", "muted")
        top.addWidget(self.value_badge, 0, Qt.AlignmentFlag.AlignTop)
        outer.addLayout(top)

        controls = QHBoxLayout()
        controls.setSpacing(8)
        self.editor = self._build_editor()
        controls.addWidget(self.editor)
        if definition.unit:
            unit = QLabel(definition.unit)
            unit.setObjectName("SmallMuted")
            controls.addWidget(unit)
        controls.addStretch(1)
        self.apply_button = make_button(
            "Apply", variant="ghost", slot=self._apply, tooltip=definition.verification
        )
        controls.addWidget(self.apply_button)
        outer.addLayout(controls)

        notes = []
        if definition.requires_admin:
            notes.append("Requires administrator")
        if definition.read_only:
            notes.append("Read-only")
        if definition.sensitivity:
            notes.append(f"{definition.sensitivity.capitalize()} setting")
        if definition.requires_restart:
            notes.append("Restart required")
        if definition.remediation_available is False:
            notes.append("Detected only")
        if notes:
            note_label = QLabel(" · ".join(notes))
            note_label.setObjectName("TinyMuted")
            note_label.setWordWrap(True)
            outer.addWidget(note_label)

        self.setEnabled(not definition.read_only or True)

    # ------------------------------------------------------------------ editor
    def _build_editor(self) -> QWidget:
        definition = self.definition
        if definition.value_type == "bool":
            editor = QCheckBox("Enabled")
            editor.setEnabled(not definition.read_only)
            self._bool_editor = editor
            return editor
        if definition.value_type == "int":
            editor = QSpinBox()
            editor.setRange(definition.minimum or 0, definition.maximum or 1_000_000)
            editor.setSingleStep(5 if definition.unit == "minutes" else 1)
            if definition.unit == "minutes":
                editor.setSpecialValueText("Never")
            editor.setEnabled(not definition.read_only)
            editor.setMinimumWidth(120)
            self._int_editor = editor
            return editor
        if definition.value_type == "enum":
            editor = QComboBox()
            for value, label in definition.choices:
                editor.addItem(str(label), value)
            editor.setEnabled(not definition.read_only)
            editor.setMinimumWidth(210)
            self._enum_editor = editor
            return editor
        editor = QLabel("No editor")
        editor.setObjectName("SmallMuted")
        return editor

    def _editor_value(self) -> Any:
        definition = self.definition
        if definition.value_type == "bool":
            return self._bool_editor.isChecked()
        if definition.value_type == "int":
            return self._int_editor.value()
        if definition.value_type == "enum":
            return self._enum_editor.currentData()
        return None

    def _set_editor_value(self, value: Any) -> None:
        definition = self.definition
        if definition.value_type == "bool":
            self._bool_editor.setChecked(bool(value))
        elif definition.value_type == "int":
            if isinstance(value, int) and not isinstance(value, bool):
                self._int_editor.setValue(value)
            else:
                self._int_editor.setValue(int(definition.default or 0))
        elif definition.value_type == "enum":
            index = self._enum_editor.findData(value)
            if index >= 0:
                self._enum_editor.setCurrentIndex(index)

    # --------------------------------------------------------------- lifecycle
    def set_detected(self, detected: DetectedSetting) -> None:
        self.detected = detected
        self.value_badge.setText(detected.status)
        self.value_badge.set_status(_status_for(detected))
        tip = detected.note or detected.error
        self.value_badge.setToolTip(tip)
        if detected.exists and detected.supported:
            self._set_editor_value(detected.value)
        else:
            self._set_editor_value(self.definition.default)
        editable = (
            not self.definition.read_only
            and detected.supported
            and self.definition.remediation_available
        )
        self.editor.setEnabled(editable)
        self.apply_button.setVisible(not self.definition.read_only)
        self.apply_button.setEnabled(editable)

    def set_detection_error(self, message: str) -> None:
        self.value_badge.setText("Detection failed")
        self.value_badge.set_status("critical")
        self.value_badge.setToolTip(message)
        self.editor.setEnabled(False)
        self.apply_button.setEnabled(False)

    # ------------------------------------------------------------------- apply
    def _apply(self) -> None:
        value = self._editor_value()
        if value is None:
            return
        try:
            spec = settings_registry.build_spec(self.definition.key, value)
        except Exception as exc:
            QMessageBox.warning(self, "Invalid value", str(exc))
            return
        results = run_operations(
            self.panel.ctx,
            self,
            [spec],
            title=f"Change {self.definition.label}",
            intro=(
                "WinFirst will apply this setting and then read it back. "
                "The result dialog shows the verified value."
            ),
        )
        if results:
            self.panel.refresh()


class SettingsPanel(QWidget):
    """Grouped settings rows with shared refresh."""

    def __init__(
        self,
        ctx: Any,
        keys: Sequence[str] | None = None,
        *,
        owner: QWidget | None = None,
        parent: QWidget | None = None,
        show_refresh: bool = True,
    ) -> None:
        super().__init__(parent)
        self.ctx = ctx
        self.owner = owner or self
        self.keys = list(keys) if keys is not None else [d.key for d in settings_registry.all_definitions()]
        self.rows: dict[str, SettingRow] = {}
        self._groups: dict[str, QVBoxLayout] = {}
        self._loading = False

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(12)

        if show_refresh:
            toolbar = QHBoxLayout()
            self.status_label = QLabel("")
            self.status_label.setObjectName("SmallMuted")
            toolbar.addWidget(self.status_label, 1)
            self.refresh_button = make_button(
                "Refresh values", variant="ghost", slot=self.refresh
            )
            toolbar.addWidget(self.refresh_button)
            outer.addLayout(toolbar)

        grouped: dict[str, QGroupBox] = {}
        for key in self.keys:
            try:
                definition = settings_registry.get_definition(key)
            except KeyError:
                continue
            group = definition.group
            if group not in grouped:
                box = QGroupBox(group)
                layout = QVBoxLayout(box)
                layout.setContentsMargins(14, 16, 14, 10)
                layout.setSpacing(4)
                grouped[group] = box
                self._groups[group] = layout
                outer.addWidget(box)
            row = SettingRow(definition, self)
            grouped[group].layout().addWidget(row)
            self.rows[key] = row
        outer.addStretch(1)

    # ------------------------------------------------------------------ detect
    def refresh(self) -> None:
        if self._loading or not self.rows:
            return
        self._loading = True
        if hasattr(self, "refresh_button"):
            self.refresh_button.setEnabled(False)
        if hasattr(self, "status_label"):
            self.status_label.setText("Reading current values…")
        keys = list(self.rows)

        from winfirst.ui.workers import run_async

        def work():
            return settings_registry.detect_all(keys)

        def done(values: dict[str, DetectedSetting]) -> None:
            self._loading = False
            if hasattr(self, "refresh_button"):
                self.refresh_button.setEnabled(True)
            if hasattr(self, "status_label"):
                self.status_label.setText("Values read from Windows.")
            for key, row in self.rows.items():
                detected = values.get(key)
                if detected is None:
                    row.set_detection_error("No detector returned a value.")
                else:
                    row.set_detected(detected)

        def failed(message: str) -> None:
            self._loading = False
            if hasattr(self, "refresh_button"):
                self.refresh_button.setEnabled(True)
            if hasattr(self, "status_label"):
                self.status_label.setText(f"Detection failed: {message}")
            for row in self.rows.values():
                row.set_detection_error(message)

        run_async(self.owner, work, done, on_error=failed)


class ReadOnlySettingRow(QWidget):
    """Compact read-only security row for the privacy page."""

    def __init__(self, label: str, value: str, status: str = "info", note: str = "") -> None:
        super().__init__()
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 4, 0, 4)
        layout.setSpacing(10)
        text_column = QVBoxLayout()
        text_column.setSpacing(2)
        title = QLabel(label)
        title.setObjectName("StrongLabel")
        text_column.addWidget(title)
        if note:
            detail = QLabel(note)
            detail.setObjectName("SmallMuted")
            detail.setWordWrap(True)
            text_column.addWidget(detail)
        layout.addLayout(text_column, 1)
        badge = Badge(value, status)
        layout.addWidget(badge, 0, Qt.AlignmentFlag.AlignTop)
