"""Self-contained HTML/PDF reports and redacted diagnostic bundles.

Reports are generated from live inspection data supplied by the caller.  The
report writer never invents values and never includes command lines that might
contain credentials.  Diagnostic bundles are sanitised recursively by key name
and by known secret/identifier patterns; this reduces risk but is not a formal
guarantee, which the bundled README states.
"""
from __future__ import annotations

import csv
import hashlib
import html
import io
import json
import os
import re
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from winfirst import APP_NAME, __version__
from winfirst.core import paths
from winfirst.core.database import Database
from winfirst.core.redaction import redact_identifiers, redact_secrets, should_redact_env_name

_MAX_LOG_BYTES = 300_000
_MAX_REPORT_ROWS = 2000


@dataclass
class ReportTable:
    columns: list[str]
    rows: list[list[Any]] = field(default_factory=list)


@dataclass
class ReportSection:
    title: str
    paragraphs: list[str] = field(default_factory=list)
    key_values: list[tuple[str, str]] = field(default_factory=list)
    tables: list[ReportTable] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


@dataclass
class ReportDocument:
    title: str
    kind: str = "general"
    subtitle: str = ""
    created_at: str = field(default_factory=lambda: datetime.now().astimezone().isoformat(timespec="seconds"))
    sections: list[ReportSection] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "kind": self.kind,
            "subtitle": self.subtitle,
            "created_at": self.created_at,
            "metadata": self.metadata,
            "sections": [
                {
                    "title": section.title,
                    "paragraphs": section.paragraphs,
                    "key_values": section.key_values,
                    "notes": section.notes,
                    "tables": [
                        {"columns": table.columns, "rows": table.rows} for table in section.tables
                    ],
                }
                for section in self.sections
            ],
        }


def _now_file_stamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def _slug(text: str, limit: int = 50) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", text.strip()).strip("-._")
    return (cleaned or "report")[:limit]


def _format_cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "Yes" if value else "No"
    return str(value)


def _table_rows(table: ReportTable) -> list[list[str]]:
    return [[_format_cell(value) for value in row] for row in table.rows[:_MAX_REPORT_ROWS]]


