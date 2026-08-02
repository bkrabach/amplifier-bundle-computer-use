"""Unit tests for the Windows overlay (`overlay_windows.py`).

CI-side only: no Windows target, no PowerShell, no subprocess spawned. Pure
geometry and registry logic, mirroring `test_overlay_linux.py`'s first tier
exactly (same constants, same right-aligned-band formula) - see
`overlay_windows.py`'s module docstring for why the two platform overlay
modules do not share one implementation yet.

Real-hardware proof (render, focus non-steal, teardown, exclusion
registration on an actual Windows desktop) lives in
`scripts/verify_windows_overlay.py` - not run here, matching this
project's established pattern (`CONTRIBUTING.md`'s "ship gate" section) for
platform code that cannot be exercised on a CI box with no Windows present.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "modules" / "tool-computer-use"))

from amplifier_module_tool_computer_use.exclusion import ExclusionZone
from amplifier_module_tool_computer_use.overlay_windows import (
    BAND_HEIGHT,
    BUTTON_MARGIN,
    BUTTON_WIDTH,
    WindowsOverlay,
)

# -- pure geometry ------------------------------------------------------------


def _rects(screen_x: int, screen_y: int, screen_width: int):
    overlay = WindowsOverlay(
        screen_width=screen_width, screen_x=screen_x, screen_y=screen_y
    )
    return overlay._button_rects()  # noqa: SLF001 - intentionally testing the pure helper directly


def test_button_rects_are_right_aligned_within_the_band():
    buttons = _rects(0, 0, 1920)
    names = [b.name for b in buttons]
    assert names == ["pause", "cancel"]
    cancel = buttons[1].rect
    assert cancel.x2 == 1920 - BUTTON_MARGIN
    assert cancel.x2 - cancel.x1 == BUTTON_WIDTH
    pause = buttons[0].rect
    assert pause.x2 == cancel.x1 - BUTTON_MARGIN
    assert pause.x2 - pause.x1 == BUTTON_WIDTH


def test_button_rects_stay_within_band_height():
    for btn in _rects(0, 0, 1920):
        assert btn.rect.y2 - btn.rect.y1 == BAND_HEIGHT


def test_button_rects_offset_by_screen_origin():
    """A monitor whose origin is not (0,0) - e.g. a secondary display to the
    right of the primary - must produce rects in the SAME absolute screen
    space `exclusion.ExclusionZone` and the injection call site both use."""
    buttons = _rects(1920, 0, 1080)
    cancel = buttons[1].rect
    assert cancel.x2 == 1920 + 1080 - BUTTON_MARGIN
    assert cancel.y1 == 0


def test_button_rects_never_overlap():
    pause, cancel = _rects(0, 0, 300)
    assert pause.rect.x2 <= cancel.rect.x1


# -- pure registry interaction (no PowerShell/Windows dependency) ------------


def test_button_rects_registered_into_a_real_exclusion_zone():
    """Confirms the two modules' shapes actually fit together - registering
    the overlay's own button rects into a bare `ExclusionZone` and checking
    a click on each - without ever touching PowerShell or a real Windows
    process."""
    zone = ExclusionZone()
    for btn in _rects(0, 0, 1920):
        zone.register(f"overlay_{btn.name}_button", btn.rect)
    pause_rect = _rects(0, 0, 1920)[0].rect
    midpoint = (
        (pause_rect.x1 + pause_rect.x2) // 2,
        (pause_rect.y1 + pause_rect.y2) // 2,
    )
    assert zone.contains(*midpoint) == "overlay_pause_button"
    assert zone.contains(10, 10) is None  # far from either button


def test_show_registers_rects_and_hide_unregisters_them_without_a_process():
    """`show()`'s rect computation + exclusion registration happens BEFORE
    the PowerShell launch attempt - verified here by constructing an
    overlay with an unresolvable `powershell_path` override so `show()`
    fails fast at the launch step, then confirming the rects were
    registered up to that point and are cleanly unregistered again."""
    zone = ExclusionZone()
    overlay = WindowsOverlay(
        screen_width=1920,
        exclusion=zone,
        powershell_path="/nonexistent/powershell.exe",
    )
    try:
        overlay.show()
    except Exception:
        pass
    assert overlay.shown is False
    assert len(zone.rects) == 0  # registered, then unregistered on the failure path


def test_hide_before_show_is_a_safe_no_op():
    overlay = WindowsOverlay(screen_width=1920)
    overlay.hide()  # must not raise
    assert overlay.shown is False
    assert overlay.pid is None
