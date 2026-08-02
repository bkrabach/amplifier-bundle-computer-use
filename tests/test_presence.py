"""Unit tests for the presence detector (`presence.py`) - the mechanism behind
`docs/designs/coexistence.md` \u00a75, proven empirically by O5/U1c in
`coexistence-probes.md`.

No X server, no subprocess, no real desktop - `idle_source` is a plain
closure over a mutable "fake OS idle clock" so the margin arithmetic itself
is what gets tested, deterministically, with no timing flakiness. Every call
into the monitor passes an explicit fake `now`/`at` from the SAME `FakeClock`
so the whole suite runs on a virtual clock rather than real wall time (real
elapsed test-execution microseconds would otherwise pollute margins that are
themselves measured in single-digit milliseconds).

`FakeClock` models the one fact U1b proved empirically: the agent's OWN
synthetic injection resets the OS idle counter exactly the same as a real
human keystroke does (`SendInput`, `XTEST`, `CGEventPost` all do this) - so
`last_input_at` is a single timeline that BOTH `agent_inject_now()` and
`human_input_now()` update. That is the entire reason idle-based
reconciliation needs `our_last_inject` bookkeeping at all: idle alone cannot
tell the two apart, only comparing it against what we know we just did can.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "modules" / "tool-computer-use"))

import pytest
from amplifier_module_tool_computer_use.presence import (
    GUARD_MS,
    LATCH_DECAY_SECONDS,
    Confidence,
    IdleUnreadableError,
    PresenceMonitor,
    PresenceState,
)


class FakeClock:
    """A controllable virtual clock plus a fake OS idle counter."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.last_input_at = self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds

    def agent_inject_now(self) -> None:
        """Our own synthetic injection - resets idle, exactly like a real
        keystroke (U1b: verified for `SendInput`/XTEST/`CGEventPost`)."""
        self.last_input_at = self.now

    def human_input_now(self) -> None:
        """A genuinely independent human event - also resets idle, from the
        OS's point of view indistinguishably from our own injection."""
        self.last_input_at = self.now

    def idle_ms(self) -> float:
        return (self.now - self.last_input_at) * 1000.0


def make_monitor(clock: FakeClock, platform: str = "linux-x11") -> PresenceMonitor:
    return PresenceMonitor(idle_source=clock.idle_ms, platform=platform)


def inject(monitor: PresenceMonitor, clock: FakeClock) -> None:
    """Simulate one elementary injected event at the clock's current time -
    exactly the pairing `CoexistenceGuard.after_event()` performs."""
    clock.agent_inject_now()
    monitor.record_inject(at=clock.now)


# -- basic construction ---------------------------------------------------


def test_unknown_platform_rejected():
    with pytest.raises(ValueError, match="unknown platform"):
        PresenceMonitor(idle_source=lambda: 0.0, platform="ms-dos")


def test_guard_ms_matches_platform_table():
    clock = FakeClock()
    monitor = make_monitor(clock, platform="linux-x11")
    assert monitor.guard_ms == GUARD_MS["linux-x11"]
    assert monitor.guard_measured is True


# -- the O5 scenario, reproduced exactly ------------------------------------


def test_o5_scenario_detects_human_keystroke_mid_typing():
    """Directly reproduces O5's harness shape: the agent injects every 60ms
    for 6s; a genuinely independent human event fires once mid-run, 25.1ms
    after one particular injection (the same order of magnitude O5 measured
    for its one real detection). Sequence per iteration mirrors
    `CoexistenceGuard`: inject, then (after the gap) sample - the sample
    always compares against the PREVIOUS injection.

    Two things are asserted, matching \u00a75.4's latch semantics precisely:
    zero false positives on every sample BEFORE the human event (nothing
    but our own injections has happened yet), and the human event is caught
    on the very next sample and then correctly LATCHED as `human_active`
    for the remainder of the run (\u00a75.4: once seen, present until a full
    minute of silence - 6s of test run never clears it). A raw, un-latched
    re-detection is also confirmed to have happened exactly once, via
    `margin_ms` (which the latch does not overwrite).
    """
    clock = FakeClock()
    monitor = make_monitor(clock)
    cadence = 0.060
    n_samples = 98
    human_at_i = 49
    fresh_margin_detections = 0
    false_positives_before_human_event = 0
    inject(monitor, clock)

    for i in range(n_samples):
        if i == human_at_i:
            # Human types 25.1ms after the last agent injection - a
            # genuinely independent event landing inside this gap.
            clock.advance(0.0251)
            clock.human_input_now()
            clock.advance(cadence - 0.0251)
        else:
            clock.advance(cadence)
        snap = monitor.sample(now=clock.now)
        if i < human_at_i and snap.state is PresenceState.HUMAN_ACTIVE:
            false_positives_before_human_event += 1
        if snap.margin_ms is not None and snap.margin_ms > snap.guard_ms:
            fresh_margin_detections += 1
        if i >= human_at_i:
            # \u00a75.4's latch: once seen, stays human_active for
            # LATCH_DECAY_SECONDS (60s) - this entire 98-sample/6s run
            # never clears it.
            assert snap.state is PresenceState.HUMAN_ACTIVE
        inject(monitor, clock)

    assert false_positives_before_human_event == 0
    assert fresh_margin_detections == 1