class ReportManager:
    def __init__(self, db: Database) -> None:
        self.db = db

    # --------------------------------------------------------------- rendering
    def render_html(self, document: ReportDocument, destination: str | Path | None = None) -> Path:
        target = Path(destination) if destination else self._new_path(document, "html")
        target.parent.mkdir(parents=True, exist_ok=True)
        parts: list[str] = []
        parts.append("<!DOCTYPE html>")
        parts.append('<html lang="en"><head><meta charset="utf-8">')
        parts.append('<meta name="viewport" content="width=device-width, initial-scale=1">')
        parts.append(f"<title>{html.escape(document.title)}</title>")
        parts.append(
            """<style>
:root { color-scheme: light; }
* { box-sizing: border-box; }
body { margin: 0; padding: 32px; font-family: "Segoe UI", Arial, sans-serif;
       background: #f4f6f8; color: #17212b; line-height: 1.5; }
main { max-width: 1100px; margin: 0 auto; background: #ffffff; border: 1px solid #d9e0e7;
       border-radius: 12px; padding: 32px 36px; box-shadow: 0 6px 24px rgba(20,40,60,.06); }
h1 { margin: 0 0 4px; font-size: 28px; letter-spacing: -.02em; }
h2 { margin: 30px 0 10px; padding-bottom: 6px; border-bottom: 1px solid #e4e9ee; font-size: 19px; }
.subtitle { color: #52606d; margin: 0 0 2px; }
.meta { color: #72808d; font-size: 12px; margin-top: 6px; }
p { margin: 8px 0; }
table { width: 100%; border-collapse: collapse; margin: 10px 0 18px; font-size: 13px; }
th { text-align: left; background: #eef2f6; font-weight: 600; }
th, td { border: 1px solid #dfe5eb; padding: 7px 9px; vertical-align: top; word-break: break-word; }
tr:nth-child(even) td { background: #fafbfc; }
.kv { width: 100%; }
.kv th { width: 30%; background: #f7f9fa; font-weight: 600; }
.note { background: #fff8e6; border-left: 4px solid #d9a514; padding: 8px 12px; margin: 8px 0; font-size: 13px; }
.empty { color: #72808d; font-style: italic; }
footer { margin-top: 32px; padding-top: 12px; border-top: 1px solid #e4e9ee; color: #72808d; font-size: 11px; }
@media print { body { background: #fff; padding: 0; } main { border: none; box-shadow: none; padding: 0; } }
</style></head><body><main>"""
        )
        parts.append(f"<h1>{html.escape(document.title)}</h1>")
        if document.subtitle:
            parts.append(f'<p class="subtitle">{html.escape(document.subtitle)}</p>')
        meta = [f"Generated {html.escape(document.created_at)}", html.escape(APP_NAME), f"v{html.escape(__version__)}"]
        source = document.metadata.get("source")
        if source:
            meta.append(html.escape(str(source)))
        parts.append(f'<div class="meta">{" &middot; ".join(meta)}</div>')
        for section in document.sections:
            parts.append(f"<h2>{html.escape(section.title)}</h2>")
            for paragraph in section.paragraphs:
                parts.append(f"<p>{html.escape(paragraph)}</p>")
            if section.key_values:
                parts.append('<table class="kv"><tbody>')
                for key, value in section.key_values:
                    parts.append(
                        f"<tr><th>{html.escape(str(key))}</th><td>{html.escape(str(value))}</td></tr>"
                    )
                parts.append("</tbody></table>")
            for table in section.tables:
                rows = _table_rows(table)
                parts.append("<table><thead><tr>")
                parts.extend(f"<th>{html.escape(str(column))}</th>" for column in table.columns)
                parts.append("</tr></thead><tbody>")
                if not rows:
                    parts.append(
                        f'<tr><td colspan="{max(1, len(table.columns))}" class="empty">No rows.</td></tr>'
                    )
                for row in rows:
                    parts.append("<tr>")
                    parts.extend(f"<td>{html.escape(cell)}</td>" for cell in row)
                    parts.append("</tr>")
                parts.append("</tbody></table>")
                if len(table.rows) > _MAX_REPORT_ROWS:
                    parts.append(
                        f'<p class="note">Only the first {_MAX_REPORT_ROWS:,} of {len(table.rows):,} rows are shown in this report.</p>'
                    )
            for note in section.notes:
                parts.append(f'<div class="note">{html.escape(note)}</div>')
        parts.append(
            f"<footer>{html.escape(APP_NAME)} {html.escape(__version__)} &middot; "
            "This report was generated from live system inspection. "
            "Unavailable values are shown as unavailable rather than inferred.</footer>"
        )
        parts.append("</main></body></html>")
        target.write_text("\n".join(parts), encoding="utf-8")
        return target

    def render_pdf(self, document: ReportDocument, destination: str | Path | None = None) -> Path:
        target = Path(destination) if destination else self._new_path(document, "pdf")
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            from reportlab.lib import colors
            from reportlab.lib.enums import TA_LEFT
            from reportlab.lib.pagesizes import A4
            from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
            from reportlab.lib.units import mm
            from reportlab.platypus import (
                PageBreak,
                Paragraph,
                SimpleDocTemplate,
                Spacer,
                Table,
                TableStyle,
            )
        except ImportError as exc:  # pragma: no cover - dependency is optional at runtime
            raise RuntimeError(
                "PDF export requires the reportlab package. Install it with: pip install reportlab"
            ) from exc

        styles = getSampleStyleSheet()
        title_style = ParagraphStyle(
            "WinFirstTitle", parent=styles["Title"], fontName="Helvetica-Bold", fontSize=19, leading=23,
            textColor=colors.HexColor("#17212b"), alignment=TA_LEFT, spaceAfter=4,
        )
        subtitle_style = ParagraphStyle(
            "WinFirstSubtitle", parent=styles["Normal"], fontSize=10, leading=14,
            textColor=colors.HexColor("#52606d"), spaceAfter=10,
        )
        heading_style = ParagraphStyle(
            "WinFirstHeading", parent=styles["Heading2"], fontName="Helvetica-Bold", fontSize=13,
            leading=17, spaceBefore=12, spaceAfter=5, textColor=colors.HexColor("#1f3b57"),
        )
        body_style = ParagraphStyle(
            "WinFirstBody", parent=styles["BodyText"], fontSize=9, leading=12.5, spaceAfter=4,
        )
        note_style = ParagraphStyle(
            "WinFirstNote", parent=body_style, textColor=colors.HexColor("#6b4e00"),
            backColor=colors.HexColor("#fff8e6"), borderPadding=5, spaceBefore=4,
        )
        cell_style = ParagraphStyle(
            "WinFirstCell", parent=body_style, fontSize=7.2, leading=9, spaceAfter=0,
        )

        def para(text: str, style=body_style) -> Paragraph:
            return Paragraph(html.escape(text).replace("\n", "<br/>"), style)

        def footer(canvas, doc) -> None:
            canvas.saveState()
            canvas.setFont("Helvetica", 7.5)
            canvas.setFillColor(colors.HexColor("#72808d"))
            canvas.drawString(18 * mm, 10 * mm, f"{APP_NAME} {__version__} - {document.title[:80]}")
            canvas.drawRightString(A4[0] - 18 * mm, 10 * mm, f"Page {doc.page}")
            canvas.restoreState()

        pdf = SimpleDocTemplate(
            str(target),
            pagesize=A4,
            leftMargin=18 * mm,
            rightMargin=18 * mm,
            topMargin=16 * mm,
            bottomMargin=18 * mm,
            title=document.title,
            author=APP_NAME,
        )
        story: list[Any] = [para(document.title, title_style)]
        if document.subtitle:
            story.append(para(document.subtitle, subtitle_style))
        story.append(para(f"Generated {document.created_at} with {APP_NAME} {__version__}.", subtitle_style))
        for section in document.sections:
            story.append(para(section.title, heading_style))
            for paragraph_text in section.paragraphs:
                story.append(para(paragraph_text))
            if section.key_values:
                rows = [[para(str(key), cell_style), para(str(value), cell_style)] for key, value in section.key_values]
                table = Table(rows, colWidths=[48 * mm, 126 * mm])
                table.setStyle(
                    TableStyle(
                        [
                            ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#f2f5f7")),
                            ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#dfe5eb")),
                            ("VALIGN", (0, 0), (-1, -1), "TOP"),
                            ("LEFTPADDING", (0, 0), (-1, -1), 4),
                            ("RIGHTPADDING", (0, 0), (-1, -1), 4),
                            ("TOPPADDING", (0, 0), (-1, -1), 2),
                            ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
                        ]
                    )
                )
                story.append(table)
                story.append(Spacer(1, 4))
            for table_data in section.tables:
                values = _table_rows(table_data)
                header = [para(str(column), cell_style) for column in table_data.columns]
                rows = [header]
                for row in values:
                    rows.append([para(cell, cell_style) for cell in row])
                if len(rows) == 1:
                    rows.append([para("No rows.", cell_style)] + [""] * (len(header) - 1))
                column_count = max(1, len(table_data.columns))
                width = 174 * mm / column_count
                table = Table(rows, colWidths=[width] * column_count, repeatRows=1)
                table.setStyle(
                    TableStyle(
                        [
                            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#eef2f6")),
                            ("GRID", (0, 0), (-1, -1), 0.35, colors.HexColor("#dfe5eb")),
                            ("VALIGN", (0, 0), (-1, -1), "TOP"),
                            ("LEFTPADDING", (0, 0), (-1, -1), 3),
                            ("RIGHTPADDING", (0, 0), (-1, -1), 3),
                            ("TOPPADDING", (0, 0), (-1, -1), 2),
                            ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
                        ]
                    )
                )
                story.append(table)
                story.append(Spacer(1, 4))
                if len(table_data.rows) > _MAX_REPORT_ROWS:
                    story.append(
                        para(
                            f"Only the first {_MAX_REPORT_ROWS:,} of {len(table_data.rows):,} rows are shown.",
                            note_style,
                        )
                    )
            for note in section.notes:
                story.append(para(note, note_style))
        pdf.build(story, onFirstPage=footer, onLaterPages=footer)
        return target

    def _new_path(self, document: ReportDocument, extension: str) -> Path:
        base = paths.reports_dir() / f"{_now_file_stamp()}_{_slug(document.title)}"
        candidate = base.with_suffix(f".{extension}")
        if candidate.exists():
            for index in range(2, 1000):
                candidate = base.with_name(f"{base.name}-{index}").with_suffix(f".{extension}")
                if not candidate.exists():
                    break
        return candidate

    def record(self, document: ReportDocument, path: Path, fmt: str) -> int:
        metadata = dict(document.metadata)
        metadata.update({"format": fmt, "app_version": __version__})
        return self.db.execute(
            "INSERT INTO reports(kind, title, path, created_at, metadata_json) VALUES (?, ?, ?, ?, ?)",
            (
                document.kind,
                document.title,
                str(path),
                datetime.now(timezone.utc).isoformat(timespec="seconds"),
                json.dumps(metadata, ensure_ascii=False, default=str),
            ),
        )

    def save(self, document: ReportDocument, fmt: str = "html") -> Path:
        if fmt == "html":
            path = self.render_html(document)
        elif fmt == "pdf":
            path = self.render_pdf(document)
        else:
            raise ValueError("Report format must be 'html' or 'pdf'.")
        self.record(document, path, fmt)
        return path

    def list_reports(self, limit: int = 200) -> list[dict[str, Any]]:
        rows = self.db.query(
            "SELECT * FROM reports ORDER BY id DESC LIMIT ?", (max(1, min(limit, 1000)),)
        )
        for row in rows:
            row["exists"] = Path(str(row.get("path") or "")).is_file()
        return rows

    # ------------------------------------------------------------- diagnostics
    def create_diagnostic_bundle(
        self,
        *,
        data: dict[str, Any],
        log_text: str | None = None,
        include_identifiers: bool = False,
        hostname: str = "",
        username: str = "",
        extra_sections: dict[str, str] | None = None,
    ) -> Path:
        safe = _sanitize(
            data, include_identifiers=include_identifiers, hostname=hostname, username=username
        )
        if log_text is None:
            log_text = self._tail_log()
        safe_log = _sanitize_text(
            log_text, include_identifiers=include_identifiers, hostname=hostname, username=username
        )
        operations = safe.get("operations") if isinstance(safe.get("operations"), list) else []
        csv_buffer = io.StringIO()
        writer = csv.writer(csv_buffer)
        writer.writerow(["Started", "Category", "Operation", "Status", "Profile", "Message"])
        for row in operations[:1000]:
            if not isinstance(row, dict):
                continue
            writer.writerow(
                [
                    row.get("started_at", ""),
                    row.get("category", ""),
                    row.get("name", ""),
                    row.get("status", ""),
                    row.get("profile_name", ""),
                    row.get("message", "") or row.get("error", ""),
                ]
            )
        readme = (
            f"{APP_NAME} {__version__} diagnostic bundle\n"
            f"Created: {datetime.now().astimezone().isoformat(timespec='seconds')}\n\n"
            "Contents:\n"
            "- diagnostics.json: system, capability and configuration summary\n"
            "- operations.csv: recent operation history\n"
            "- winfirst.log: recent application log tail\n"
            "- manifest.json: SHA-256 of each file in this bundle\n\n"
            "Privacy: "
            + (
                "This bundle includes host and user identifiers because you explicitly enabled them. "
                if include_identifiers
                else "Host name, user name, IPv4 addresses and MAC addresses were redacted. "
            )
            + "Values whose key names look like passwords, tokens, keys or credentials are redacted. "
            "Pattern-based redaction reduces risk but is not a formal guarantee. Review this bundle "
            "before sharing it. It does not contain command lines, registry dumps, files from the "
            "repository, or software installers.\n"
        )
        zip_path = paths.bundles_dir() / f"{_now_file_stamp()}_winfirst-diagnostics.zip"
        if zip_path.exists():
            zip_path = paths.bundles_dir() / f"{_now_file_stamp()}_winfirst-diagnostics-{os.getpid()}.zip"
        files: dict[str, bytes] = {
            "diagnostics.json": json.dumps(safe, indent=2, ensure_ascii=False, default=str).encode("utf-8"),
            "operations.csv": csv_buffer.getvalue().encode("utf-8-sig"),
            "winfirst.log": safe_log.encode("utf-8", errors="replace"),
            "README.txt": readme.encode("utf-8"),
        }
        for name, content in (extra_sections or {}).items():
            safe_name = _slug(name, 80)
            if safe_name and content:
                files[f"extra/{safe_name}.txt"] = _sanitize_text(
                    content, include_identifiers=include_identifiers, hostname=hostname, username=username
                ).encode("utf-8", errors="replace")
        manifest = {
            "app": APP_NAME,
            "version": __version__,
            "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "identifiers_included": include_identifiers,
            "files": {
                name: hashlib.sha256(content).hexdigest() for name, content in files.items()
            },
        }
        with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for name, content in files.items():
                archive.writestr(name, content)
            archive.writestr(
                "manifest.json",
                json.dumps(manifest, indent=2, ensure_ascii=False).encode("utf-8"),
            )
        self.db.execute(
            "INSERT INTO reports(kind, title, path, created_at, metadata_json) VALUES (?, ?, ?, ?, ?)",
            (
                "diagnostic_bundle",
                "WinFirst diagnostic bundle",
                str(zip_path),
                datetime.now(timezone.utc).isoformat(timespec="seconds"),
                json.dumps({"identifiers_included": include_identifiers, "app_version": __version__}),
            ),
        )
        return zip_path

    def _tail_log(self) -> str:
        path = paths.log_file()
        try:
            size = path.stat().st_size
            with path.open("rb") as handle:
                if size > _MAX_LOG_BYTES:
                    handle.seek(size - _MAX_LOG_BYTES)
                raw = handle.read()
            return raw.decode("utf-8", errors="replace")
        except OSError:
            return "(The application log could not be read.)"


