"""Shared test isolation for process-wide module state.

`amplifier_module_tool_computer_use._announcement_decisions` (the one-
disclosure-decision-per-physical-channel cache added to fix the double-
announce defect - see `tests/test_announcement_dedup.py`) is deliberately
process-lifetime state, exactly like `shared_transport._registry`
(`tests/test_shared_transport.py` already clears that one the same way).
Without this, one test's fake backend "consuming" a channel key leaks into
every later test that happens to construct a fake with the same `.name` -
most of the existing announcement tests use `_FakeRemoteBackend("macos")`,
which all hash to the same channel key by design (that IS the behavior
under test), so isolation between test functions has to be enforced here,
not left to chance.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "modules" / "tool-computer-use"))

import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _clear_announcement_decisions():
    import amplifier_module_tool_computer_use as cu

    cu._announcement_decisions.clear()
    yield
    cu._announcement_decisions.clear()
