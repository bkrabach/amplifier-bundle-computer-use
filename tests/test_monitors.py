"""Unit tests for monitor selection (`monitors.select_monitor`).

Per-monitor targeting depends entirely on this being correct: a bad selection
means the wrong region of a real desktop gets captured and clicked. Every
`MonitorInfo` here is a plain dataclass - no backend, no I/O.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "modules" / "tool-computer-use"))

import pytest
from amplifier_module_tool_computer_use.backend import BackendError, MonitorInfo
from amplifier_module_tool_computer_use.monitors import (
    PRIMARY,
    VIRTUAL_DESKTOP,
    select_monitor,
)

# The real four-monitor layout this feature was built for (see top-level report):
# DISPLAY3 primary at (0,0), DISPLAY2 at (5760,0), DISPLAY1 at (1946,-2160),
# DISPLAY4 at (5786,-2163) - two of the four at negative y origins.
REAL_LAYOUT = [
    MonitorInfo(
        id="DISPLAY3", x=0, y=0, width=3840, height=2160, primary=True, name="DISPLAY3"
    ),
    MonitorInfo(
        id="DISPLAY2",
        x=5760,
        y=0,
        width=3840,
        height=2160,
        primary=False,
        name="DISPLAY2",
    ),
    MonitorInfo(
        id="DISPLAY1",
        x=1946,
        y=-2160,
        width=3840,
        height=2160,
        primary=False,
        name="DISPLAY1",
    ),
    MonitorInfo(
        id="DISPLAY4",
        x=5786,
        y=-2163,
        width=3840,
        height=2160,
        primary=False,
        name="DISPLAY4",
    ),
]


def test_select_monitor_default_is_primary():
    chosen = select_monitor(REAL_LAYOUT, None)
    assert chosen.id == "DISPLAY3"
    assert chosen.primary


def test_select_monitor_explicit_primary_sentinel():
    chosen = select_monitor(REAL_LAYOUT, PRIMARY)
    assert chosen.id == "DISPLAY3"


def test_select_monitor_by_explicit_id():
    chosen = select_monitor(REAL_LAYOUT, "DISPLAY1")
    assert chosen.id == "DISPLAY1"
    assert chosen.x == 1946
    assert chosen.y == -2160


def test_select_monitor_by_explicit_id_negative_origin_display4():
    chosen = select_monitor(REAL_LAYOUT, "DISPLAY4")
    assert chosen.id == "DISPLAY4"
    assert chosen.x == 5786
    assert chosen.y == -2163


def test_select_monitor_unknown_id_fails_loud_and_lists_available():
    with pytest.raises(BackendError) as excinfo:
        select_monitor(REAL_LAYOUT, "DISPLAY99")
    message = str(excinfo.value)
    assert "DISPLAY99" in message
    # Every real candidate is named, so the error is immediately actionable.
    for mon in REAL_LAYOUT:
        assert mon.id in message


def test_select_monitor_empty_list_fails_loud():
    """No fallback, no synthetic monitor - an empty enumeration is a hard error."""
    with pytest.raises(BackendError):
        select_monitor([], None)
    with pytest.raises(BackendError):
        select_monitor([], "primary")
    with pytest.raises(BackendError):
        select_monitor([], "DISPLAY1")


def test_select_monitor_falls_back_to_first_when_none_flagged_primary():
    """Not a synthesized fallback: every candidate is real, enumerated data. This
    is a deterministic tie-break for missing metadata, not invented geometry."""
    no_primary = [
        MonitorInfo(id="eDP-1", x=0, y=0, width=1920, height=1080, primary=False),
        MonitorInfo(id="HDMI-1", x=1920, y=0, width=1920, height=1080, primary=False),
    ]
    chosen = select_monitor(no_primary, None)
    assert chosen.id == "eDP-1"  # first enumerated, deterministically


def test_virtual_desktop_sentinel_is_not_a_monitor_id():
    """VIRTUAL_DESKTOP is handled one layer up (ComputerTool), not here - but it
    must never accidentally collide with a real monitor id in this list."""
    assert VIRTUAL_DESKTOP not in {mon.id for mon in REAL_LAYOUT}
