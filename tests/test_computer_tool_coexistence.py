"""Unit tests: TASK 2 - the coexistence guard now wraps every mutating action
in `ComputerTool._run` (`_guard_write`), not just `type_text`.

No real backend, no real display server - a minimal fake `Backend` records
every call it receives so we can assert the backend method is (or is not)
invoked, and a real `CoexistenceGuard` (wrapped by a small recording proxy)
proves the before/after discipline actually fires around each action, and
exactly once per composite (not once per constituent click/motion) - see
`docs/coexistence.md` §8.4.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "modules" / "tool-computer-use"))

import pytest
from amplifier_module_tool_computer_use import ComputerTool
from amplifier_module_tool_computer_use.backend import (
    BackendError,
    ScreenGeometry,
    WindowList,
)
from amplifier_module_tool_computer_use.coexistence_guard import (
    CoexistenceGuard,
    HaltedError,
)
from amplifier_module_tool_computer_use.presence import (
    Confidence,
    PresenceMonitor,
    PresenceSnapshot,
    PresenceState,
)


class _FakeBackend:
    """Records every call; supports every `Backend` method `_run` may
    invoke across the actions under test."""

    name = "linux-x11"

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def screen_geometry(self) -> ScreenGeometry:
        return ScreenGeometry(800, 600, 0, 0)

    def list_monitors(self):
        raise BackendError("no monitor enumeration on this fake")

    def capture(self, region=None) -> bytes:  # pragma: no cover - unused here
        raise AssertionError("capture() not exercised by these tests")

    def cursor_position(self) -> tuple[int, int]:
        return (0, 0)

    def move(self, x, y) -> None:
        self.calls.append(("move", x, y))

    def click(self, x, y, button="left", count=1) -> None:
        self.calls.append(("click", x, y, button, count))

    def mouse_down(self, x, y, button="left") -> None:
        self.calls.append(("mouse_down", x, y, button))

    def mouse_up(self, x, y, button="left") -> None:
        self.calls.append(("mouse_up", x, y, button))

    def drag(self, start, end) -> None:
        self.calls.append(("drag", start, end))

    def scroll(self, x, y, direction, amount) -> None:
        self.calls.append(("scroll", x, y, direction, amount))

    def key(self, combo) -> None:
        self.calls.append(("key", combo))

    def hold_key(self, combo, duration) -> None:
        self.calls.append(("hold_key", combo, duration))

    def type_text(self, text, guard=None) -> None:
        self.calls.append(("type_text", text))

    def list_windows(self) -> WindowList:
        return WindowList([], None)

    def focus_window(self, handle) -> None:
        self.calls.append(("focus_window", handle))

    def get_clipboard(self) -> str:
        return "clip"

    def set_clipboard(self, text) -> None:
        self.calls.append(("set_clipboard", text))

    def close(self) -> None:  # pragma: no cover
        pass


class _RecordingGuard:
    """Wraps a real `CoexistenceGuard`, recording the sequence of calls
    `_guard_write` makes into it - proves before/after discipline actually
    fires, and fires exactly ONCE per composite action, not per constituent
    sub-event (§8.4)."""

    def __init__(self, real: CoexistenceGuard) -> None:
        self._real = real
        self.calls: list[object] = []

    def check_start_permission(self) -> None:
        self.calls.append("check_start_permission")
        self._real.check_start_permission()

    def bind_target(self) -> None:
        self.calls.append("bind_target")
        self._real.bind_target()

    def before_event(self, *, coord=None, **_kwargs) -> None:
        self.calls.append(("before_event", coord))
        self._real.before_event(coord=coord)

    def after_event(self) -> None:
        self.calls.append("after_event")
        self._real.after_event()

    def release_target(self) -> None:
        self.calls.append("release_target")
        self._real.release_target()


def _make_computer_with_guard() -> tuple[ComputerTool, _FakeBackend, _RecordingGuard]:
    backend = _FakeBackend()
    computer = ComputerTool(backend, {})
    computer.resolve_display()
    idle_source = lambda: 999_999.0  # always long-idle -> never human_active
    presence = PresenceMonitor(idle_source=idle_source, platform="linux-x11")
    released: list[str] = []
    real_guard = CoexistenceGuard(
        presence=presence, release_all=lambda reason: (released.append(reason), [])[1]
    )
    spy = _RecordingGuard(real_guard)
    computer._coexistence_guard = spy  # type: ignore[assignment]
    return computer, backend, spy


# -- every mutating action gets before/after wiring --------------------------


def test_mouse_move_is_guarded():
    computer, backend, spy = _make_computer_with_guard()
    computer._run("mouse_move", {"coordinate": [10, 20]})
    assert backend.calls == [("move", 10, 20)]
    assert spy.calls == [
        "check_start_permission",
        "bind_target",
        ("before_event", (10, 20)),
        "after_event",
        "release_target",
    ]


def test_focus_window_is_guarded():
    computer, backend, spy = _make_computer_with_guard()
    computer._run("focus_window", {"handle": "42"})
    assert backend.calls == [("focus_window", "42")]
    assert spy.calls == [
        "check_start_permission",
        "bind_target",
        ("before_event", None),
        "after_event",
        "release_target",
    ]


def test_key_and_hold_key_are_guarded():
    computer, backend, spy = _make_computer_with_guard()
    computer._run("key", {"text": "ctrl+s"})
    assert backend.calls == [("key", "ctrl+s")]
    assert len([c for c in spy.calls if c == "check_start_permission"]) == 1

    computer2, backend2, spy2 = _make_computer_with_guard()
    computer2._run("hold_key", {"text": "shift", "duration": 0.1})
    assert backend2.calls == [("hold_key", "shift", 0.1)]
    assert len([c for c in spy2.calls if c == "check_start_permission"]) == 1


def test_left_mouse_down_and_up_are_guarded():
    computer, backend, spy = _make_computer_with_guard()
    computer._run("left_mouse_down", {"coordinate": [5, 5]})
    computer._run("left_mouse_up", {"coordinate": [5, 5]})
    assert backend.calls == [
        ("mouse_down", 5, 5, "left"),
        ("mouse_up", 5, 5, "left"),
    ]
    assert spy.calls.count("check_start_permission") == 2


def test_scroll_is_guarded():
    computer, backend, spy = _make_computer_with_guard()
    computer._run(
        "scroll", {"coordinate": [1, 1], "scroll_direction": "down", "scroll_amount": 5}
    )
    assert backend.calls == [("scroll", 1, 1, "down", 5)]
    assert ("before_event", (1, 1)) in spy.calls


# -- composites: guarded ONCE, not once per constituent sub-event (§8.4) -----


def test_double_click_is_guarded_exactly_once_not_per_sub_click():
    computer, backend, spy = _make_computer_with_guard()
    computer._run("double_click", {"coordinate": [3, 4]})
    # One click() call handles both sub-clicks internally (count=2) - the
    # guard must not be asked to check twice for one composite.
    assert backend.calls == [("click", 3, 4, "left", 2)]
    before_events = [
        c for c in spy.calls if isinstance(c, tuple) and c[0] == "before_event"
    ]
    after_events = [c for c in spy.calls if c == "after_event"]
    assert len(before_events) == 1
    assert len(after_events) == 1


def test_drag_is_guarded_exactly_once_around_the_whole_composite():
    computer, backend, spy = _make_computer_with_guard()
    computer._run("left_click_drag", {"start_coordinate": [1, 1], "coordinate": [9, 9]})
    assert backend.calls == [("drag", (1, 1), (9, 9))]
    before_events = [
        c for c in spy.calls if isinstance(c, tuple) and c[0] == "before_event"
    ]
    assert len(before_events) == 1
    # Exclusion-zone check uses the drag's END coordinate, per §7.5 - where
    # the synthetic input actually lands, not where it started.
    assert before_events[0] == ("before_event", (9, 9))


# -- the halt invariant reaches every mutating action, not just type --------


def test_halted_guard_blocks_a_click_before_the_backend_is_ever_called():
    computer, backend, spy = _make_computer_with_guard()
    # Force the underlying real guard into an already-halted state, exactly
    # as `before_event()` would leave it after a genuine detection - see
    # `coexistence_guard.CoexistenceGuard.before_event`.
    snapshot = PresenceSnapshot(
        state=PresenceState.HUMAN_ACTIVE,
        confidence=Confidence.HIGH,
        basis="idle_reconciliation",
        last_human_input_ago_ms=12.0,
        margin_ms=30.0,
        guard_ms=5.0,
        guard_measured=True,
        sample_interval_ms=60.0,
        latched_until_ms=None,
    )
    spy._real._halted = True
    spy._real._halt_snapshot = snapshot

    with pytest.raises(HaltedError):
        computer._run("left_click", {"coordinate": [1, 1]})

    # The whole point: the backend's click() must NEVER be reached once
    # halted - not a half-executed click, not a click that happens anyway.
    assert backend.calls == []


def test_halted_guard_blocks_a_drag_before_any_sub_event_fires():
    computer, backend, spy = _make_computer_with_guard()
    snapshot = PresenceSnapshot(
        state=PresenceState.HUMAN_ACTIVE,
        confidence=Confidence.HIGH,
        basis="idle_reconciliation",
        last_human_input_ago_ms=12.0,
        margin_ms=30.0,
        guard_ms=5.0,
        guard_measured=True,
        sample_interval_ms=60.0,
        latched_until_ms=None,
    )
    spy._real._halted = True
    spy._real._halt_snapshot = snapshot

    with pytest.raises(HaltedError):
        computer._run(
            "left_click_drag", {"start_coordinate": [0, 0], "coordinate": [50, 50]}
        )
    assert backend.calls == []


# -- no guard on this backend/platform: every action is a pre-existing no-op --


def test_no_coexistence_guard_means_actions_run_unaffected():
    backend = _FakeBackend()
    computer = ComputerTool(backend, {})
    computer.resolve_display()
    assert computer._coexistence_guard is None

    computer._run("mouse_move", {"coordinate": [7, 7]})
    computer._run("left_click", {"coordinate": [7, 7]})
    computer._run("key", {"text": "a"})

    assert ("move", 7, 7) in backend.calls
    assert ("click", 7, 7, "left", 1) in backend.calls
    assert ("key", "a") in backend.calls
