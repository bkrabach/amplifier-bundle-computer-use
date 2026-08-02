"""Unit tests for `CoexistenceGuard` (`coexistence_guard.py`) - the combined
per-event check from `docs/designs/coexistence.md` \u00a78.6: halt, pause,
geometric exclusion, and target binding in one call site, plus \u00a76.0's
release-on-abort behavior.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "modules" / "tool-computer-use"))

import pytest
from amplifier_module_tool_computer_use.coexistence_guard import (
    CoexistenceGuard,
    ExcludedCoordinateError,
    HaltedError,
)
from amplifier_module_tool_computer_use.exclusion import Rect
from amplifier_module_tool_computer_use.pause import PausedError
from amplifier_module_tool_computer_use.presence import (
    Confidence,
    PresenceMonitor,
    PresenceSnapshot,
    PresenceState,
)
from amplifier_module_tool_computer_use.target_binding import TargetChangedError


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0
        self.last_input_at = self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds

    def touch(self) -> None:
        self.last_input_at = self.now

    def idle_ms(self) -> float:
        return (self.now - self.last_input_at) * 1000.0


def make_guard(clock: FakeClock, **kwargs) -> tuple[CoexistenceGuard, list[str]]:
    released: list[str] = []
    monitor = PresenceMonitor(idle_source=clock.idle_ms, platform="linux-x11")
    guard = CoexistenceGuard(
        presence=monitor,
        release_all=lambda reason: (released.append(reason), [reason])[1],
        **kwargs,
    )
    return guard, released


# -- normal operation: nothing blocks a quiet session ------------------------


def test_before_event_passes_when_nothing_is_wrong():
    clock = FakeClock()
    guard, _released = make_guard(clock)
    guard.before_event()  # first sample ever - no injection recorded yet
    guard.after_event()
    clock.advance(0.030)  # well past QUIET_FLOOR with nothing new
    guard.before_event()  # still fine - quiet


# -- the halt invariant, end to end via the guard ----------------------------


def test_human_detected_mid_session_halts_and_releases(monkeypatch):
    """`before_event()`/`after_event()` call `PresenceMonitor.record_inject`/
    `.sample` with no explicit clock argument in production use (that is the
    whole point - callers never plumb a clock through), which means they
    fall back to real `time.monotonic()`. To drive that real path
    deterministically here, `presence.time.monotonic` is patched to read
    from the same `FakeClock` instead of real wall time.
    """
    import amplifier_module_tool_computer_use.presence as presence_module

    clock = FakeClock()
    monkeypatch.setattr(presence_module.time, "monotonic", lambda: clock.now)

    guard, released = make_guard(clock)
    guard.before_event()
    guard.after_event()  # records our own injection at clock.now
    clock.advance(0.030)
    clock.touch()  # independent human input, 30ms after our injection

    with pytest.raises(HaltedError):
        guard.before_event()
    assert guard.halted is True
    assert released == ["halted"]

    # Halt is sticky: every subsequent before_event() keeps raising, with no
    # further release_all calls needed (already released).
    with pytest.raises(HaltedError):
        guard.before_event()


# -- pause -------------------------------------------------------------------


def test_pause_blocks_before_event_and_releases():
    clock = FakeClock()
    guard, released = make_guard(clock)
    guard.pause.set("overlay_click", reason="human wants a break")
    with pytest.raises(PausedError):
        guard.before_event()
    assert released == ["paused"]


def test_clearing_pause_allows_before_event_again():
    clock = FakeClock()
    guard, _released = make_guard(clock)
    guard.pause.set("overlay_click")
    guard.pause.clear("overlay_click")
    guard.before_event()  # no longer raises


# -- geometric exclusion ------------------------------------------------------


def test_coordinate_inside_excluded_rect_is_refused():
    clock = FakeClock()
    guard, _released = make_guard(clock)
    guard.exclusion.register("pause_button", Rect(0, 0, 90, 36))
    with pytest.raises(ExcludedCoordinateError):
        guard.before_event(coord=(45, 18))


def test_coordinate_outside_excluded_rects_passes():
    clock = FakeClock()
    guard, _released = make_guard(clock)
    guard.exclusion.register("pause_button", Rect(0, 0, 90, 36))
    guard.before_event(coord=(500, 500))  # nowhere near the button


# -- target binding ------------------------------------------------------------


def test_target_change_aborts_and_releases():
    clock = FakeClock()
    targets = iter(["window-1", "window-1", "window-2"])
    guard, released = make_guard(clock, target_source=lambda: next(targets))
    guard.bind_target()  # reads "window-1"
    guard.before_event()  # reads "window-1" again - matches, passes
    with pytest.raises(TargetChangedError):
        guard.before_event()  # reads "window-2" - mismatch, aborts
    assert released == ["target_changed"]


def test_no_target_source_means_binding_is_never_enforced():
    clock = FakeClock()
    guard, _released = make_guard(clock)  # no target_source supplied
    guard.bind_target()
    assert guard.binding.status == "not_bound"
    guard.before_event()  # never raises on target binding


# -- seed_halted: cross-session durable halt seeding (defect 2) --------------


def test_seed_halted_makes_the_very_first_before_event_raise():
    """A guard seeded from a durable halt record (`halt_state.py`) must halt
    on its very FIRST `before_event()` call - before any live presence
    sample could possibly have detected anything - because the record is
    memory of a PRIOR session, not a live read."""
    clock = FakeClock()
    guard, released = make_guard(clock)
    snapshot = PresenceSnapshot(
        state=PresenceState.HUMAN_ACTIVE,
        confidence=Confidence.HIGH,
        basis="persisted_halt_from_prior_session",
        last_human_input_ago_ms=90_000.0,
        margin_ms=89_995.0,
        guard_ms=5.0,
        guard_measured=True,
        sample_interval_ms=None,
        latched_until_ms=None,
    )

    guard.seed_halted(snapshot)

    assert guard.halted is True
    with pytest.raises(HaltedError) as excinfo:
        guard.before_event()
    assert excinfo.value.snapshot is snapshot
    assert released == ["halted"]


def test_seed_halted_survives_even_a_long_quiet_idle_read():
    """The whole point: a fresh guard's OWN live idle read would say
    `quiet` (nothing has touched the machine in this new process's short
    life) - `seed_halted` must not be something a subsequent quiet sample
    can wash away. There is still no way to clear it from this class."""
    clock = FakeClock()
    guard, _released = make_guard(clock)
    snapshot = PresenceSnapshot(
        state=PresenceState.HUMAN_ACTIVE,
        confidence=Confidence.HIGH,
        basis="persisted_halt_from_prior_session",
        last_human_input_ago_ms=90_000.0,
        margin_ms=89_995.0,
        guard_ms=5.0,
        guard_measured=True,
        sample_interval_ms=None,
        latched_until_ms=None,
    )
    guard.seed_halted(snapshot)

    clock.advance(120.0)  # far past LATCH_DECAY_SECONDS; idle_ms() stays huge -> quiet
    with pytest.raises(HaltedError):
        guard.before_event()  # live sample says quiet; seeded halt still wins


# -- as_dict / presence envelope ----------------------------------------------


def test_as_dict_reports_halted_paused_and_binding_status():
    clock = FakeClock()
    guard, _released = make_guard(clock)
    out = guard.as_dict()
    assert out["halted"] is False
    assert out["paused"] is False
    assert out["target_binding"] == "not_bound"
