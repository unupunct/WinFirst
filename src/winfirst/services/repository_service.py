"""Portable software repository indexing.

WinFirst inventories a user-selected folder, computes SHA-256 hashes, and reads
Authenticode signatures.  It deliberately never launches scripts or unknown
installers from the repository.  "Open folder" is the only action offered.
"""
from __future__ import annotations

import csv
import hashlib
import json
import os
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable

from winfirst.core import paths
from winfirst.core.app_settings import AppSettings
from winfirst.core.command_runner import CommandRunner, as_list, ps_quote
from winfirst.core.database import Database
from winfirst.services.common import ServiceError, ps_json, require_windows

ProgressFn = Callable[[str], None]
MAX_FILES = 2500
MAX_DEPTH = 4
_SKIP_DIRS = {"$recycle.bin", "system volume information", "node_modules", ".git", "__pycache__"}
_SCRIPT_SUFFIXES = {".ps1", ".cmd", ".bat", ".vbs", ".js", ".jse", ".wsf", ".hta"}
_PACKAGE_SUFFIXES = {".exe", ".msi", ".msix", ".appx", ".msixbundle", ".appxbundle"}
_ARCHIVE_SUFFIXES = {".zip", ".7z", ".rar", ".tar", ".gz", ".iso", ".img"}
_UPDATE_SUFFIXES = {".msu", ".cab"}
_DOC_SUFFIXES = {".txt", ".md", ".pdf", ".json", ".xml", ".csv"}


@dataclass
class RepositoryItem:
    path: str
    relative_path: str
    name: str
    kind: str = "File"
    size: int = 0
    sha256: str = ""
    signature_status: str = "unknown"
    signer: str = ""
    version: str = ""
    modified: str = ""
    error: str = ""
    warning: str = ""

    @property
    def size_label(self) -> str:
        from winfirst.services.storage_health_service import format_bytes

        return format_bytes(self.size)

    @property
    def is_script(self) -> bool:
        return self.kind == "Script"

    @property
    def signed(self) -> bool:
        return self.signature_status.casefold() == "valid"

    @property
    def signature_label(self) -> str:
        if self.kind not in {"Executable", "Installer", "Script", "Update package"}:
            return "Not applicable"
        if self.signature_status.casefold() == "notsigned":
            return "Not signed"
        return self.signature_status or "Unknown"


@dataclass
class RepositoryScan:
    root: str
    items: list[RepositoryItem] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    truncated: bool = False
    cancelled: bool = False
    scanned_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds"))

    @property
    def total_size(self) -> int:
        return sum(item.size for item in self.items)


def repository_dir(settings: AppSettings) -> Path:
    configured = settings.get("repository_dir").strip()
    if configured:
        return Path(os.path.expandvars(os.path.expanduser(configured)))
    return paths.data_dir() / "repository"


def ensure_repository_dir(settings: AppSettings) -> Path:
    directory = repository_dir(settings)
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ServiceError(f"Could not create or open the repository folder: {exc}") from exc
    if not directory.is_dir():
        raise ServiceError(f"The repository path is not a folder: {directory}")
    return directory


def validate_repository_path(path: str | Path) -> Path:
    candidate = Path(os.path.expandvars(os.path.expanduser(str(path)))).resolve()
    if not candidate.is_dir():
        raise ServiceError(f"The repository folder does not exist: {candidate}")
    return candidate


def classify(path: Path) -> str:
    suffix = path.suffix.casefold()
    if suffix in _SCRIPT_SUFFIXES:
        return "Script"
    if suffix in _PACKAGE_SUFFIXES:
        return "Installer" if suffix != ".exe" else "Executable"
    if suffix in _UPDATE_SUFFIXES:
        return "Update package"
    if suffix in _ARCHIVE_SUFFIXES:
        return "Archive"
    if suffix in _DOC_SUFFIXES:
        return "Document"
    return "File"


