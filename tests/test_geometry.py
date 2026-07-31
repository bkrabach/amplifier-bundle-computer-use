"""Unit tests for the coordinate math shared by every backend (`geometry.py`).

This is pure logic with zero I/O - it had zero test coverage before this change,
despite being the one place MODEL<->SCREEN coordinate translation happens for every
click, drag, and scroll this bundle ever sends to a real desktop.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "modules" / "tool-computer-use"))

from amplifier_module_tool_computer_use.geometry import (
    Display,
    compute_display,
)

# -- compute_display -----------------------------------------------------------


def test_compute_display_preserves_aspect_ratio_when_downscaling():
    mw, mh = compute_display(3840, 2160, max_edge=1280, max_pixels=1_150_000)
    assert mw <= 1280
    assert mh <= 1280
    # 16:9 in, 16:9 out (within even-dimension rounding).
    assert abs((mw / mh) - (3840 / 2160)) < 0.01


def test_compute_display_never_upscales():
    # A screen already smaller than the budget must come back unchanged (mod evenness).
    mw, mh = compute_display(800, 600, max_edge=1280, max_pixels=1_150_000)
    assert mw <= 800
    assert mh <= 600


def test_compute_display_respects_max_pixels_even_under_max_edge():
    # 1280x1280 is within max_edge but is 1.6MP - over the 1.15MP budget.
    mw, mh = compute_display(1280, 1280, max_edge=1280, max_pixels=1_150_000)
    assert mw * mh <= 1_150_000


def test_compute_display_dimensions_always_even():
    for w, h in [(1024, 768), (3840, 2160), (1921, 1081), (801, 601)]:
        mw, mh = compute_display(w, h, max_edge=1280, max_pixels=1_150_000)
        assert mw % 2 == 0
        assert mh % 2 == 0


def test_compute_display_never_below_floor():
    # Degenerate/tiny inputs must not collapse to zero.
    mw, mh = compute_display(2, 2, max_edge=1280, max_pixels=1_150_000)
    assert mw >= 2
    assert mh >= 2


# -- Display.to_screen / to_model ------------------------------------------------


def test_to_screen_scales_and_clamps():
    disp = Display(
        screen_width=3840, screen_height=2160, model_width=1280, model_height=720
    )
    sx, sy = disp.to_screen(0, 0)
    assert (sx, sy) == (0, 0)
    sx, sy = disp.to_screen(1280, 720)  # bottom-right corner, model space
    assert sx == disp.screen_width - 1
    assert sy == disp.screen_height - 1


def test_to_screen_applies_origin_offset():
    # A secondary monitor at a non-zero origin (the real Windows multi-monitor case).
    disp = Display(
        screen_width=1920,
        screen_height=1080,
        model_width=1280,
        model_height=720,
        origin_x=1920,
        origin_y=0,
    )
    sx, sy = disp.to_screen(0, 0)
    assert sx == 1920
    assert sy == 0


def test_to_model_is_the_inverse_of_to_screen():
    """This inverse did not exist before this change - `cursor_position` had no
    correct way to report a real screen position back in the coordinate space the
    model actually uses."""
    disp = Display(
        screen_width=3840, screen_height=2160, model_width=1280, model_height=720
    )
    for mx, my in [(0, 0), (640, 360), (1279, 719)]:
        sx, sy = disp.to_screen(mx, my)
        rx, ry = disp.to_model(sx, sy)
        # Round-trip through integer rounding at 3x scale: within 1px.
        assert abs(rx - mx) <= 1
        assert abs(ry - my) <= 1


def test_to_model_clamps_to_model_bounds():
    disp = Display(
        screen_width=1024, screen_height=768, model_width=1024, model_height=768
    )
    x, y = disp.to_model(-100, -100)
    assert x == 0
    assert y == 0
    x, y = disp.to_model(999999, 999999)
    assert x == disp.model_width - 1
    assert y == disp.model_height - 1


def test_scale_x_and_scale_y_track_independent_axes():
    disp = Display(
        screen_width=1024, screen_height=768, model_width=1024, model_height=768
    )
    assert disp.scale_x == 1.0
    assert disp.scale_y == 1.0
    assert disp.scale == disp.scale_x  # documented alias
