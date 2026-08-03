"""Unit tests for `_normalize_openai_action` - the wire-shape translation
between OpenAI's Responses API `computer_call` action batch (`{"type": ...,
"x": ..., "y": ..., ...}`) and this tool's own `(action, params)` vocabulary
(`{"action": ..., "coordinate": [...], ...}`).

Live-captured evidence this translation is built from:
`tests/fixtures/captures/openai-turn0.json` (`{"type": "screenshot"}`),
`openai-turn1.json` (`{"type": "move", "keys": null, "x": 426, "y": 87}`).
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "modules" / "tool-computer-use"))

import pytest
from amplifier_module_tool_computer_use import _normalize_openai_action


def test_screenshot():
    assert _normalize_openai_action({"type": "screenshot"}) == ("screenshot", {})


def test_wait():
    assert _normalize_openai_action({"type": "wait"}) == ("wait", {})


def test_move_matches_live_capture():
    """Exact shape from openai-turn1.json."""
    action, params = _normalize_openai_action(
        {"type": "move", "keys": None, "x": 426, "y": 87}
    )
    assert action == "mouse_move"
    assert params == {"coordinate": [426.0, 87.0]}


def test_move_missing_xy_raises():
    with pytest.raises(ValueError, match="missing x/y"):
        _normalize_openai_action({"type": "move"})


def test_click_default_button_is_left():
    action, params = _normalize_openai_action({"type": "click", "x": 10, "y": 20})
    assert action == "left_click"
    assert params == {"coordinate": [10.0, 20.0]}


def test_click_right_button():
    action, params = _normalize_openai_action(
        {"type": "click", "button": "right", "x": 5, "y": 6}
    )
    assert action == "right_click"


def test_click_middle_and_wheel_button():
    action, _ = _normalize_openai_action(
        {"type": "click", "button": "middle", "x": 1, "y": 1}
    )
    assert action == "middle_click"
    action, _ = _normalize_openai_action(
        {"type": "click", "button": "wheel", "x": 1, "y": 1}
    )
    assert action == "middle_click"


def test_double_click():
    action, params = _normalize_openai_action({"type": "double_click", "x": 3, "y": 4})
    assert action == "double_click"
    assert params == {"coordinate": [3.0, 4.0]}


def test_drag_uses_first_and_last_path_points():
    action, params = _normalize_openai_action(
        {
            "type": "drag",
            "path": [{"x": 1, "y": 2}, {"x": 5, "y": 6}, {"x": 9, "y": 10}],
        }
    )
    assert action == "left_click_drag"
    assert params == {"start_coordinate": [1.0, 2.0], "coordinate": [9.0, 10.0]}


def test_drag_with_short_path_raises():
    with pytest.raises(ValueError, match="path of >= 2 points"):
        _normalize_openai_action({"type": "drag", "path": [{"x": 1, "y": 2}]})


def test_scroll_down():
    action, params = _normalize_openai_action(
        {"type": "scroll", "x": 100, "y": 100, "scroll_x": 0, "scroll_y": 3}
    )
    assert action == "scroll"
    assert params["scroll_direction"] == "down"
    assert params["scroll_amount"] == 3
    assert params["coordinate"] == [100.0, 100.0]


def test_scroll_up():
    action, params = _normalize_openai_action(
        {"type": "scroll", "scroll_x": 0, "scroll_y": -2}
    )
    assert action == "scroll"
    assert params["scroll_direction"] == "up"
    assert params["scroll_amount"] == 2
    assert "coordinate" not in params


def test_scroll_horizontal():
    action, params = _normalize_openai_action(
        {"type": "scroll", "scroll_x": -4, "scroll_y": 0}
    )
    assert action == "scroll"
    assert params["scroll_direction"] == "left"
    assert params["scroll_amount"] == 4


def test_keypress():
    action, params = _normalize_openai_action(
        {"type": "keypress", "keys": ["ctrl", "s"]}
    )
    assert action == "key"
    assert params == {"text": "ctrl+s"}


def test_type_text():
    action, params = _normalize_openai_action({"type": "type", "text": "hello"})
    assert action == "type"
    assert params == {"text": "hello"}


def test_unsupported_action_type_raises():
    with pytest.raises(ValueError, match="unsupported OpenAI computer-use action"):
        _normalize_openai_action({"type": "some_future_action"})


def test_missing_type_raises():
    with pytest.raises(ValueError, match="unsupported OpenAI computer-use action"):
        _normalize_openai_action({})
