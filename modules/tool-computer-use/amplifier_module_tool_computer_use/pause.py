"""Pause/cancel semantics - `docs/designs/coexistence.md` \u00a78.1-\u00a78.5.

The central property (\u00a78.1, U7): **the injector owns and enforces pause
state. The controller may only observe it.** There is no `pause`/`resume` op
the controller can send (\u00a710.3 of `remote-transport.md`), and only a human
actor may clear a human-set pause - a buggy or compromised controller must
not be able to unpause itself.

| Actor                          | Set pause | Clear pause | Read pause |
|---------------------------------|:---------:|:-----------:|:----------:|
| Human, via the overlay          |    yes    |     yes     |    yes     |
| Human, via their own input (halt)|   yes    |     no      |     -      |
| Agent / injector                | self-halt |     no      |    yes     |
| Controller / model              |    no     |     no      |    yes     |

This module encodes that table as an actual constraint, not a docstring: only
`PauseController.set(source="human", ...)` / `.clear(source="human")` succeed;
anything else raises `PermissionError` naming the rejected source, rather than
silently no-op'ing (a silent no-op here would look, from the caller's side,
exactly like a bug where pause didn't take effect).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field


class PausedError(RuntimeError):
    """Raised by the guard when an operation is attempted while paused.

    \u00a78.3: "a distinct structured error... not a generic failure." Carries
    the setter and timestamp so a caller can render exactly the three
    required facts: who paused it, when, and (for an in-flight op) how much
    progress had been made.
    """

    def __init__(
        self, *, source: str, reason: str, since_monotonic: float, progress: str = ""
    ) -> None:
        self.source = source
        self.reason = reason
        self.since_monotonic = since_monotonic
        self.progress = progress
        detail = f" ({progress})" if progress else ""
        super().__init__(f"paused by {source} ({reason}){detail}")


@dataclass
class PauseController:
    """Owns pause state. Only a human-sourced call may set or clear it.

    `HUMAN_SOURCES` is the whitelist of caller-supplied `source` values that
    count as "a human, not the controller/model" - the overlay's own click
    handler passes `"overlay_click"`; the halt invariant (a real keystroke
    detected, \u00a76.0) is a distinct, stronger mechanism (see
    `coexistence_guard.CoexistenceGuard`) and is not routed through this
    class at all, so that the halt invariant can never be undone by anything
    that clears an ordinary pause.
    """

    HUMAN_SOURCES = frozenset({"overlay_click", "overlay_key", "manual_test"})

    _paused: bool = field(default=False, init=False)
    _source: str | None = field(default=None, init=False)
    _reason: str | None = field(default=None, init=False)
    _since: float | None = field(default=None, init=False)

    def set(self, source: str, reason: str = "human requested pause") -> None:
        if source not in self.HUMAN_SOURCES:
            raise PermissionError(
                f"pause can only be SET by a human source "
                f"({sorted(self.HUMAN_SOURCES)}); got {source!r} - "
                "the controller/model may never set pause"
            )
        self._paused = True
        self._source = source
        self._reason = reason
        self._since = time.monotonic()

    def clear(self, source: str) -> None:
        """Only the human may clear a human-set pause (\u00a78.1) - there is no
        controller-clear path, by construction, not by convention."""
        if source not in self.HUMAN_SOURCES:
            raise PermissionError(
                f"pause can only be CLEARED by a human source "
                f"({sorted(self.HUMAN_SOURCES)}); got {source!r} - a buggy or "
                "compromised controller must not be able to unpause itself"
            )
        self._paused = False
        self._source = None
        self._reason = None
        self._since = None

    @property
    def is_paused(self) -> bool:
        return self._paused

    def check(self, *, progress: str = "") -> None:
        """Raise `PausedError` if currently paused; no-op otherwise. Call
        this from the same guard point the halt invariant and target-binding
        checks live in (\u00a78.6's combined pseudocode)."""
        if self._paused:
            assert self._source is not None and self._since is not None
            raise PausedError(
                source=self._source,
                reason=self._reason or "",
                since_monotonic=self._since,
                progress=progress,
            )


@dataclass
class DragState:
    """Tracks an in-flight drag so a pause mid-drag can report truthfully
    where it landed rather than silently completing it or snapping it back
    (\u00a78.4): "the drag ends where the pointer is, and this is reported
    loudly... moving the pointer back to the drag origin first would be
    *driving the machine during a pause*."
    """

    active: bool = False
    start: tuple[int, int] | None = None
    last_position: tuple[int, int] | None = None

    def begin(self, start: tuple[int, int]) -> None:
        self.active = True
        self.start = start
        self.last_position = start

    def update(self, position: tuple[int, int]) -> None:
        self.last_position = position

    def end(self) -> dict[str, object]:
        """Call when a drag is interrupted by pause/halt. Returns the honest
        report: where it started, where it ended up, and that it was NOT
        completed to its intended endpoint."""
        report = {
            "drag_interrupted": True,
            "start": self.start,
            "ended_at": self.last_position,
        }
        self.active = False
        self.start = None
        self.last_position = None
        return report
