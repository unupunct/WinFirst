"""Live acceptance test support.

These tests are skipped unless ``WINFIRST_LIVE=1`` is set, which the
acceptance runner does only after the operator confirms a disposable VM.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone

import pytest

RUN_LIVE = os.environ.get("WINFIRST_LIVE") == "1"


def pytest_collection_modifyitems(config, items) -> None:
    if RUN_LIVE:
        return
    skip = pytest.mark.skip(
        reason=(
            "Live acceptance test: run scripts/acceptance/run_acceptance.ps1 "
            "on a disposable VM (sets WINFIRST_LIVE=1)."
        )
    )
    for item in items:
        if "live" in item.keywords:
            item.add_marker(skip)


@pytest.fixture
def evidence():
    """Append observed state to the evidence file when one is configured."""
    path = os.environ.get("WINFIRST_ACCEPTANCE_EVIDENCE", "").strip()

    def record(scenario: str, **data) -> None:
        payload = {
            "scenario": scenario,
            "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            **data,
        }
        if path:
            with open(path, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")

    return record
