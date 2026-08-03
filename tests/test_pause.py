"""Unit tests for pause/cancel semantics (`pause.py`) -
`docs/coexistence.md` \u00a78.1-\u00a78.4: the injector owns pause state; only
a human-sourced call may set or clear it; drags interrupted mid-flight are
reported truthfully rather than silently completed or reverted.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "modules" / "tool-computer-use"))

import pytest
from amplifier_module_tool_computer_use.pause import (
    DragState,
    PauseController,
    PausedError,
)


def test_human_source_can_set_and_clear_pause():
    pause = PauseController()
    pause.set("overlay_click", reason="human clicked pause")
    assert pause.is_paused is True
    pause.clear("overlay_click")
    assert pause.is_paused is False


def test_controller_source_cannot_set_pause():
    """\u00a78.1: there is no controller-settable `pause` op - only a human
    source may set it."""
    pause = PauseController()
    with pytest.raises(PermissionError):
        pause.set("controller")
    assert pause.is_paused is False


def test_controller_source_cannot_clear_a_human_set_pause():
    """\u00a78.1: "Only the human clears a human-set pause." A buggy or
    compromised controller must not be able to unpause itself."""
    pause = PauseController()
    pause.set("overlay_click")
    with pytest.raises(PermissionError):
        pause.clear("controller")
    assert pause.is_paused is True  # unchanged - the attempt did not clear it


def test_check_raises_paused_error_with_setter_and_reason():
    pause = PauseController()
    pause.set("overlay_click", reason="taking a break")
    with pytest.raises(PausedError) as exc_info:
        pause.check(progress="typed 47 of 200 characters")
    assert exc_info.value.source == "overlay_click"
    assert exc_info.value.reason == "taking a break"
    assert exc_info.value.progress == "typed 47 of 200 characters"


def test_check_is_a_no_op_when_not_paused():
    pause = PauseController()
    pause.check()  # must not raise


# -- drag interruption (\u00a78.4): reported truthfully, not silently fixed -----


def test_drag_end_reports_start_and_actual_position_not_intended_endpoint():
    drag = DragState()
    drag.begin((10, 10))
    drag.update((50, 60))  # the drag was interrupted here, short of its target
    report = drag.end()
    assert report["drag_interrupted"] is True
    assert report["start"] == (10, 10)
    assert report["ended_at"] == (50, 60)
    assert drag.active is False


def test_drag_state_resets_after_end():
    drag = DragState()
    drag.begin((0, 0))
    drag.update((5, 5))
    drag.end()
    assert drag.start is None
    assert drag.last_position is None