def _sanitize(value: Any, *, include_identifiers: bool, hostname: str, username: str) -> Any:
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            if _sensitive_key(key_text):
                result[key_text] = "***REDACTED***"
            else:
                result[key_text] = _sanitize(
                    item, include_identifiers=include_identifiers, hostname=hostname, username=username
                )
        return result
    if isinstance(value, (list, tuple, set)):
        return [
            _sanitize(item, include_identifiers=include_identifiers, hostname=hostname, username=username)
            for item in value
        ]
    if isinstance(value, str):
        return _sanitize_text(value, include_identifiers=include_identifiers, hostname=hostname, username=username)
    return value


def _sanitize_text(text: str, *, include_identifiers: bool, hostname: str, username: str) -> str:
    result = redact_secrets(text)
    if not include_identifiers:
        result = redact_identifiers(result, hostname=hostname, username=username)
    return result


def _sensitive_key(key: str) -> bool:
    lowered = key.casefold()
    if should_redact_env_name(key):
        return True
    return any(
        token in lowered
        for token in ("password", "passwd", "secret", "token", "credential", "private_key", "api_key", "access_key")
    )


# ------------------------------------------------------------------ builders
def build_system_report(
    *,
    os_info: Any,
    hardware: Any = None,
    storage: Any = None,
    battery: Any = None,
    security_findings: Iterable[dict[str, Any]] | None = None,
    device_health: Any = None,
    startup_count: int | None = None,
    notes: list[str] | None = None,
) -> ReportDocument:
    from winfirst.services.storage_health_service import format_bytes

    document = ReportDocument(
        title="System health report",
        kind="system",
        subtitle="Read-only Windows inventory and findings",
        metadata={"source": "local machine"},
    )
    overview = ReportSection(title="Overview")
    if os_info is not None:
        overview.key_values.extend(
            [
                ("Computer", getattr(os_info, "hostname", "") or "Unknown"),
                ("Windows", getattr(os_info, "version_label", "") or "Unknown"),
                ("Edition", getattr(os_info, "edition", "") or "Unknown"),
                ("Architecture", getattr(os_info, "architecture_label", "") or "Unknown"),
                ("Install date", getattr(os_info, "install_date", "") or "Unknown"),
                ("Uptime", getattr(os_info, "uptime_label", "") or "Unknown"),
                ("Pending reboot", "Yes" if getattr(os_info, "pending_reboot", False) else "No"),
                ("Form factor", "Laptop" if getattr(os_info, "is_laptop", False) else "Desktop / unknown"),
            ]
        )
    if startup_count is not None:
        overview.key_values.append(("Startup entries", str(startup_count)))
    document.sections.append(overview)

    if hardware is not None:
        section = ReportSection(title="Hardware")
        try:
            section.key_values.extend(hardware.summary_rows())
        except Exception:
            section.paragraphs.append("Hardware details could not be formatted.")
        errors = getattr(hardware, "errors", None)
        if errors:
            section.notes.extend(str(item) for item in errors)
        document.sections.append(section)

    if storage is not None:
        section = ReportSection(title="Storage")
        rows = []
        for volume in getattr(storage, "volumes", []):
            rows.append(
                [
                    volume.mountpoint,
                    volume.label,
                    volume.filesystem,
                    format_bytes(volume.total),
                    format_bytes(volume.free),
                    f"{volume.percent:.0f}%",
                    volume.health or "Unknown",
                ]
            )
        section.tables.append(
            ReportTable(
                ["Volume", "Label", "File system", "Size", "Free", "Used", "Health"],
                rows,
            )
        )
        disk_rows = []
        for disk in getattr(storage, "disks", []):
            disk_rows.append(
                [
                    disk.friendly_name,
                    disk.media_type,
                    f"{disk.size_gb:.1f} GB",
                    disk.health_status,
                    disk.operational_status,
                    disk.bus_type,
                ]
            )
        section.tables.append(
            ReportTable(["Physical disk", "Media", "Size", "Health", "Operational", "Bus"], disk_rows)
        )
        smart = getattr(storage, "smart", None)
        if smart is not None:
            if getattr(smart, "available", False):
                section.key_values.append(
                    ("SMART failure prediction", "Failure predicted" if smart.failure_predicted else "No failure predicted")
                )
            else:
                section.notes.append(
                    getattr(smart, "reason", "") or "SMART failure prediction is unavailable for this storage configuration."
                )
        errors = getattr(storage, "errors", None)
        if errors:
            section.notes.extend(str(item) for item in errors)
        document.sections.append(section)

    if battery is not None:
        section = ReportSection(title="Battery")
        if not getattr(battery, "available", False):
            section.paragraphs.append(getattr(battery, "reason", "") or "No battery is available.")
        else:
            try:
                section.key_values.extend(battery.summary_rows())
            except Exception:
                section.paragraphs.append("Battery details could not be formatted.")
            if getattr(battery, "estimated", True):
                section.notes.append("Battery wear is an estimate when design and full-charge capacities are reported.")
        document.sections.append(section)

    if security_findings is not None:
        section = ReportSection(title="Security findings")
        rows = [
            [str(item.get("status", "")), str(item.get("title", "")), str(item.get("detail", ""))]
            for item in security_findings
            if isinstance(item, dict)
        ]
        section.tables.append(ReportTable(["Status", "Finding", "Detail"], rows))
        section.notes.append(
            "WinFirst reports security posture but does not disable Defender, Firewall, UAC or updates."
        )
        document.sections.append(section)

    if device_health is not None:
        section = ReportSection(title="Devices")
        section.key_values.append(("Present devices", str(getattr(device_health, "present_devices", "Unknown"))))
        rows = []
        for problem in getattr(device_health, "problems", []):
            rows.append(
                [
                    getattr(problem, "name", ""),
                    getattr(problem, "severity", ""),
                    getattr(problem, "problem_text", ""),
                    getattr(problem, "pnp_class", ""),
                    getattr(problem, "manufacturer", ""),
                ]
            )
        section.tables.append(ReportTable(["Device", "Severity", "Problem", "Class", "Manufacturer"], rows))
        document.sections.append(section)

    if notes:
        document.sections.append(ReportSection(title="Notes", notes=list(notes)))
    return document


