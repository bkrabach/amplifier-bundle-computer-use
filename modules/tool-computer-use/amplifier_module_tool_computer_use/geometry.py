"""Coordinate geometry shared by every computer-use backend.

Coordinate spaces
------------------
MODEL space  - what Claude sees and emits. Matches the downscaled screenshot exactly.
SCREEN space - real physical pixels of the desktop (a Windows virtual desktop, an X11
               root window, ...).

`Display.to_screen()` / `Display.to_model()` are the only places the two are converted.
This module is pure math - no I/O, no subprocess, no backend-specific knowledge - so it
is shared unchanged by every backend (`WindowsBackend`, `LinuxX11Backend`, ...).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Display:
    """Mapping between the real desktop and the image the model actually sees."""

    screen_width: int
    screen_height: int
    model_width: int
    model_height: int
    origin_x: int = 0
    origin_y: int = 0

    @property
    def scale_x(self) -> float:
        return self.screen_width / self.model_width

    @property
    def scale_y(self) -> float:
        return self.screen_height / self.model_height

    @property
    def scale(self) -> float:
        """Alias for `scale_x`, kept for callers that only need one factor.

        `compute_display` preserves aspect ratio, so `scale_x` and `scale_y` are equal
        to within a pixel of rounding; `to_screen`/`to_model` always use the correct
        per-axis factor internally and never rely on this alias.
        """
        return self.scale_x

    def to_screen(self, x: float, y: float) -> tuple[int, int]:
        """MODEL coords -> SCREEN coords, clamped to the desktop bounds."""
        sx = round(x * self.scale_x) + self.origin_x
        sy = round(y * self.scale_y) + self.origin_y
        sx = max(self.origin_x, min(sx, self.origin_x + self.screen_width - 1))
        sy = max(self.origin_y, min(sy, self.origin_y + self.screen_height - 1))
        return sx, sy

    def to_model(self, sx: float, sy: float) -> tuple[int, int]:
        """SCREEN coords -> MODEL coords (inverse of `to_screen`), clamped to the image.

        This is the missing inverse: without it, anything that reports a real screen
        position back to the model (e.g. `cursor_position`) has no correct way to
        express it in the coordinate space the model actually uses.
        """
        x = round((sx - self.origin_x) / self.scale_x)
        y = round((sy - self.origin_y) / self.scale_y)
        x = max(0, min(x, self.model_width - 1))
        y = max(0, min(y, self.model_height - 1))
        return x, y


def compute_display(
    screen_w: int, screen_h: int, max_edge: int, max_pixels: int
) -> tuple[int, int]:
    """Largest model-space size that preserves aspect ratio and respects both budgets."""
    scale = min(
        1.0,
        max_edge / max(screen_w, screen_h),
        (max_pixels / (screen_w * screen_h)) ** 0.5,
    )
    # Even dimensions avoid half-pixel rounding drift when mapping coordinates back.
    return max(2, int(screen_w * scale) // 2 * 2), max(
        2, int(screen_h * scale) // 2 * 2
    )
