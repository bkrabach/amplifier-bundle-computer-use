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
    attribute_monitor,
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


# -- attribute_monitor: the join that was missing (see module docstring) -------
#
# This is the fix for a real incident: `focus_window` raised a window on
# DISPLAY2 while capture was scoped to DISPLAY3. The window list gave no
# monitor/geometry data at all, so the caller could not tell "focus_window
# did nothing" from "focus_window worked, just not where you're looking" -
# three sessions in a row misdiagnosed a working `focus_window` as broken.


def test_attribute_monitor_picks_the_containing_monitor():
    """Fails without the fix: before `attribute_monitor` existed, there was no
    way to answer "which monitor is this window on" at all."""
    rect = (5760 + 100, 100, 5760 + 900, 900)  # fully inside DISPLAY2
    assert attribute_monitor(rect, REAL_LAYOUT) == "DISPLAY2"


def test_attribute_monitor_picks_largest_overlap_when_straddling_a_boundary():
    """A window mostly on DISPLAY3 with a sliver hanging into DISPLAY2's
    space must attribute to DISPLAY3 - the monitor with the larger real
    intersection area, not the first one checked."""
    rect = (3700, 100, 3900, 900)  # 140px on DISPLAY3, 60px into DISPLAY2's space
    assert attribute_monitor(rect, REAL_LAYOUT) == "DISPLAY3"


def test_attribute_monitor_none_when_rect_is_none():
    """The exact real-world case this must NOT paper over: this backend could
    not determine the window's geometry at all. No guess - `None`."""
    assert attribute_monitor(None, REAL_LAYOUT) is None


def test_attribute_monitor_none_when_no_monitors_enumerated():
    assert attribute_monitor((0, 0, 100, 100), []) is None


def test_attribute_monitor_none_when_off_every_monitor():
    """Real, not hypothetical: Windows parks minimized windows off-screen
    (commonly around (-32000, -32000), a genuine `GetWindowRect` result).
    `attribute_monitor` must report `None`, not the nearest/primary monitor -
    that would be exactly the kind of fabricated attribution this feature
    exists to avoid."""
    minimized_rect = (-32000, -32000, -31840, -31832)
    assert attribute_monitor(minimized_rect, REAL_LAYOUT) is None


def test_attribute_monitor_is_the_real_incident_scenario():
    """The decisive scenario from the top-level report: `computer` is scoped
    to DISPLAY3 (the capture target); `focus_window` actually raised the
    window on DISPLAY2. `attribute_monitor` must say so explicitly, which is
    what lets `ComputerTool._focus_monitor_warning` tell the caller "it
    worked, just not where you're looking" instead of looking identical to
    a silent no-op."""
    window_rect_on_display2 = (5760 + 200, 200, 5760 + 1400, 1000)
    landed = attribute_monitor(window_rect_on_display2, REAL_LAYOUT)
    assert landed == "DISPLAY2"
    capture_target = "DISPLAY3"
    assert landed != capture_target  # the mismatch the warning must catch