def test_guard_band_of_250ms_would_have_masked_the_same_event():
    """The regression this design fixes: at a wide guard band, a human
    event with a small enough margin is invisible - never `human_active` -
    because the agent re-injects every 60ms and a wide guard band never
    clears. This is what O5 proved was the defect, not the detector
    mechanism. Demonstrated against `windows-wsl2`'s real, measured (O4)
    20ms GUARD (see presence.py's `GUARD_MS` comment - 900 real-hardware
    samples, max observed margin 16.000ms): a margin constructed just
    below THAT platform's actual guard, rather than a hardcoded number
    that would silently stop demonstrating masking if the constant is
    ever re-measured again.
    """
    clock = FakeClock()
    monitor = PresenceMonitor(idle_source=clock.idle_ms, platform="windows-wsl2")
    below_guard_s = (monitor.guard_ms - 2.0) / 1000.0
    inject(monitor, clock)
    clock.advance(below_guard_s)
    clock.human_input_now()
    snap = monitor.sample(now=clock.now)
    assert snap.state is not PresenceState.HUMAN_ACTIVE
    assert snap.margin_ms is not None
    assert snap.margin_ms < monitor.guard_ms


# -- margin classification (\u00a75.3) -------------------------------------------


def test_margin_above_guard_is_human_active_high_confidence():
    clock = FakeClock()
    monitor = make_monitor(clock)
    inject(monitor, clock)
    clock.advance(0.030)  # 30ms after our own injection
    clock.human_input_now()
    snap = monitor.sample(now=clock.now)
    assert snap.state is PresenceState.HUMAN_ACTIVE
    assert snap.confidence is Confidence.HIGH
    assert snap.margin_ms is not None and snap.margin_ms > monitor.guard_ms


def test_margin_within_guard_band_is_quiet_not_a_false_positive():
    """Directly inside the ambiguous +-GUARD band (\u00a75.3's middle case): this
    must NOT be reported as `human_active` - see `presence.PresenceMonitor
    ._classify`'s docstring for why this is resolved to `quiet` rather than
    a standalone `unknown` state (an inconsistency between \u00a75.3's per-sample
    table and \u00a75.4's state-machine transition list)."""
    clock = FakeClock()
    monitor = make_monitor(clock)
    inject(monitor, clock)
    clock.advance(0.001)  # 1ms - inside the 5ms Linux guard, no human event
    snap = monitor.sample(now=clock.now)
    assert snap.state is not PresenceState.HUMAN_ACTIVE
    assert snap.margin_ms is not None
    assert abs(snap.margin_ms) <= monitor.guard_ms


def test_long_quiet_period_is_high_confidence_quiet():
    clock = FakeClock()
    monitor = make_monitor(clock)
    inject(monitor, clock)
    clock.advance(3.0)  # long past QUIET_FLOOR_SECONDS with no new input
    snap = monitor.sample(now=clock.now)
    assert snap.state is PresenceState.QUIET
    assert snap.confidence is Confidence.HIGH


def test_brief_quiet_period_is_low_confidence_quiet():
    clock = FakeClock()
    monitor = make_monitor(clock)
    inject(monitor, clock)
    clock.advance(0.5)  # our own injection is most recent, but not long ago
    snap = monitor.sample(now=clock.now)
    assert snap.state is PresenceState.QUIET
    assert snap.confidence is Confidence.LOW


# -- the latch (\u00a75.4) -------------------------------------------------------


def test_latch_holds_human_active_through_a_quiet_sample_within_decay_window():
    clock = FakeClock()
    monitor = make_monitor(clock)
    inject(monitor, clock)
    clock.advance(0.030)
    clock.human_input_now()
    snap = monitor.sample(now=clock.now)
    assert snap.state is PresenceState.HUMAN_ACTIVE

    # Immediately after, our own injection is most recent again - a naive
    # per-sample read would go straight back to quiet. The latch must keep
    # reporting human_active.
    inject(monitor, clock)
    clock.advance(1.0)
    snap2 = monitor.sample(now=clock.now)
    assert snap2.state is PresenceState.HUMAN_ACTIVE
    assert snap2.confidence is Confidence.HIGH


def test_latch_decays_after_full_window_of_silence():
    clock = FakeClock()
    monitor = make_monitor(clock)
    inject(monitor, clock)
    clock.advance(0.030)
    clock.human_input_now()
    monitor.sample(now=clock.now)

    inject(monitor, clock)
    clock.advance(LATCH_DECAY_SECONDS + 1.0)
    snap = monitor.sample(now=clock.now)
    assert snap.state is PresenceState.QUIET


# -- idle_unreadable (\u00a79.6): hard error, never silently "quiet" -------------


def test_idle_unreadable_raises_and_never_reports_quiet():
    def _boom() -> float:
        raise OSError("idle counter unreadable")

    monitor = PresenceMonitor(idle_source=_boom, platform="linux-x11")
    with pytest.raises(IdleUnreadableError):
        monitor.sample()
    assert monitor.state is PresenceState.UNKNOWN


# -- per-event sampling, not per-operation (\u00a75.2) ----------------------------


def test_sample_interval_is_reported_between_calls():
    clock = FakeClock()
    monitor = make_monitor(clock)
    monitor.sample(now=clock.now)
    clock.advance(0.060)
    snap = monitor.sample(now=clock.now)
    assert snap.sample_interval_ms is not None
    assert snap.sample_interval_ms == pytest.approx(60.0, abs=1.0)
