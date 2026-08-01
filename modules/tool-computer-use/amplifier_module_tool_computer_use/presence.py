"""The presence detector - `docs/designs/coexistence.md` \u00a75.

Answers one question, cheaply and per elementary event: *did a human just touch
this machine?* The mechanism is idle-time reconciliation (O5, U1c): the agent
already knows exactly when it last injected a synthetic event
(`our_last_inject`, `time.monotonic()`); the OS idle counter tells it when *any*
input (synthetic or real) last landed (`idle`). If the OS's most-recent-input
time is *later* than our own last injection by more than `GUARD`, something
else touched the machine - a human.

Why this shape, specifically
-----------------------------
Revision 1 of the design recorded one timestamp per *operation* and used
`GUARD = 250ms`. Both were wrong, and O5 (`docs/designs/coexistence-probes.md`)
proved it: at 250ms, a human keystroke landing mid-`type_text` (60ms injection
cadence) is invisible for the *entire* duration of the operation, because the
guard band never clears between the agent's own injections. The fix proven
there and implemented here:

* `record_inject()` is called after **every elementary event** (every
  keystroke, not once per `type_text` call).
* `GUARD` is single-digit milliseconds on Linux (5ms - the number O5 measured
  zero false positives at, with the one real human event caught at
  margin=+25.1ms across 98 samples).
* The detector samples **once per inter-injection gap**, not once per
  operation - `sample()` is meant to be called right before the *next*
  injection, exactly matching O5's harness shape (inject, sample, inject,
  sample, ...).

Clocks: everything here is `time.monotonic()` on ONE machine (the target this
detector runs on). Comparing a controller-side timestamp against a target-side
idle counter would require the two clocks to agree to within `GUARD` - orders
of magnitude tighter than tailnet NTP skew provides (\u00a75.1). This module is
never handed a foreign timestamp; the `idle_source` callable it is constructed
with must itself be local to the machine it reads.

Confidence is a margin test, not a probability
------------------------------------------------
`confidence` in the returned `PresenceSnapshot` is exactly two values:
`"high"` (the margin comparison is unambiguous) or `"low"` (idle is readable
but stale, so "quiet" is asserted without a fresh confirming sample).
Inventing a continuous probability from one jitter measurement would be a
confidence costume the design explicitly rejects (\u00a75.3).
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

#: Per-platform GUARD bands (milliseconds), \u00a75.5.
#:
#: Linux: proven directly by O5 - 98 samples at 60ms agent-injection cadence,
#: zero false positives, one true detection at margin=+25.1ms. 5ms is the
#: number the evidence supports, not a guess.
#:
#: Windows: `GetLastInputInfo`'s `dwTime` is quantised to `GetTickCount`
#: ticks (10-16ms per \u00a75.5) - the guard band must exceed the quantisation
#: noise floor or every reconciliation would be dominated by tick jitter
#: rather than real signal, so it is set to roughly 2x the worst-case tick
#: (32ms) rather than the 5ms Linux figure.
#:
#: macOS: `CGEventSourceSecondsSinceLastEventType`'s resolution was never
#: measured (O4 is still open per \u00a73.2/\u00a75.5) - extrapolating Linux's 5ms
#: here would be exactly the mistake revision 1 made with 250ms (assuming a
#: number without evidence). Until O4 measures it, macOS uses the Windows
#: figure as the conservative (not the optimistic) placeholder, and any
#: `PresenceMonitor` built for macOS must say so - see `guard_source`.
GUARD_MS: dict[str, float] = {
    "linux-x11": 5.0,
    "windows-wsl2": 32.0,
    "macos": 32.0,  # conservative placeholder pending O4 - see module docstring
}

#: Whether GUARD_MS for a platform is evidence-backed (O5, Linux) or a
#: conservative placeholder awaiting a probe (O4, macOS). Exposed so callers
#: (and `intra_op_detection` reporting, \u00a75.5) can tell the two apart honestly
#: rather than presenting an unmeasured number as if it were proven.
GUARD_MEASURED: dict[str, bool] = {
    "linux-x11": True,
    "windows-wsl2": True,  # quantisation is documented Win32 behavior, not guessed
    "macos": False,
}

#: \u00a75.4 - once a human is seen, they are latched present for a full minute
#: of silence. Converts a noisy per-sample read into a stable state and models
#: the human who is reading rather than typing (\u00a72.2).
LATCH_DECAY_SECONDS = 60.0

#: \u00a75.4 - "nobody has typed for this long" needed before a `margin < -GUARD`
#: read (our own input is most recent) is asserted `quiet, high` rather than
#: `quiet, low`. Purely a confidence label; it never gates the halt invariant.
QUIET_FLOOR_SECONDS = 2.0


class PresenceState(str, Enum):
    UNKNOWN = "unknown"
    QUIET = "quiet"
    HUMAN_ACTIVE = "human_active"


class Confidence(str, Enum):
    HIGH = "high"
    LOW = "low"


class IdleUnreadableError(RuntimeError):
    """`idle_source()` could not report a value.

    \u00a79.6: this is a hard error, never silently treated as `quiet` - "assume
    nobody's there" is exactly the incorrect assumption behind the incident
    this feature exists to prevent.
    """


@dataclass(frozen=True)
class PresenceSnapshot:
    """The `presence` block attached to every result, \u00a75.3."""

    state: PresenceState
    confidence: Confidence
    basis: str
    last_human_input_ago_ms: float | None
    margin_ms: float | None
    guard_ms: float
    guard_measured: bool
    sample_interval_ms: float | None
    latched_until_ms: float | None

    def to_dict(self) -> dict[str, object]:
        return {
            "state": self.state.value,
            "confidence": self.confidence.value,
            "basis": self.basis,
            "last_human_input_ago_ms": self.last_human_input_ago_ms,
            "margin_ms": self.margin_ms,
            "guard_ms": self.guard_ms,
            "guard_measured": self.guard_measured,
            "sample_interval_ms": self.sample_interval_ms,
            "latched_until_ms": self.latched_until_ms,
        }


@dataclass
class PresenceMonitor:
    """Per-elementary-event presence detector with a latch, \u00a75.2-\u00a75.4.

    `idle_source` returns the OS idle time in **milliseconds** (time since the
    last input event of any kind, synthetic or real) as measured on the
    machine this monitor runs against - e.g. `MIT-SCREEN-SAVER`'s
    `idle` field on Linux (see `linux_x11_idle_source`). It must be cheap
    (microseconds) and must raise on failure - never guess a fallback value.

    `platform` selects the GUARD band from `GUARD_MS`/`GUARD_MEASURED` above.
    An unknown platform name is a configuration error, not swallowed to a
    default - a silently-wrong guard band is precisely the defect O5 found in
    revision 1 (\u00a75.3).

    This class has NO configuration key, constructor parameter, or method
    that widens/disables the guard band or the human_active determination
    itself - that is deliberate (\u00a76.0's halt invariant sits one layer up in
    `coexistence_guard.CoexistenceGuard` and is likewise unconditional). See
    `test_halt_invariant.py`.
    """

    idle_source: Callable[[], float]
    platform: str
    _last_inject_monotonic: float | None = field(default=None, init=False)
    _last_sample_monotonic: float | None = field(default=None, init=False)
    _state: PresenceState = field(default=PresenceState.UNKNOWN, init=False)
    _latched_until: float | None = field(default=None, init=False)
    _last_snapshot: PresenceSnapshot | None = field(default=None, init=False)

    def __post_init__(self) -> None:
        if self.platform not in GUARD_MS:
            raise ValueError(
                f"unknown platform {self.platform!r} for PresenceMonitor; "
                f"known platforms: {sorted(GUARD_MS)}"
            )

    @property
    def guard_ms(self) -> float:
        return GUARD_MS[self.platform]

    @property
    def guard_measured(self) -> bool:
        return GUARD_MEASURED[self.platform]

    @property
    def state(self) -> PresenceState:
        return self._state

    @property
    def last_snapshot(self) -> PresenceSnapshot | None:
        return self._last_snapshot

    def record_inject(self, *, at: float | None = None) -> None:
        """Call immediately around every elementary injection syscall.

        \u00a75.2: "on every elementary event - not once per operation." A
        200-character `type_text` calls this 200 times, not once.
        """
        self._last_inject_monotonic = at if at is not None else time.monotonic()

    def sample(self, *, now: float | None = None) -> PresenceSnapshot:
        """Read idle, reconcile against our own last injection, update the
        latch, and return the snapshot (\u00a75.3/\u00a75.4).

        Intended to be called once per inter-injection gap - i.e. right
        before the *next* injection - matching O5's harness shape exactly.
        """
        now = now if now is not None else time.monotonic()
        interval_ms: float | None = None
        if self._last_sample_monotonic is not None:
            interval_ms = (now - self._last_sample_monotonic) * 1000.0
        self._last_sample_monotonic = now

        try:
            idle_ms = float(self.idle_source())
        except Exception as exc:  # any read failure -> unknown, never a guess
            self._state = PresenceState.UNKNOWN
            snap = PresenceSnapshot(
                state=PresenceState.UNKNOWN,
                confidence=Confidence.LOW,
                basis="idle_unreadable",
                last_human_input_ago_ms=None,
                margin_ms=None,
                guard_ms=self.guard_ms,
                guard_measured=self.guard_measured,
                sample_interval_ms=interval_ms,
                latched_until_ms=self._latched_until,
            )
            self._last_snapshot = snap
            raise IdleUnreadableError(f"idle_source() raised: {exc}") from exc

        inferred_last_input = now - (idle_ms / 1000.0)

        if self._last_inject_monotonic is None:
            # No injection has happened yet this session - nothing of ours to
            # reconcile against. Any measured idle simply reports quiet/active
            # by the QUIET_FLOOR rule below, basis stays idle_reconciliation.
            margin_ms: float | None = None
        else:
            margin_ms = (inferred_last_input - self._last_inject_monotonic) * 1000.0

        state, confidence = self._classify(margin_ms, idle_ms)

        if state is PresenceState.HUMAN_ACTIVE:
            self._latched_until = now + LATCH_DECAY_SECONDS
        elif self._latched_until is not None and now < self._latched_until:
            # Latch (\u00a75.4): once seen, present until LATCH_DECAY_SECONDS of
            # silence - a fresh `quiet` sample within the latch window does not
            # clear it early.
            state = PresenceState.HUMAN_ACTIVE
            confidence = Confidence.HIGH
        elif self._latched_until is not None and now >= self._latched_until:
            self._latched_until = None

        self._state = state
        snap = PresenceSnapshot(
            state=state,
            confidence=confidence,
            basis="idle_reconciliation",
            last_human_input_ago_ms=idle_ms,
            margin_ms=margin_ms,
            guard_ms=self.guard_ms,
            guard_measured=self.guard_measured,
            sample_interval_ms=interval_ms,
            latched_until_ms=self._latched_until,
        )
        self._last_snapshot = snap
        return snap

    def _classify(
        self, margin_ms: float | None, idle_ms: float
    ) -> tuple[PresenceState, Confidence]:
        """Classify one raw margin observation as `HUMAN_ACTIVE` or `QUIET`.

        \u00a75.3 states the margin comparison as three bands (`margin > GUARD`,
        `|margin| <= GUARD`, `margin < -GUARD`) with the middle band labelled
        "unknown". \u00a75.4's latch **state machine**, however, only ever drives
        `UNKNOWN` from a failed idle read or the pre-first-sample state - there
        is no listed transition INTO `UNKNOWN` from `QUIET`/`HUMAN_ACTIVE` on
        an ordinary sample - and algebraically, `|margin| <= GUARD` is not a
        rare edge case: in the steady state where nothing but our own
        injections has ever touched the machine, `inferred_last_input`
        converges on `our_last_inject` exactly, so margin sits at
        (approximately) zero for as long as nothing new happens - this is the
        ORDINARY "quiet" condition, not an occasional ambiguity. Given that
        inconsistency between \u00a75.3's per-sample table and \u00a75.4's transition
        list, this resolves it in the direction that keeps the halt invariant
        meaningful and the reported state useful: a margin at or below
        `GUARD` is `QUIET` (confidence keyed off `idle_ms` vs.
        `QUIET_FLOOR_SECONDS`, so a sample taken moments after our own
        injection is honestly reported as *low*-confidence quiet, one taken
        long after is *high*-confidence quiet) rather than a bare `UNKNOWN`
        that would never resolve on its own. `UNKNOWN` remains reserved for
        exactly what \u00a75.4 lists: idle unreadable (`IdleUnreadableError`,
        raised - never returned as a state), and "no sample has ever been
        taken".
        """
        if margin_ms is None:
            # Never injected yet: treat purely as a quiet/idle read, no
            # reconciliation possible.
            if idle_ms > QUIET_FLOOR_SECONDS * 1000.0:
                return PresenceState.QUIET, Confidence.HIGH
            return PresenceState.QUIET, Confidence.LOW

        if margin_ms > self.guard_ms:
            return PresenceState.HUMAN_ACTIVE, Confidence.HIGH
        # margin_ms <= guard_ms: nothing but our own injection(s) is the most
        # recent input this reconciliation can see.
        if idle_ms > QUIET_FLOOR_SECONDS * 1000.0:
            return PresenceState.QUIET, Confidence.HIGH
        return PresenceState.QUIET, Confidence.LOW


def linux_x11_idle_source(display: Any) -> Callable[[], float]:
    """Build an `idle_source` callable reading `MIT-SCREEN-SAVER` idle time
    (milliseconds) from an already-connected `Xlib.display.Display`.

    Kept as a tiny factory (rather than importing Xlib at module top) so
    `presence.py` itself has zero display-server dependency and can be
    unit-tested with a fake `idle_source` with no X server present at all -
    see `tests/test_presence.py`.
    """

    def _read() -> float:
        info = display.screen().root.screensaver_query_info()
        return float(info.idle)

    return _read
