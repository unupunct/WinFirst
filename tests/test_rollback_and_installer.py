import hashlib

import pytest

from winfirst.core import rollback_manager, settings_registry
from winfirst.services import software_catalog_service as catalog
from winfirst.services import software_install_service as installer


def test_local_catalog_entry_requires_sha256():
    item = {
        "id": "Vendor.App", "name": "App", "source": "local",
        "installer": r"C:\installers\app.exe", "verify": {"type": "file", "path": r"C:\app.exe"},
    }
    assert any("sha256" in e for e in catalog.validate_package_details(item, 0))
    item["sha256"] = "a" * 64
    assert not any("sha256" in e for e in catalog.validate_package_details(item, 0))


def test_installer_hash_mismatch_is_refused(tmp_path):
    exe = tmp_path / "setup.exe"
    exe.write_bytes(b"payload")
    package = catalog.CatalogPackage(id="V.A", name="A", source="local", sha256=hashlib.sha256(b"payload").hexdigest())
    installer._verify_installer_hash(package, exe)
    package.sha256 = "0" * 64
    with pytest.raises(installer.ServiceError):
        installer._verify_installer_hash(package, exe)
    package.sha256 = ""
    with pytest.raises(installer.ServiceError):
        installer._verify_installer_hash(package, exe)


def test_setting_rollback_refuses_when_value_changed(monkeypatch):
    class Detected:
        supported = True
        value = False

    class Definition:
        label = "Test setting"

        @staticmethod
        def detect():
            return Detected()

        @staticmethod
        def format_value(value):
            return str(value)

    monkeypatch.setattr(settings_registry, "get_definition", lambda key: Definition)
    with pytest.raises(ValueError, match="has changed since"):
        rollback_manager._setting_handler({"key": "k", "previous": True, "applied": True})


def test_software_install_rollback_is_registered():
    assert "software_install" in rollback_manager._HANDLERS
