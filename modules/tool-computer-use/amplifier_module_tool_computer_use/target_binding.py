"""Target binding - `docs/coexistence.md` \u00a78.6.

Closes a gap no indicator speed and no detector speed can fix: if the human
switches window focus mid-`type_text`, the remaining keystrokes land in the
wrong application, as a side effect of the human's own action, with no
perceivable decision point.

> Every multi-event operation is bound to a delivery target at its start.
> Before each elementary event, the injector re-reads the current target. If
> it changed, the operation aborts and reports.

The abort is **unconditional and dumb, on purpose** (\u00a78.6): it trips on any
target change, including one the agent's own keystroke caused (an autocomplete
popup, a dialog opened by Enter). Distinguishing "the agent caused this" from
"the human caused this" requires exactly the causal attribution this whole
feature exists because the agent gets wrong - so it does not try. Fail loud
beats clever.

Pure logic - `current_target_id` is supplied by the caller (a window handle,
foreground-window id, or `None` when the platform cannot determine one at all,
in which case binding is `"unverified"` rather than silently assumed correct -
see `TargetBinding.status`).
"""

from __future__ import annotations

from dataclasses import dataclass


class TargetChangedError(RuntimeError):
    """The delivery target changed mid-operation. Unconditional, on purpose -
    see module docstring."""

    def __init__(self, expected_target: str | None, actual_target: str | None) -> None:
        self.expected_target = expected_target
        self.actual_target = actual_target
        super().__init__(
            f"target changed mid-operation: bound to {expected_target!r}, "
            f"now {actual_target!r} - operation aborted"
        )


@dataclass
class TargetBinding:
    """Tracks the delivery target a single multi-event operation is bound to.

    Usage, matching \u00a78.6's pseudocode exactly:

        binding = TargetBinding()
        binding.bind(current_target_id())          # at operation start
        for ch in text:
            binding.check(current_target_id())      # before EACH elementary event
            inject(ch)
    """

    _bound_target: str | None = None
    _bound: bool = False

    def bind(self, target_id: str | None) -> None:
        """Bind to `target_id` at operation start.

        `target_id` of `None` means the platform could not determine a
        target at all (e.g. macOS pending O9, \u00a78.6) - binding is recorded as
        unverified rather than silently treated as "no target to check",
        which would make every subsequent `check()` a no-op. See `status`.
        """
        self._bound_target = target_id
        self._bound = True

    def check(self, current_target_id: str | None) -> None:
        """Call before every elementary event. Raises `TargetChangedError` if
        the target has changed since `bind()` - including a change to/from
        `None`, since that is itself a loss of the ability to verify binding
        mid-operation and must not be silently ignored.

        No-op (never raises) if `bind()` was never called - callers that
        never bind are declaring they do not use target binding for this
        operation, not asking for it to fail.
        """
        if not self._bound:
            return
        if current_target_id != self._bound_target:
            raise TargetChangedError(self._bound_target, current_target_id)

    def release(self) -> None:
        """Call at operation end (success or abort) to unbind."""
        self._bound = False
        self._bound_target = None

    @property
    def status(self) -> str:
        """`"bound"`, `"unverified"` (bound to `None` - platform cannot
        determine a target), or `"not_bound"` (no operation in flight)."""
        if not self._bound:
            return "not_bound"
        return "unverified" if self._bound_target is None else "bound"

    @property
    def bound_target(self) -> str | None:
        return self._bound_target
