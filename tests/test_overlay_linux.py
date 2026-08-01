"""Unit tests for the Linux overlay (`overlay_linux.py`).

Two tiers, matching this codebase's established pattern for X11 code
(`test_geometry.py`/`test_backend_monitors.py` for pure math, live X11-only
scripts for the rest):

1. **Pure geometry** - `_button_rects()` is tested directly with no `Xlib`
   import, no display connection, no window ever created. This is what runs
   in CI with no desktop present.
2. **Live smoke test** - if `DISPLAY` is set and Xlib/SHAPE actually connect,
   a real override-redirect window is created on a scratch offscreen pixmap
   size, shown, its button rects registered into a real `ExclusionZone`, and
   torn down - proving the class works against a real X server, not just
   its math. Skipped (not failed) when no display is available, matching
   `test_macos_backend.py`'s stated division of labor for platform code that
   cannot be exercised on every CI box.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "modules" / "tool-computer-use"))

import pytest
from amplifier_module_tool_computer_use.exclusion import ExclusionZone
from amplifier_module_tool_computer_use.overlay_linux import (
    BAND_HEIGHT,
    BUTTON_MARGIN,
    BUTTON_WIDTH,
)

# -- pure geometry, no Xlib import at all ------------------------------------


class _GeometryOnlyOverlay:
    """Exercises `LinuxOverlay._button_rects()`'s pure math without ever
    importing Xlib - constructed by calling the real class's method as an
    unbound function against a lightweight stand-in, so this test has zero
    display-server dependency."""

    def __init__(self, screen_x: int, screen_y: int, screen_width: int) -> None:
        self._screen_x = screen_x
        self._screen_y = screen_y
        self._screen_width = screen_width


def _button_rects(overlay: _GeometryOnlyOverlay):
    from amplifier_module_tool_computer_use.overlay_linux import LinuxOverlay

    return LinuxOverlay._button_rects(overlay)  # type: ignore[arg-type]


def test_button_rects_are_right_aligned_within_the_band():
    overlay = _GeometryOnlyOverlay(screen_x=0, screen_y=0, screen_width=1920)
    buttons = _button_rects(overlay)
    names = [b.name for b in buttons]
    assert names == ["pause", "cancel"]
    cancel = buttons[1].rect
    assert cancel.x2 == 1920 - BUTTON_MARGIN
    assert cancel.x2 - cancel.x1 == BUTTON_WIDTH
    pause = buttons[0].rect
    assert pause.x2 == cancel.x1 - BUTTON_MARGIN
    assert pause.x2 - pause.x1 == BUTTON_WIDTH


def test_button_rects_stay_within_band_height():
    overlay = _GeometryOnlyOverlay(screen_x=0, screen_y=0, screen_width=1920)
    for btn in _button_rects(overlay):
        assert btn.rect.y2 - btn.rect.y1 == BAND_HEIGHT


def test_button_rects_offset_by_screen_origin():
    """A monitor whose origin is not (0,0) - e.g. a secondary display to the
    right of the primary - must produce rects in the SAME absolute screen
    space `exclusion.ExclusionZone` and the injection call site both use."""
    overlay = _GeometryOnlyOverlay(screen_x=1920, screen_y=0, screen_width=1080)
    buttons = _button_rects(overlay)
    cancel = buttons[1].rect
    assert cancel.x2 == 1920 + 1080 - BUTTON_MARGIN
    assert cancel.y1 == 0


def test_button_rects_never_overlap():
    overlay = _GeometryOnlyOverlay(screen_x=0, screen_y=0, screen_width=300)
    pause, cancel = _button_rects(overlay)
    assert pause.rect.x2 <= cancel.rect.x1


# -- pure registry interaction (no display) -----------------------------------


def test_button_rects_registered_into_a_real_exclusion_zone():
    """Confirms the two modules' shapes actually fit together - registering
    the overlay's own button rects into a bare `ExclusionZone` and checking
    a click on each - without ever touching Xlib."""
    overlay = _GeometryOnlyOverlay(screen_x=0, screen_y=0, screen_width=1920)
    zone = ExclusionZone()
    for btn in _button_rects(overlay):
        zone.register(f"overlay_{btn.name}_button", btn.rect)
    pause_rect = _button_rects(overlay)[0].rect
    midpoint = (
        (pause_rect.x1 + pause_rect.x2) // 2,
        (pause_rect.y1 + pause_rect.y2) // 2,
    )
    assert zone.contains(*midpoint) == "overlay_pause_button"
    assert zone.contains(10, 10) is None  # far from either button


# -- live smoke test: real X server, skipped if unavailable -----------------


def _x11_available() -> bool:
    if not os.environ.get("DISPLAY"):
        return False
    try:
        from Xlib import display as xlib_display
    except ImportError:
        return False
    try:
        d = xlib_display.Display(os.environ.get("DISPLAY"))
        d.close()
    except Exception:  # noqa: BLE001 - any connection failure -> unavailable
        return False
    return True


@pytest.mark.skipif(not _x11_available(), reason="no live X11 DISPLAY available")
def test_overlay_shows_and_hides_on_a_real_x_server():
    from amplifier_module_tool_computer_use.overlay_linux import LinuxOverlay
    from Xlib import display as xlib_display

    conn = xlib_display.Display(os.environ.get("DISPLAY"))
    try:
        zone = ExclusionZone()
        overlay = LinuxOverlay(conn, screen_width=400, exclusion=zone)
        overlay.show()
        try:
            assert overlay.shown is True
            assert len(overlay.buttons) == 2
            assert len(zone.rects) == 2
        finally:
            overlay.hide()
        assert overlay.shown is False
        assert len(zone.rects) == 0
    finally:
        conn.close()
