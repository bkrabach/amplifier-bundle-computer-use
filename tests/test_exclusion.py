"""Unit tests for geometric exclusion zones (`exclusion.py`) -
`docs/coexistence.md` \u00a77.5: the overlay's own Pause/Cancel button
rects are excluded at the injection call site so the agent cannot click its
own controls.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "modules" / "tool-computer-use"))

import pytest
from amplifier_module_tool_computer_use.exclusion import ExclusionZone, Rect


def test_rect_contains_is_half_open():
    rect = Rect(10, 10, 20, 20)
    assert rect.contains(10, 10) is True  # inclusive lower bound
    assert rect.contains(19, 19) is True  # inclusive up to, not including, upper
    assert rect.contains(20, 20) is False  # exclusive upper bound
    assert rect.contains(9, 10) is False


def test_degenerate_rect_rejected():
    with pytest.raises(ValueError):
        Rect(10, 10, 10, 20)
    with pytest.raises(ValueError):
        Rect(10, 10, 20, 10)


def test_zone_contains_returns_registering_name():
    zone = ExclusionZone()
    zone.register("pause_button", Rect(0, 0, 90, 36))
    zone.register("cancel_button", Rect(100, 0, 190, 36))
    assert zone.contains(45, 18) == "pause_button"
    assert zone.contains(150, 18) == "cancel_button"
    assert zone.contains(95, 18) is None  # gap between the two buttons


def test_unregister_removes_the_exclusion():
    zone = ExclusionZone()
    zone.register("pause_button", Rect(0, 0, 90, 36))
    zone.unregister("pause_button")
    assert zone.contains(45, 18) is None


def test_clear_removes_all():
    zone = ExclusionZone()
    zone.register("a", Rect(0, 0, 10, 10))
    zone.register("b", Rect(20, 20, 30, 30))
    zone.clear()
    assert zone.contains(5, 5) is None
    assert zone.contains(25, 25) is None


def test_re_registering_same_name_replaces_rect():
    zone = ExclusionZone()
    zone.register("btn", Rect(0, 0, 10, 10))
    zone.register("btn", Rect(100, 100, 110, 110))
    assert zone.contains(5, 5) is None
    assert zone.contains(105, 105) == "btn"