def _iter_files(
    root: Path,
    *,
    max_files: int,
    max_depth: int,
    cancel_event: threading.Event | None,
) -> tuple[list[Path], list[str], bool, bool]:
    files: list[Path] = []
    errors: list[str] = []
    truncated = False
    cancelled = False
    stack: list[tuple[Path, int]] = [(root, 0)]
    while stack:
        if cancel_event is not None and cancel_event.is_set():
            cancelled = True
            break
        directory, depth = stack.pop()
        try:
            with os.scandir(directory) as iterator:
                for entry in iterator:
                    if cancel_event is not None and cancel_event.is_set():
                        cancelled = True
                        break
                    try:
                        if entry.is_symlink() or getattr(entry, "is_junction", lambda: False)():
                            continue
                        if entry.is_file(follow_symlinks=False):
                            files.append(Path(entry.path))
                            if len(files) >= max_files:
                                truncated = True
                                break
                        elif entry.is_dir(follow_symlinks=False) and depth < max_depth:
                            if entry.name.casefold() in _SKIP_DIRS:
                                continue
                            stack.append((Path(entry.path), depth + 1))
                    except OSError as exc:
                        if len(errors) < 30:
                            errors.append(f"{entry.path}: {exc}")
        except PermissionError:
            errors.append(f"Access denied: {directory}")
        except OSError as exc:
            errors.append(f"Could not read {directory}: {exc}")
        if truncated or cancelled:
            break
    if truncated:
        errors.append(f"The repository scan stopped after {max_files:,} files.")
    if cancelled:
        errors.append("The repository scan was cancelled.")
    return files, errors, truncated, cancelled


def _sha256(path: Path, cancel_event: threading.Event | None) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            if cancel_event is not None and cancel_event.is_set():
                raise ServiceError("Hashing was cancelled.")
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _signature_info(paths: list[Path], cancel_event: threading.Event | None) -> dict[str, dict[str, str]]:
    result: dict[str, dict[str, str]] = {}
    candidates = [
        path
        for path in paths
        if path.suffix.casefold() in _PACKAGE_SUFFIXES | _SCRIPT_SUFFIXES | _UPDATE_SUFFIXES | {".dll"}
    ]
    if not candidates:
        return result
    runner = CommandRunner()
    chunk_size = 35
    for start in range(0, len(candidates), chunk_size):
        if cancel_event is not None and cancel_event.is_set():
            break
        chunk = candidates[start : start + chunk_size]
        quoted = ",".join(ps_quote(str(path)) for path in chunk)
        script = (
            f"$paths = @({quoted}); "
            "$out = @(); foreach ($p in $paths) { "
            "try { $s = Get-AuthenticodeSignature -LiteralPath $p -ErrorAction Stop; "
            "$item = Get-Item -LiteralPath $p -ErrorAction Stop; "
            "[pscustomobject]@{ Path = $p; Status = [string]$s.Status; "
            "Signer = if ($s.SignerCertificate) { [string]$s.SignerCertificate.Subject } else { '' }; "
            "Version = [string]$item.VersionInfo.FileVersion; Error = '' } } "
            "catch { [pscustomobject]@{ Path = $p; Status = 'UnknownError'; Signer = ''; "
            "Version = ''; Error = $_.Exception.Message } } }; "
            "$out | ConvertTo-Json -Depth 3 -Compress"
        )
        try:
            data = ps_json(runner, script, timeout=180, default=[])
        except Exception as exc:
            for path in chunk:
                result[str(path)] = {"status": "UnknownError", "signer": "", "version": "", "error": str(exc)}
            continue
        for row in as_list(data):
            if not isinstance(row, dict):
                continue
            path = str(row.get("Path") or "")
            if not path:
                continue
            result[path] = {
                "status": str(row.get("Status") or "Unknown"),
                "signer": str(row.get("Signer") or ""),
                "version": str(row.get("Version") or ""),
                "error": str(row.get("Error") or ""),
            }
    return result