def build_history_report(operations: list[dict[str, Any]], *, title: str = "Change history report") -> ReportDocument:
    document = ReportDocument(
        title=title,
        kind="history",
        subtitle="Recorded WinFirst operations",
        metadata={"source": "operation history"},
    )
    rows = []
    for row in operations[:1000]:
        rows.append(
            [
                str(row.get("started_at", "")).replace("T", " ")[:19],
                str(row.get("category", "")),
                str(row.get("name", "")),
                str(row.get("status", "")),
                str(row.get("profile_name", "")),
                redact_secrets(str(row.get("message", "") or row.get("error", "")))[:400],
            ]
        )
    section = ReportSection(title="Operations")
    section.tables.append(ReportTable(["Started", "Category", "Operation", "Status", "Profile", "Result"], rows))
    section.notes.append("Operation results are recorded locally in the WinFirst database. Sensitive values are redacted.")
    document.sections.append(section)
    return document


def build_drift_report(diff: Any) -> ReportDocument:
    document = ReportDocument(
        title=f"Configuration drift: {getattr(diff, 'profile_name', 'Profile')}",
        kind="drift",
        subtitle="Expected profile values compared with the live system",
        metadata={"profile": getattr(diff, "profile_name", "")},
    )
    summary = ReportSection(title="Summary")
    summary.key_values.extend(
        [
            ("Profile", str(getattr(diff, "profile_name", ""))),
            ("Settings compared", str(len(getattr(diff, "items", [])))),
            ("Matches", str(getattr(diff, "matches", 0))),
            ("Different", str(getattr(diff, "differences", 0))),
            ("Not configured", str(getattr(diff, "unconfigured", 0))),
            ("Unsupported", str(getattr(diff, "unsupported", 0))),
            ("Missing software", str(len(getattr(diff, "missing_software", [])))),
        ]
    )
    document.sections.append(summary)
    rows = []
    for item in getattr(diff, "sorted_items", lambda: [])():
        rows.append(
            [
                getattr(item, "group", ""),
                getattr(item, "label", ""),
                getattr(item, "expected_display", ""),
                getattr(item, "actual_display", ""),
                getattr(item, "status_label", ""),
                getattr(item, "severity", "").value if hasattr(getattr(item, "severity", ""), "value") else str(getattr(item, "severity", "")),
                "Yes" if getattr(item, "remediation_available", False) else "No",
            ]
        )
    section = ReportSection(title="Differences")
    section.tables.append(
        ReportTable(["Group", "Setting", "Expected", "Actual", "Status", "Severity", "Remediable"], rows)
    )
    document.sections.append(section)
    return document


