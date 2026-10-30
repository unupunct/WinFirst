from __future__ import annotations

from pathlib import Path

import pytest

from winfirst.core.schema_validation import ProfileValidationError
from winfirst.services import software_catalog_service as catalog_service


def test_builtin_catalog_loads_and_is_valid() -> None:
    catalog = catalog_service.load_catalog()
    assert len(catalog.packages) >= 20
    ids = [package.id for package in catalog.packages]
    assert len(ids) == len(set(ids))
    assert all(package.name for package in catalog.packages)
    assert catalog.categories()


def test_catalog_find_and_parse() -> None:
    catalog = catalog_service.load_catalog()
    first = catalog.packages[0]
    assert catalog.find(first.id) is first
    assert catalog.find("does.not.exist") is None


def test_selection_roundtrip(tmp_path: Path) -> None:
    catalog = catalog_service.load_catalog()
    chosen = [package.id for package in catalog.packages[:3]]
    path = catalog_service.save_selection(tmp_path / "selection.json", chosen, catalog)
    assert path.is_file()
    packages, missing = catalog_service.load_selection(path, catalog)
    assert [package.id for package in packages] == chosen
    assert missing == []


def test_selection_reports_missing_packages(tmp_path: Path) -> None:
    catalog = catalog_service.load_catalog()
    path = tmp_path / "selection.json"
    path.write_text(
        '{"schema_version": 1, "packages": ["not.in.catalog"]}', encoding="utf-8"
    )
    packages, missing = catalog_service.load_selection(path, catalog)
    assert packages == []
    assert any("not.in.catalog" in warning for warning in missing)


def test_invalid_catalog_copy_is_rejected(tmp_path: Path) -> None:
    source = tmp_path / "catalog.json"
    source.write_text(
        '{"schema_version": 1, "packages": [{"id": "A.B", "name": "X", "source": "bad"}]}',
        encoding="utf-8",
    )
    with pytest.raises(ValueError):
        catalog_service.import_catalog(source)
    assert not catalog_service.user_catalog_path().exists()


def test_export_catalog_writes_a_copy(tmp_path: Path) -> None:
    destination = tmp_path / "exported.json"
    path = catalog_service.export_catalog(destination)
    assert path == destination
    assert destination.is_file()
    assert '"packages"' in destination.read_text(encoding="utf-8")


def test_reset_catalog_removes_override(tmp_path: Path) -> None:
    override = catalog_service.user_catalog_path()
    override.parent.mkdir(parents=True, exist_ok=True)
    override.write_text("{}", encoding="utf-8")
    assert override.exists()
    catalog_service.reset_catalog()
    assert not override.exists()
