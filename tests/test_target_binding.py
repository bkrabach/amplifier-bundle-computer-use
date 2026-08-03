"""Unit tests for target binding (`target_binding.py`) -
`docs/coexistence.md` \u00a78.6: every multi-event operation is bound to a
delivery target at its start; before each elementary event, the injector
re-reads the current target, and aborts unconditionally on any change.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "modules" / "tool-computer-use"))

import pytest
from amplifier_module_tool_computer_use.target_binding import (
    TargetBinding,
    TargetChangedError,
)


def test_unbound_check_is_a_no_op():
    binding = TargetBinding()
    binding.check("anything")  # never raises when bind() was never called
    assert binding.status == "not_bound"


def test_bound_check_passes_when_target_unchanged():
    binding = TargetBinding()
    binding.bind("window-123")
    binding.check("window-123")
    binding.check("window-123")
    assert binding.status == "bound"


def test_bound_check_raises_on_any_target_change():
    binding = TargetBinding()
    binding.bind("window-123")
    with pytest.raises(TargetChangedError) as exc_info:
        binding.check("window-456")
    assert exc_info.value.expected_target == "window-123"
    assert exc_info.value.actual_target == "window-456"


def test_abort_is_unconditional_even_for_a_plausible_agent_caused_change():
    """\u00a78.6: the abort is unconditional and dumb ON PURPOSE - it trips even
    on a change the agent's OWN keystroke plausibly caused (an autocomplete
    popup). This test documents that there is no "was this us?" heuristic to
    bypass - any change at all raises."""
    binding = TargetBinding()
    binding.bind("main-window")
    # Simulates a dialog the agent's own Enter keypress opened - still a
    # target change, still raises.
    with pytest.raises(TargetChangedError):
        binding.check("popup-dialog")


def test_binding_to_none_is_unverified_not_a_silent_pass():
    """\u00a78.6: where target identity cannot be determined at all (macOS
    pending O9), binding is `unverified`, not silently treated as always-
    passing."""
    binding = TargetBinding()
    binding.bind(None)
    assert binding.status == "unverified"
    binding.check(None)  # consistent None -> None is not a "change"
    with pytest.raises(TargetChangedError):
        binding.check("suddenly-a-real-handle")


def test_release_clears_binding():
    binding = TargetBinding()
    binding.bind("window-1")
    binding.release()
    assert binding.status == "not_bound"
    binding.check("window-2")  # no-op again, since nothing is bound


def test_rebinding_updates_the_target():
    binding = TargetBinding()
    binding.bind("window-1")
    binding.bind("window-2")  # new operation starts, rebinds
    binding.check("window-2")
    with pytest.raises(TargetChangedError):
        binding.check("window-1")