def build_deployment_report(session: Any) -> ReportDocument:
    document = ReportDocument(
        title=f"Deployment checklist: {getattr(session, 'template_name', 'Session')}",
        kind="deployment",
        subtitle=f"Session {getattr(session, 'id', '')}",
        metadata={
            "template": getattr(session, "template_name", ""),
            "profile": getattr(session, "profile_name", ""),
            "status": getattr(session, "status", ""),
        },
    )
    summary = ReportSection(title="Session summary")
    summary.key_values.extend(
        [
            ("Template", str(getattr(session, "template_name", ""))),
            ("Profile", str(getattr(session, "profile_name", "") or "Not attached")),
            ("Status", str(getattr(session, "status", ""))),
            ("Started", str(getattr(session, "started_at", ""))),
            ("Completed", str(getattr(session, "completed_at", "") or "Not completed")),
            ("Progress", str(getattr(session, "progress_label", ""))),
        ]
    )
    if getattr(session, "notes", ""):
        summary.paragraphs.append(str(session.notes))
    document.sections.append(summary)
    rows = []
    for task in getattr(session, "tasks", []):
        rows.append(
            [
                str(getattr(task, "title", "")),
                "Required" if getattr(task, "required", False) else "Optional",
                str(getattr(task, "status", "")),
                str(getattr(task, "detail", "")),
                str(getattr(task, "auto_label", "")),
            ]
        )
    section = ReportSection(title="Tasks")
    section.tables.append(ReportTable(["Task", "Requirement", "Status", "Detail", "Check"], rows))
    section.notes.append("A session snapshot is stored with this report; later template edits do not change it.")
    document.sections.append(section)
    return document
