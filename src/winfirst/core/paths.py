"""Filesystem locations used by WinFirst.

Writable state (database, logs, reports) lives in the per-user local
application data directory by default.  A portable data directory can be
requested explicitly with the ``WINFIRST_PORTABLE_DATA=1`` environment
variable or a ``portable-data.txt`` marker next to the executable.  A writable
directory is verified before it is used; otherwise the per-user location is
kept so the application never writes into a protected installation folder.

No secrets are stored in these locations.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from winfirst import APP_NAME

_MARKER = "portable-data.txt"


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def install_dir() -> Path:
    """Directory containing the executable / source checkout."""
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[3]


def resources_dir() -> Path:
    """Read-only packaged resources (styles, catalogs, templates)."""
    candidates: list[Path] = []
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        candidates.append(Path(meipass) / "winfirst" / "resources")
        candidates.append(Path(meipass) / "resources")
    candidates.append(Path(__file__).resolve().parents[1] / "resources")
    for candidate in candidates:
        if candidate.is_dir():
            return candidate
    return candidates[-1]


def icon_path() -> Path:
    """Application icon, packaged with the read-only resources."""
    return resources_dir() / "icons" / "winfirst.ico"


def _default_data_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA")
    if base:
        return Path(base) / APP_NAME
    return Path.home() / "AppData" / "Local" / APP_NAME


def _is_writable(path: Path) -> bool:
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".write-probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)
        return True
    except OSError:
        return False


def portable_mode_active() -> bool:
    env = os.environ.get("WINFIRST_PORTABLE_DATA", "").strip().lower()
    if env in {"1", "true", "yes", "on"}:
        return True
    try:
        return (install_dir() / _MARKER).is_file()
    except OSError:
        return False


def data_dir() -> Path:
    """Writable application data directory (created on demand)."""
    override = os.environ.get("WINFIRST_DATA_DIR")
    if override:
        candidate = Path(override).expanduser()
        if _is_writable(candidate):
            return candidate
    if portable_mode_active():
        candidate = install_dir() / "data"
        if _is_writable(candidate):
            return candidate
    candidate = _default_data_dir()
    candidate.mkdir(parents=True, exist_ok=True)
    return candidate


def logs_dir() -> Path:
    path = data_dir() / "logs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def reports_dir() -> Path:
    path = data_dir() / "reports"
    path.mkdir(parents=True, exist_ok=True)
    return path


def bundles_dir() -> Path:
    path = data_dir() / "bundles"
    path.mkdir(parents=True, exist_ok=True)
    return path


def cache_dir() -> Path:
    path = data_dir() / "cache"
    path.mkdir(parents=True, exist_ok=True)
    return path


def exports_dir() -> Path:
    path = data_dir() / "exports"
    path.mkdir(parents=True, exist_ok=True)
    return path


def database_path() -> Path:
    return data_dir() / "winfirst.db"


def elevation_request_path() -> Path:
    path = data_dir() / "elevation"
    path.mkdir(parents=True, exist_ok=True)
    return path / "pending-request.json"


def log_file() -> Path:
    return logs_dir() / "winfirst.log"


def is_portable() -> bool:
    """True when the active data directory is the portable one."""
    try:
        return data_dir().resolve() == (install_dir() / "data").resolve()
    except OSError:
        return False


def describe_locations() -> dict[str, str]:
    return {
        "install": str(install_dir()),
        "data": str(data_dir()),
        "logs": str(logs_dir()),
        "reports": str(reports_dir()),
        "database": str(database_path()),
        "portable_mode": "yes" if is_portable() else "no",
    }
