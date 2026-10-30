"""Background task plumbing.

Every service call that can touch the registry, Win32 APIs or PowerShell runs
on a worker thread.  Results return to the GUI thread through Qt signals, so a
page never blocks the event loop while Windows works.
"""
from __future__ import annotations

import logging
import traceback
from typing import Any, Callable

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal, Slot

_log = logging.getLogger("winfirst.ui.workers")

TaskFn = Callable[[], Any]
ResultFn = Callable[[Any], None]
ErrorFn = Callable[[str], None]
ProgressFn = Callable[[str], None]


class WorkerSignals(QObject):
    progress = Signal(str)
    finished = Signal(object)
    failed = Signal(str)
    done = Signal()


class Worker(QRunnable):
    """Run one callable and report its result on the GUI thread."""

    def __init__(self, fn: TaskFn) -> None:
        super().__init__()
        self.fn = fn
        self.signals = WorkerSignals()
        self.setAutoDelete(True)

    @Slot()
    def run(self) -> None:  # pragma: no cover - exercised through the GUI
        try:
            result = self.fn()
        except Exception as exc:
            _log.debug("Background task failed", exc_info=True)
            detail = str(exc).strip() or type(exc).__name__
            try:
                self.signals.failed.emit(detail)
            except RuntimeError:
                return
        else:
            try:
                self.signals.finished.emit(result)
            except RuntimeError:
                return
        finally:
            try:
                self.signals.done.emit()
            except RuntimeError:
                pass


def pool() -> QThreadPool:
    instance = QThreadPool.globalInstance()
    if instance.maxThreadCount() < 4:
        instance.setMaxThreadCount(6)
    return instance


def run_async(
    owner: Any,
    fn: TaskFn,
    on_done: ResultFn,
    *,
    on_error: ErrorFn | None = None,
    on_progress: ProgressFn | None = None,
) -> Worker:
    """Start ``fn`` in the thread pool and keep the worker alive on ``owner``."""
    worker = Worker(fn)
    if on_progress is not None:
        worker.signals.progress.connect(on_progress)
    worker.signals.finished.connect(on_done)
    if on_error is not None:
        worker.signals.failed.connect(on_error)

    workers = getattr(owner, "_workers", None)
    if workers is None:
        workers = set()
        setattr(owner, "_workers", workers)
    workers.add(worker)

    def _cleanup() -> None:
        workers.discard(worker)

    worker.signals.done.connect(_cleanup)
    pool().start(worker)
    return worker


def run_with_progress(
    fn: Callable[[ProgressFn], Any],
    *,
    on_done: ResultFn,
    on_error: ErrorFn | None = None,
    on_progress: ProgressFn | None = None,
) -> Worker:
    """Start a callable that accepts a progress callback."""
    worker = Worker(lambda: fn(worker.signals.progress.emit))  # type: ignore[has-type]
    if on_progress is not None:
        worker.signals.progress.connect(on_progress)
    worker.signals.finished.connect(on_done)
    if on_error is not None:
        worker.signals.failed.connect(on_error)
    pool().start(worker)
    return worker


def format_exception(exc: BaseException) -> str:
    return "".join(traceback.format_exception_only(type(exc), exc)).strip()
