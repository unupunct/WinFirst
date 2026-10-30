"""Validated software catalog and selection files.

The catalog is declarative data.  It cannot contain scripts or shell commands.
Winget entries are installed only through winget's argument-based CLI; local
entries must name an existing .exe/.msi installer and an independent
verification method.
"""
from __future__ import annotations

import json
import os
import re
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from winfirst.core import paths
from winfirst.core.schema_validation import validate_software_catalog
from winfirst.core.schema_validation import ValidationResult, clean_text

MAX_CATALOG_BYTES = 2 * 1024 * 1024
MAX_SELECTION_BYTES = 128 * 1024
_PACKAGE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+\-]{0,199}$")
_ARG_RE = re.compile(r"^[^\x00-\x1f\x7f]{1,512}$")


@dataclass
class CatalogPackage:
    id: str
    name: str
    publisher: str = ""
    category: str = ""
    source: str = "winget"
    description: str = ""
    license: str = ""
    homepage: str = ""
    silent_args: list[str] = field(default_factory=list)
    installer: str = ""
    sha256: str = ""
    verify: dict[str, str] = field(default_factory=dict)
    optional: bool = True
    version: str = ""

    @property
    def source_label(self) -> str:
        return "Winget" if self.source == "winget" else "Local installer"

    @property
    def display_version(self) -> str:
        return self.version or "Latest"

    @property
    def verify_summary(self) -> str:
        kind = self.verify.get("type", "")
        if kind == "winget_id":
            return f"winget list shows {self.verify.get('value', self.id)}"
        if kind == "uninstall_name":
            return f"Uninstall registry contains '{self.verify.get('value', '')}'"
        if kind == "file":
            return f"File exists: {self.verify.get('path', '')}"
        return "No independent verification configured"

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "id": self.id,
            "name": self.name,
            "source": self.source,
            "optional": self.optional,
        }
        for key in ("publisher", "category", "description", "license", "homepage", "version"):
            value = getattr(self, key)
            if value:
                data[key] = value
        if self.silent_args:
            data["silent_args"] = list(self.silent_args)
        if self.installer:
            data["installer"] = self.installer
        if self.sha256:
            data["sha256"] = self.sha256
        if self.verify:
            data["verify"] = dict(self.verify)
        return data


@dataclass
class SoftwareCatalog:
    packages: list[CatalogPackage] = field(default_factory=list)
    path: str = ""
    warnings: list[str] = field(default_factory=list)
    is_user_override: bool = False

    def find(self, package_id: str) -> CatalogPackage | None:
        for package in self.packages:
            if package.id.casefold() == package_id.casefold():
                return package
        return None

    def categories(self) -> list[str]:
        return sorted({package.category for package in self.packages if package.category}, key=str.casefold)


def builtin_catalog_path() -> Path:
    return paths.resources_dir() / "catalog" / "software.json"


def user_catalog_path() -> Path:
    return paths.data_dir() / "catalog" / "software.json"


def active_catalog_path() -> Path:
    override = user_catalog_path()
    if override.is_file():
        return override
    return builtin_catalog_path()


