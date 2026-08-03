"""Geometric exclusion zones - `docs/coexistence.md` \u00a77.5.

The Linux overlay's status band is input-transparent (X `SHAPE`/`ShapeInput` -
see `overlay_linux.py`); only its Pause/Cancel button rectangles actually take
input. Making that safe against the agent clicking its own controls requires
a *second*, independent check: those same rectangles must be excluded at the
injection call site itself, in SCREEN space, so the agent's own `click`/`move`
calls refuse to land there - not merely "the overlay doesn't visually cover
this pixel," but "no synthetic event this process emits may target this pixel
at all," regardless of what the overlay is doing.

Pure logic, no X11/display dependency - this module is a plain rectangle
registry so it is unit-testable with no server present, and shared by every
platform (a future Windows/macOS overlay's buttons plug into the same
registry).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Rect:
    """A SCREEN-space rectangle, `[x1, y1, x2, y2)` - x2/y2 exclusive, matching
    the `(x1, y1, x2, y2)` region convention already used by `Backend.capture`.
    """

    x1: int
    y1: int
    x2: int
    y2: int

    def contains(self, x: int, y: int) -> bool:
        return self.x1 <= x < self.x2 and self.y1 <= y < self.y2

    def __post_init__(self) -> None:
        if self.x2 <= self.x1 or self.y2 <= self.y1:
            raise ValueError(
                f"degenerate exclusion rect {self!r}: x2 must be > x1 and "
                "y2 must be > y1"
            )


class ExclusionZone:
    """A registry of rectangles no synthetic input may target.

    Deliberately dumb: `contains(x, y)` is a linear scan over however many
    rects are registered (at most a handful - a status band's Pause/Cancel
    buttons, never a hot path with hundreds of entries).
    """

    def __init__(self) -> None:
        self._rects: dict[str, Rect] = {}

    def register(self, name: str, rect: Rect) -> None:
        """Register (or replace) an excluded rectangle under `name` - e.g.
        `"pause_button"`, `"cancel_button"` - so it can be individually
        cleared later (`unregister`) without disturbing the others."""
        self._rects[name] = rect

    def unregister(self, name: str) -> None:
        self._rects.pop(name, None)

    def clear(self) -> None:
        self._rects.clear()

    def contains(self, x: int, y: int) -> str | None:
        """Return the name of the first excluded rect containing `(x, y)`,
        or `None` if the point is not excluded."""
        for name, rect in self._rects.items():
            if rect.contains(x, y):
                return name
        return None

    @property
    def rects(self) -> dict[str, Rect]:
        return dict(self._rects)