def scan_repository(
    root: str | Path,
    *,
    progress: ProgressFn | None = None,
    cancel_event: threading.Event | None = None,
    compute_hashes: bool = True,
) -> RepositoryScan:
    require_windows("Repository scanning")
    directory = validate_repository_path(root)
    report = progress or (lambda _message: None)
    files, errors, truncated, cancelled = _iter_files(
        directory, max_files=MAX_FILES, max_depth=MAX_DEPTH, cancel_event=cancel_event
    )
    scan = RepositoryScan(root=str(directory), errors=errors, truncated=truncated, cancelled=cancelled)
    signatures = _signature_info(files, cancel_event) if files else {}
    total = len(files)
    for index, path in enumerate(files, start=1):
        if cancel_event is not None and cancel_event.is_set():
            scan.cancelled = True
            scan.errors.append("The repository scan was cancelled before all files were hashed.")
            break
        try:
            stat = path.stat(follow_symlinks=False)
        except OSError as exc:
            scan.errors.append(f"{path}: {exc}")
            continue
        item = RepositoryItem(
            path=str(path),
            relative_path=str(path.relative_to(directory)),
            name=path.name,
            kind=classify(path),
            size=stat.st_size,
            modified=datetime.fromtimestamp(stat.st_mtime).astimezone().isoformat(timespec="seconds"),
        )
        if compute_hashes:
            try:
                item.sha256 = _sha256(path, cancel_event)
            except Exception as exc:
                item.error = str(exc)
                scan.errors.append(f"Could not hash {path.name}: {exc}")
        signature = signatures.get(str(path))
        if signature:
            item.signature_status = signature.get("status", "unknown")
            item.signer = signature.get("signer", "")
            item.version = signature.get("version", "")
            if signature.get("error"):
                item.error = signature["error"]
        if item.is_script:
            item.warning = "Script files are inventoried only. WinFirst never executes repository scripts."
        elif item.kind == "Executable" and not item.signed:
            item.warning = "This executable is not signed by a trusted publisher. WinFirst will not launch it."
        scan.items.append(item)
        if index % 20 == 0:
            report(f"Hashed {index:,} of {total:,} files...")
    scan.items.sort(key=lambda row: (row.kind, row.name.casefold()))
    return scan


class RepositoryService:
    """Persists scan results for the Portable Toolkit page."""

    def __init__(self, db: Database) -> None:
        self.db = db

    def save(self, scan: RepositoryScan) -> None:
        if scan.cancelled:
            return
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        rows = []
        for item in scan.items:
            data = {
                "relative_path": item.relative_path,
                "signer": item.signer,
                "version": item.version,
                "modified": item.modified,
                "error": item.error,
                "warning": item.warning,
                "root": scan.root,
            }
            rows.append(
                (
                    item.path,
                    item.kind,
                    item.name,
                    item.version,
                    item.size,
                    item.sha256,
                    item.signature_status,
                    now,
                    now,
                    json.dumps(data, ensure_ascii=False),
                )
            )
        with self.db.transaction() as conn:
            conn.execute("DELETE FROM repository_items")
            conn.executemany(
                """
                INSERT INTO repository_items
                (path, kind, name, version, size, sha256, signature_status, created_at, updated_at, data_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )

    def load(self) -> list[RepositoryItem]:
        rows = self.db.query("SELECT * FROM repository_items ORDER BY kind, name COLLATE NOCASE")
        items: list[RepositoryItem] = []
        for row in rows:
            try:
                data = json.loads(row.get("data_json") or "{}")
            except json.JSONDecodeError:
                data = {}
            items.append(
                RepositoryItem(
                    path=str(row.get("path") or ""),
                    relative_path=str(data.get("relative_path") or ""),
                    name=str(row.get("name") or ""),
                    kind=str(row.get("kind") or "File"),
                    size=int(row.get("size") or 0),
                    sha256=str(row.get("sha256") or ""),
                    signature_status=str(row.get("signature_status") or "unknown"),
                    signer=str(data.get("signer") or ""),
                    version=str(row.get("version") or ""),
                    modified=str(data.get("modified") or ""),
                    error=str(data.get("error") or ""),
                    warning=str(data.get("warning") or ""),
                )
            )
        return items

    def clear(self) -> None:
        self.db.execute("DELETE FROM repository_items")


def export_inventory_csv(items: Iterable[RepositoryItem], destination: str | Path) -> Path:
    target = Path(destination)
    with target.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            ["Name", "Kind", "SizeBytes", "SHA256", "Signature", "Signer", "Version", "Modified", "RelativePath", "Warning"]
        )
        for item in items:
            writer.writerow(
                [
                    item.name,
                    item.kind,
                    item.size,
                    item.sha256,
                    item.signature_status,
                    item.signer,
                    item.version,
                    item.modified,
                    item.relative_path,
                    item.warning,
                ]
            )
    return target


def copy_checksum(items: Iterable[RepositoryItem], names: Iterable[str]) -> str:
    wanted = {name.casefold() for name in names}
    lines = [f"{item.sha256}  {item.relative_path}" for item in items if item.name.casefold() in wanted]
    return "\n".join(lines)
