"""Development entry point for WinFirst.

For an installed copy use the packaged executable, or ``python -m winfirst``
after installing the package in editable mode.
"""
from __future__ import annotations

import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parent / "src"
if _SRC.is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from winfirst.app import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
