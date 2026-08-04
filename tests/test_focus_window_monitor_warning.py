"""THE decisive test: reproduce the real incident end-to-end through
`ComputerTool` and prove it's gone.

Real incident (see top-level report): driving a real 4-monitor Windows
desktop, `focus_window(791284)` returned success and the foreground handle
genuinely changed - but the screenshot was unchanged, exactly like the two
attempts before it. Two prior sessions concluded `focus_window` was broken.
It was working every time: the window landed on DISPLAY2 while `computer`'s
capture was scoped to DISPLAY3. `list_windows` gave no monitor/geometry data
at all, so there was no way to tell "focus worked, wrong monitor" from "focus
silently did nothing."

These tests build a `ComputerTool` scoped to DISPLAY3 (mirroring the real
four-monitor layout in `test_monitors.py`/`test_backend_monitors.py`) against
a fake backend that raises the requested window onto DISPLAY2 - the exact
scenario - and prove:

  1. `list_windows` now reports which monitor each window is actually on.
  2. `focus_window` now tells the caller, in the result text itself, when the
     window it just raised landed on a monitor other than the one being
     captured - FAILS WITHOUT THE FIX: before this change, `_run`'s
     `focus_window` branch returned only `f"focused window {handle}"`, with
     no way to distinguish a real cross-monitor focus from a silent no-op.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "modules" / "tool-computer-use"))

# ComputerTool imports amplifier_core.models.ToolResult at module load time;
# this suite runs without amplifier_core installed, same shim pattern
# test_remote_monitor_scoping_e2e.py already uses.
if "amplifier_core" not in sys.modules:
    _core = types.ModuleType("amplifier_core")
    _models = types.ModuleType("amplifier_core.models")

    class _ToolResult:
        def __init__(self, success=True, output=None, error=None) -> None:
            self.success = success
            self.output = output
            self.error = error

    _models.ToolResult = _ToolResult  # type: ignore[attr-defined]
    _core.models = _models  # type: ignore[attr-defined]
    sys.modules["amplifier_core"] = _core
    sys.modules["amplifier_core.models"] = _models

from amplifier_module_tool_computer_use import ComputerTool
from amplifier_module_tool_computer_use.backend import (
    BackendError,
    MonitorInfo,
    ScreenGeometry,
    WindowInfo,
    WindowList,
)

# The real four-monitor layout this feature was built for (see top-level
# report and test_monitors.py's REAL_LAYOUT): DISPLAY3 primary at (0,0),
# DISPLAY2 at (5760,0).
_MONITORS = [
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
]


class _FakeIncidentBackend:
    """`focus_window` genuinely raises the window (foreground changes, exactly
    as the real Windows bridge did) - but it's on DISPLAY2, not the DISPLAY3
    `computer` is scoped to. Nothing here lies: this is what a WORKING
    `focus_window` looks like when the target window lives on another
    monitor."""

    name = "fake-incident-backend"

    def __init__(self) -> None:
        self.focus_calls: list[str] = []
        # Starts on DISPLAY3 (foreground before any focus_window call).
        self._foreground = "100"
        self._rects = {
            "100": (100, 100, 900, 900),  # on DISPLAY3
            "791284": (5760 + 200, 200, 5760 + 1400, 1000),  # on DISPLAY2
        }

    def screen_geometry(self) -> ScreenGeometry:
        return ScreenGeometry(width=9600, height=2160, origin_x=0, origin_y=0)

    def list_monitors(self) -> list[MonitorInfo]:
        return _MONITORS

    def cursor_position(self) -> tuple[int, int]:
        return (0, 0)

    def list_windows(self) -> WindowList:
        return WindowList(
            windows=[
                WindowInfo(
                    handle="100",
                    title="Terminal",
                    minimized=False,
                    rect=self._rects["100"],
                ),
                WindowInfo(
                    handle="791284",
                    title="Visual Studio Code",
                    minimized=False,
                    rect=self._rects["791284"],
                ),
            ],
            foreground=self._foreground,
        )

    def focus_window(self, handle: str) -> None:
        # Genuinely raises the window - foreground handle really changes,
        # exactly like the real incident ("the foreground handle changed").
        self.focus_calls.append(handle)
        self._foreground = handle

    def type_text(self, text, guard=None) -> None:  # pragma: no cover - unused
        pass

    def get_clipboard(self) -> str:  # pragma: no cover - unused
        return ""

    def set_clipboard(self, text: str) -> None:  # pragma: no cover - unused
        pass

    def close(self) -> None:  # pragma: no cover
        pass


def _make_computer(
    target_monitor: str = "DISPLAY3",
) -> tuple[ComputerTool, _FakeIncidentBackend]:
    backend = _FakeIncidentBackend()
    computer = ComputerTool(
        backend, {"read_only": False, "target_monitor": target_monitor}
    )
    computer.resolve_display()
    return computer, backend


# -- list_windows: monitor attribution is now reported ------------------------


def test_list_windows_reports_which_monitor_each_window_is_on():
    computer, _backend = _make_computer()

    summary, _ = computer._run("list_windows", {})

    assert "[100] Terminal (monitor='DISPLAY3')" in summary
    assert "[791284] Visual Studio Code (monitor='DISPLAY2')" in summary


# -- focus_window: THE decisive reproduction ----------------------------------


def test_focus_window_on_a_different_monitor_now_warns_instead_of_looking_silent():
    """FAILS WITHOUT THE FIX: pre-fix, this assertion string never appears -
    the summary is just "focused window 791284", indistinguishable from a
    silent no-op."""
    computer, backend = _make_computer(target_monitor="DISPLAY3")

    summary, _ = computer._run("focus_window", {"handle": "791284"})

    # The focus genuinely happened - never in doubt, and must stay proven.
    assert backend.focus_calls == ["791284"]
    # THE fix: the caller is told, explicitly, that it landed elsewhere.
    assert "warning" in summary
    assert "DISPLAY2" in summary
    assert "DISPLAY3" in summary
    assert "desktop.select_monitor" in summary


def test_focus_window_on_the_current_monitor_gets_no_spurious_warning():
    """The other half of the contract: when the window IS on the capture
    target, there must be no warning at all - only genuine cross-monitor
    focuses get flagged."""
    computer, backend = _make_computer(target_monitor="DISPLAY3")

    summary, _ = computer._run("focus_window", {"handle": "100"})

    assert backend.focus_calls == ["100"]
    assert "warning" not in summary


def test_focus_window_warning_absent_in_virtual_desktop_mode():
    """Virtual-desktop mode captures everything - there is nothing to warn
    about, regardless of which monitor the window lands on."""
    computer, backend = _make_computer(target_monitor="virtual-desktop")

    summary, _ = computer._run("focus_window", {"handle": "791284"})

    assert backend.focus_calls == ["791284"]
    assert "warning" not in summary


def test_focus_window_warns_honestly_when_window_geometry_is_unavailable():
    """A backend that cannot report geometry for the focused window must get
    an honest "could not verify" warning, never a false "same monitor"
    silence and never a fabricated "different monitor" claim."""
    computer, backend = _make_computer(target_monitor="DISPLAY3")

    class _NoGeometryList:
        def list_windows(self):
            return WindowList(
                windows=[WindowInfo(handle="791284", title="VS Code", minimized=False)],
                foreground="791284",
            )

    backend.list_windows = _NoGeometryList().list_windows  # type: ignore[method-assign]

    summary, _ = computer._run("focus_window", {"handle": "791284"})

    assert "could not verify" in summary


def test_focus_window_warning_degrades_gracefully_when_enumeration_fails():
    """A transient `list_windows`/`list_monitors` failure after a successful
    focus must not blow up the whole action - the focus already happened and
    must be reported; only the warning is skipped."""
    computer, backend = _make_computer(target_monitor="DISPLAY3")

    def _boom():
        raise BackendError("target went away mid-call")

    backend.list_windows = _boom  # type: ignore[method-assign]

    summary, _ = computer._run("focus_window", {"handle": "791284"})

    assert backend.focus_calls == ["791284"]
    assert "focused window 791284" in summary