def _read_json(path: Path, max_bytes: int) -> Any:
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise ValueError(f"Could not read {path.name}: {exc}") from exc
    if size > max_bytes:
        raise ValueError(f"{path.name} is larger than the allowed {max_bytes // 1024} KB.")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except UnicodeDecodeError as exc:
        raise ValueError(f"{path.name} is not valid UTF-8: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"{path.name} is not valid JSON: line {exc.lineno}, column {exc.colno}: {exc.msg}") from exc


def validate_package_details(item: dict[str, Any], index: int) -> list[str]:
    errors: list[str] = []
    source = item.get("source")
    if source == "winget":
        package_id = str(item.get("id") or "")
        if not _PACKAGE_ID_RE.match(package_id):
            errors.append(f"packages[{index}]: winget id is invalid")
        if item.get("silent_args"):
            errors.append(
                f"packages[{index}]: winget entries cannot define silent_args (they could override installer arguments)"
            )
        if item.get("installer"):
            errors.append(f"packages[{index}]: winget entries cannot define a local installer")
    elif source == "local":
        installer = str(item.get("installer") or "")
        if not installer or not os.path.isabs(os.path.expandvars(os.path.expanduser(installer))):
            errors.append(f"packages[{index}]: local installer path must be absolute")
        elif not installer.lower().endswith((".exe", ".msi")):
            errors.append(f"packages[{index}]: local installer must be .exe or .msi")
        if not re.fullmatch(r"[0-9a-fA-F]{64}", str(item.get("sha256") or "")):
            errors.append(f"packages[{index}]: local entries require a 64-character sha256 of the installer")
        verify = item.get("verify")
        if not isinstance(verify, dict) or not verify.get("type"):
            errors.append(f"packages[{index}]: local entries require a verify object with a type")
        args = item.get("silent_args", [])
        if not isinstance(args, list) or len(args) > 30:
            errors.append(f"packages[{index}]: silent_args must be an array of at most 30 strings")
        else:
            for arg in args:
                if not isinstance(arg, str) or not _ARG_RE.match(arg):
                    errors.append(f"packages[{index}]: silent argument contains unsupported characters")
                    break
    verify = item.get("verify")
    if verify is not None:
        if not isinstance(verify, dict):
            errors.append(f"packages[{index}]: verify must be an object")
        else:
            kind = verify.get("type")
            if kind == "file":
                if not str(verify.get("path") or "").strip():
                    errors.append(f"packages[{index}]: file verification needs a path")
            elif kind == "uninstall_name":
                if not str(verify.get("value") or "").strip():
                    errors.append(f"packages[{index}]: uninstall_name verification needs a value")
            elif kind == "winget_id":
                if not _PACKAGE_ID_RE.match(str(verify.get("value") or "")):
                    errors.append(f"packages[{index}]: winget_id verification value is invalid")
            elif kind:
                errors.append(f"packages[{index}]: unsupported verification type {kind!r}")
    for key, limit in (("publisher", 200), ("category", 60), ("description", 2000), ("license", 120), ("homepage", 500), ("version", 80)):
        value = item.get(key, "")
        if value and not isinstance(value, str):
            errors.append(f"packages[{index}].{key} must be text")
        elif isinstance(value, str) and len(value) > limit:
            errors.append(f"packages[{index}].{key} is too long")
    homepage = str(item.get("homepage") or "")
    if homepage and not homepage.lower().startswith(("https://", "http://")):
        errors.append(f"packages[{index}].homepage must be an http(s) URL")
    if not isinstance(item.get("optional", True), bool):
        errors.append(f"packages[{index}].optional must be true or false")
    return errors


def parse_catalog(data: Any, *, path: str = "", warnings: list[str] | None = None) -> SoftwareCatalog:
    validation = validate_software_catalog(data)
    errors = list(validation.errors)
    if validation.ok and validation.document:
        for index, item in enumerate(validation.document.get("packages", [])):
            errors.extend(validate_package_details(item, index))
    if errors:
        raise ValueError("; ".join(errors[:8]))
    catalog = SoftwareCatalog(path=path, warnings=list(warnings or []))
    for item in (validation.document or {}).get("packages", []):
        source = str(item.get("source") or "winget")
        package_id = str(item.get("id") or "")
        verify = item.get("verify")
        if not verify and source == "winget":
            verify = {"type": "winget_id", "value": package_id}
        catalog.packages.append(
            CatalogPackage(
                id=package_id,
                name=str(item.get("name") or ""),
                publisher=str(item.get("publisher") or ""),
                category=str(item.get("category") or ""),
                source=source,
                description=str(item.get("description") or ""),
                license=str(item.get("license") or ""),
                homepage=str(item.get("homepage") or ""),
                silent_args=[str(arg) for arg in item.get("silent_args", [])],
                installer=str(item.get("installer") or ""),
                sha256=str(item.get("sha256") or "").lower(),
                verify={str(key): str(value) for key, value in (verify or {}).items()},
                optional=bool(item.get("optional", True)),
                version=str(item.get("version") or ""),
            )
        )
    catalog.packages.sort(key=lambda item: (item.category.casefold(), item.name.casefold()))
    return catalog


def load_catalog(path: str | Path | None = None) -> SoftwareCatalog:
    candidate = Path(path) if path else active_catalog_path()
    if not candidate.is_file():
        raise ValueError(f"Software catalog not found: {candidate}")
    data = _read_json(candidate, MAX_CATALOG_BYTES)
    warnings: list[str] = []
    if candidate == user_catalog_path():
        warnings.append("Using the editable user catalog override.")
    catalog = parse_catalog(data, path=str(candidate), warnings=warnings)
    catalog.is_user_override = candidate == user_catalog_path()
    return catalog


def import_catalog(source_path: str | Path) -> SoftwareCatalog:
    source = Path(source_path)
    data = _read_json(source, MAX_CATALOG_BYTES)
    parse_catalog(data, path=str(source))
    destination = user_catalog_path()
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.is_file():
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        shutil.copy2(destination, destination.with_name(f"software.backup-{stamp}.json"))
    shutil.copy2(source, destination)
    return load_catalog(destination)


def reset_catalog() -> None:
    override = user_catalog_path()
    if override.is_file():
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        shutil.copy2(override, override.with_name(f"software.backup-{stamp}.json"))
        override.unlink()


def export_catalog(destination: str | Path) -> Path:
    catalog = load_catalog()
    target = Path(destination)
    data = {
        "schema_version": 1,
        "packages": [package.to_dict() for package in catalog.packages],
    }
    target.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    return target


def save_selection(destination: str | Path, package_ids: list[str], catalog: SoftwareCatalog | None = None) -> Path:
    catalog = catalog or load_catalog()
    cleaned: list[str] = []
    for package_id in package_ids:
        package = catalog.find(package_id)
        if package is None:
            raise ValueError(f"Unknown package in selection: {package_id}")
        if package.id not in cleaned:
            cleaned.append(package.id)
    target = Path(destination)
    document = {
        "schema_version": 1,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "packages": cleaned,
    }
    target.write_text(json.dumps(document, indent=2, ensure_ascii=False), encoding="utf-8")
    return target


def load_selection(source_path: str | Path, catalog: SoftwareCatalog | None = None) -> tuple[list[CatalogPackage], list[str]]:
    catalog = catalog or load_catalog()
    data = _read_json(Path(source_path), MAX_SELECTION_BYTES)
    if not isinstance(data, dict):
        raise ValueError("The selection file must be a JSON object.")
    if data.get("schema_version") != 1:
        raise ValueError("The selection file schema_version must be 1.")
    ids = data.get("packages")
    if not isinstance(ids, list) or len(ids) > 500:
        raise ValueError("The selection file must contain an array of at most 500 package identifiers.")
    packages: list[CatalogPackage] = []
    warnings: list[str] = []
    for item in ids:
        if not isinstance(item, str) or not _PACKAGE_ID_RE.match(item):
            warnings.append(f"Ignored an invalid package identifier: {item!r}")
            continue
        package = catalog.find(item)
        if package is None:
            warnings.append(f"Package is not in the current catalog: {item}")
            continue
        if not any(existing.id.casefold() == package.id.casefold() for existing in packages):
            packages.append(package)
    return packages, warnings
