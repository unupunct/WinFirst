r"""Capture README screenshots from the real UI using a throwaway data folder.

Only pages that show no machine-specific data (catalog, profiles) are captured,
and LOCALAPPDATA is pointed at a temporary folder so no real history is shown.

    $env:PYTHONPATH="src"; $env:SHOT_DIR="C:\winfirst-demo"; python scripts\make_screenshot.py
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# The data path is shown in the sidebar; set SHOT_DIR to a neutral folder so the
# Windows user name does not appear in the picture.
_base = os.environ.get("SHOT_DIR")
if _base:
    os.makedirs(_base, exist_ok=True)
os.environ["LOCALAPPDATA"] = tempfile.mkdtemp(prefix="demo-", dir=_base or None)
sys.path.insert(0, str(ROOT / "src"))

from PySide6.QtWidgets import QApplication  # noqa: E402

from winfirst.ui.context import AppContext  # noqa: E402
from winfirst.ui.main_window import MainWindow  # noqa: E402
from winfirst.ui.theme import apply_theme  # noqa: E402
from winfirst.ui.workers import pool  # noqa: E402

PAGES = {"software": "software.png", "profiles": "profiles.png"}


def main() -> int:
    app = QApplication(["winfirst-shots"])
    ctx = AppContext()
    ctx.pending_elevation = True
    apply_theme(app, ctx.current_theme())
    window = MainWindow(ctx)
    window.resize(1280, 800)
    window.show()
    out = ROOT / "docs" / "images"
    out.mkdir(parents=True, exist_ok=True)
    for key, name in PAGES.items():
        window.navigate(key)
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            app.processEvents()
            if pool().activeThreadCount() == 0:
                break
            time.sleep(0.05)
        for _ in range(20):
            app.processEvents()
            time.sleep(0.05)
        window.grab().save(str(out / name))
        print("saved", out / name)
    window.close()
    ctx.db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
