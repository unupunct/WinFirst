# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller build definition for WinFirst.

Build with ``scripts/build_portable.ps1`` (one-file by default).  Set
``WINFIRST_ONEFILE=0`` to produce an onedir build instead.
"""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(SPECPATH)  # injected by PyInstaller
ICON = ROOT / "src" / "winfirst" / "resources" / "icons" / "winfirst.ico"
VERSION_FILE = ROOT / "installer" / "version_info.txt"
RESOURCES = ROOT / "src" / "winfirst" / "resources"

datas = [(str(RESOURCES), "winfirst/resources")]

# Pages are imported lazily through importlib, which static analysis cannot see.
# Keep this list in sync with winfirst.ui.pages.PAGE_CLASSES.
PAGE_MODULES = [
    "dashboard",
    "quick_setup",
    "windows_settings",
    "power",
    "cleanup",
    "software",
    "system_health",
    "drivers",
    "network",
    "repair",
    "privacy",
    "explorer",
    "startup",
    "services",
    "storage",
    "printers",
    "users",
    "profiles",
    "drift",
    "deployment",
    "portable",
    "history",
    "reports",
    "app_settings",
]
hiddenimports = [f"winfirst.ui.pages.{name}" for name in PAGE_MODULES]

a = Analysis(
    [str(ROOT / "main.py")],
    pathex=[str(ROOT / "src")],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # Keep the bundle focused; these are development-only or unused by the app.
    excludes=[
        "tkinter",
        "pytest",
        "_pytest",
        "unittest",
        "doctest",
        "pydoc",
        "setuptools",
        "pip",
    ],
    noarchive=False,
)

pyz = PYZ(a.pure)

onefile = os.environ.get("WINFIRST_ONEFILE", "1").strip().lower() not in {"0", "false", "no"}

if onefile:
    exe = EXE(
        pyz,
        a.scripts,
        a.binaries,
        a.datas,
        [],
        name="WinFirst",
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=False,
        runtime_tmpdir=None,
        console=False,
        disable_windowed_traceback=False,
        argv_emulation=False,
        target_arch=None,
        codesign_identity=None,
        entitlements_file=None,
        icon=str(ICON) if ICON.is_file() else None,
        version=str(VERSION_FILE) if VERSION_FILE.is_file() else None,
    )
else:
    exe = EXE(
        pyz,
        a.scripts,
        [],
        exclude_binaries=True,
        name="WinFirst",
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=False,
        console=False,
        disable_windowed_traceback=False,
        argv_emulation=False,
        target_arch=None,
        codesign_identity=None,
        entitlements_file=None,
        icon=str(ICON) if ICON.is_file() else None,
        version=str(VERSION_FILE) if VERSION_FILE.is_file() else None,
    )
    coll = COLLECT(
        exe,
        a.binaries,
        a.datas,
        strip=False,
        upx=False,
        upx_exclude=[],
        name="WinFirst",
    )
