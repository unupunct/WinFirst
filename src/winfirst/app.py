"""Application bootstrap for WinFirst."""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path
from typing import Any

from PySide6.QtCore import QTimer
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication, QMessageBox

from winfirst import APP_ID, APP_NAME, __version__
from winfirst.core import elevation_request, paths, settings_registry
from winfirst.core.logging_setup import setup_logging
from winfirst.ui.context import AppContext
from winfirst.ui.dialogs import run_operations
from winfirst.ui.main_window import MainWindow
from winfirst.ui.theme import apply_theme

_log = logging.getLogger("winfirst.app")


def _install_excepthook() -> None:
    def handler(exc_type, exc_value, exc_traceback) -> None:
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc_value, exc_traceback)
            return
        _log.critical("Unhandled exception", exc_info=(exc_type, exc_value, exc_traceback))
        try:
            QMessageBox.critical(
                None,
                f"{APP_NAME} encountered an error",
                "An unexpected error occurred. Details were written to the application log.\n\n"
                f"{exc_type.__name__}: {exc_value}",
            )
        except Exception:
            pass

    sys.excepthook = handler


def _handle_elevated_request(ctx: AppContext, window: MainWindow, request_path: str) -> None:
    path = Path(request_path)
    document = elevation_request.read_request(path)
    elevation_request.discard(path)
    if not document:
        QMessageBox.warning(
            window,
            "Elevation request not accepted",
            "The elevation request was missing, expired or invalid. Nothing was changed.\n\n"
            "Requests are only valid for a short time and must contain known declarative settings.",
        )
        return
    raw_settings = dict(document.get("settings") or {})
    specs: list[Any] = []
    skipped: list[str] = []
    for key, value in raw_settings.items():
        try:
            specs.append(settings_registry.build_spec(key, value))
        except Exception as exc:
            skipped.append(f"{key}: {exc}")
    if skipped:
        QMessageBox.information(
            window,
            "Some requested settings were skipped",
            "These entries could not be validated and will not be applied:\n\n"
            + "\n".join(skipped[:30]),
        )
    if not specs:
        QMessageBox.warning(
            window,
            "Nothing to apply",
            "The elevation request did not contain any supported settings.",
        )
        return
    run_operations(
        ctx,
        window,
        specs,
        title="Elevated settings request",
        profile_name=str(document.get("profile_name") or ""),
        intro=(
            "This request was created before WinFirst restarted with administrator rights. "
            "Every value was validated again. Review the changes before applying them."
        ),
    )


def _windows_app_id() -> None:
    if sys.platform != "win32":
        return
    try:
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID)  # type: ignore[attr-defined]
    except Exception:
        _log.debug("Could not set the Windows application ID", exc_info=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="winfirst",
        description=f"{APP_NAME} {__version__} - Windows setup, maintenance and diagnostics.",
    )
    parser.add_argument("--version", action="version", version=f"{APP_NAME} {__version__}")
    parser.add_argument(
        "--elevated-request",
        metavar="PATH",
        default="",
        help="Internal: validated request file written before an elevation restart.",
    )
    parser.add_argument("--debug", action="store_true", help="Enable verbose logging.")
    parser.add_argument(
        "--smoke-test",
        action="store_true",
        help="Construct the application and every page, then exit without touching Windows.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    setup_logging(logging.DEBUG if args.debug else logging.INFO)
    _log.info("%s %s starting (frozen=%s)", APP_NAME, __version__, getattr(sys, "frozen", False))
    _windows_app_id()

    app = QApplication(sys.argv[:1] + (argv or []))
    app.setApplicationName(APP_NAME)
    app.setApplicationDisplayName(APP_NAME)
    app.setOrganizationName(APP_NAME)
    app.setApplicationVersion(__version__)
    app.setStyle("Fusion")
    icon_file = paths.icon_path()
    if icon_file.is_file():
        app.setWindowIcon(QIcon(str(icon_file)))
    _install_excepthook()

    try:
        ctx = AppContext()
        ctx.pending_elevation = bool(args.elevated_request)
        apply_theme(app, ctx.current_theme())
    except Exception as exc:
        _log.critical("Application context failed", exc_info=True)
        QMessageBox.critical(
            None,
            f"{APP_NAME} could not start",
            "The application context could not be created. A diagnostic log was written.\n\n"
            f"{exc}",
        )
        return 1

    if args.smoke_test:
        from winfirst.ui.pages import PAGE_CLASSES, create_page

        failures = []
        for key in PAGE_CLASSES:
            try:
                page = create_page(key, ctx)
                page.deleteLater()
            except Exception as exc:
                failures.append(f"{key}: {exc}")
                _log.exception("Page smoke test failed for %s", key)
        if failures:
            print("UI smoke test failed:\n" + "\n".join(failures))
            ctx.db.close()
            return 2
        print(f"UI smoke test passed: {len(PAGE_CLASSES)} pages constructed.")
        ctx.db.close()
        return 0

    window = MainWindow(ctx)
    window.show()

    if args.elevated_request:
        QTimer.singleShot(
            300,
            lambda: _handle_elevated_request(ctx, window, args.elevated_request),
        )

    exit_code = app.exec()
    try:
        ctx.db.close()
    except Exception:
        _log.debug("Database close failed", exc_info=True)
    _log.info("%s exiting with code %s", APP_NAME, exit_code)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
