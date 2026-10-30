"""Headless UI load smoke test.

Constructs the real MainWindow and loads every page against the current
Windows installation.  It performs read-only page loads only; no Apply button
is clicked and no operation is executed.  Run with:

    $env:PYTHONPATH="src"; $env:QT_QPA_PLATFORM="offscreen"; python scripts\\ui_load_smoke.py
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from PySide6.QtWidgets import QApplication  # noqa: E402

from winfirst.ui.context import AppContext  # noqa: E402
from winfirst.ui.main_window import MainWindow  # noqa: E402
from winfirst.ui.pages import PAGE_TITLES  # noqa: E402
from winfirst.ui.theme import apply_theme  # noqa: E402
from winfirst.ui.workers import pool  # noqa: E402


def main() -> int:
    app = QApplication(["winfirst-ui-smoke"])
    ctx = AppContext()
    ctx.pending_elevation = True  # suppress the welcome dialog in this test
    apply_theme(app, ctx.current_theme())
    window = MainWindow(ctx)
    window.show()
    app.processEvents()

    failures: list[str] = []
    for key in PAGE_TITLES:
        try:
            window.navigate(key)
            deadline = time.monotonic() + 60
            idle_since = None
            while time.monotonic() < deadline:
                app.processEvents()
                busy = pool().activeThreadCount() > 0
                if busy:
                    idle_since = None
                elif idle_since is None:
                    idle_since = time.monotonic()
                elif time.monotonic() - idle_since > 1.0:
                    break
                time.sleep(0.05)
            app.processEvents()
            page = window.pages.get(key)
            banner = getattr(page, "error_banner", None)
            if banner is not None and banner.isVisible() and banner.text():
                print(f"WARN {key}: {banner.text()}")
            print(f"OK   {key}")
        except Exception as exc:  # noqa: BLE001
            failures.append(f"{key}: {type(exc).__name__}: {exc}")
            print(f"FAIL {key}: {exc}")

    window.close()
    app.processEvents()
    ctx.db.close()
    if failures:
        print("\nUI load failures:")
        for failure in failures:
            print(" -", failure)
        return 2
    print(f"\nLoaded {len(PAGE_TITLES)} pages without an unhandled exception.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
