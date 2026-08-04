"""Unit tests: `WindowInfo.rect` is populated by every backend's `list_windows()`,
and `monitors.attribute_monitor` (see `test_monitors.py`) is the join that turns
that geometry into "which monitor is this window actually on".

This is the fix for a real incident (see the top-level report): `list_windows`
used to report a window's handle and title but never where it was, so a
`focus_window` call that raised a window on a monitor other than the one
`computer` was capturing looked, from the caller's side, identical to a
`focus_window` call that silently did nothing. Three sessions in a row
misdiagnosed a working `focus_window` as broken because of exactly this gap.

Every test here is a parsing/computation test against faked platform data - no
real desktop, no real X server, no real Quartz - matching the existing division
of labor `test_backend_monitors.py` and `test_macos_backend.py` already
establish for `list_monitors()`.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "modules" / "tool-computer-use"))

from amplifier_module_tool_computer_use.linux_x11 import LinuxX11Backend
from amplifier_module_tool_computer_use.windows import WindowsBackend

# -- WindowsBackend.list_windows(): rect from GetWindowRect (bridge.ps1) -------
#
# Real `bridge.ps1` shape (see that script's `list_windows` case): each entry
# already carries `rect = @($r.L, $r.T, $r.R, $r.B)` from `GetWindowRect` - this
# was always fetched, but `windows.py` discarded it before this fix. FAILS
# WITHOUT THE FIX: on the pre-fix code, `WindowInfo` has no `rect` field at
# all, so `w.rect` below raises `AttributeError`.

_REAL_LIST_WINDOWS_RESPONSE = {
    "ok": True,
    "foreground": "1001",
    "windows": [
        {
            "handle": "1001",
            "title": "Visual Studio Code",
            "pid": 4242,
            "minimized": False,
            "rect": [5760 + 100, 100, 5760 + 1700, 1100],  # on DISPLAY2
        },
        {
            "handle": "2002",
            "title": "Minimized Notepad",
            "pid": 4243,
            "minimized": True,
            # Real Windows behavior: minimized windows are parked off-screen.
            "rect": [-32000, -32000, -31840, -31832],
        },
        {
            "handle": "3003",
            "title": "No geometry available",
            "pid": 4244,
            "minimized": False,
            # Malformed/missing rect must decode to None, never a guess.
        },
    ],
}


def test_windows_list_windows_parses_rect_from_bridge_response(monkeypatch):
    backend = WindowsBackend({})
    monkeypatch.setattr(
        backend, "raw", lambda action, **kw: _REAL_LIST_WINDOWS_RESPONSE
    )

    result = backend.list_windows()

    by_handle = {w.handle: w for w in result.windows}
    assert by_handle["1001"].rect == (5860, 100, 7460, 1100)
    assert by_handle["2002"].rect == (-32000, -32000, -31840, -31832)
    assert by_handle["3003"].rect is None


def test_windows_list_windows_rect_missing_entirely_is_none(monkeypatch):
    backend = WindowsBackend({})
    monkeypatch.setattr(
        backend,
        "raw",
        lambda action, **kw: {
            "ok": True,
            "foreground": None,
            "windows": [{"handle": "5", "title": "no rect key", "minimized": False}],
        },
    )

    result = backend.list_windows()

    assert result.windows[0].rect is None


# -- LinuxX11Backend.list_windows(): rect via get_geometry + translate_coords --


class _FakeXlibRect:
    def __init__(self, width: int, height: int) -> None:
        self.width = width
        self.height = height


class _FakeTranslateCoords:
    def __init__(self, x: int, y: int) -> None:
        self.x = x
        self.y = y


class _FakeXlibWindow:
    """Stands in for an Xlib `Window` resource - only what `_window_rect` and
    `list_windows()` touch."""

    def __init__(
        self,
        wid: int,
        title: str = "",
        geom: tuple[int, int] | None = (800, 600),
        origin: tuple[int, int] | None = (100, 50),
    ) -> None:
        self._wid = wid
        self._title = title
        self._geom = geom
        self._origin = origin

    def get_full_property(self, atom, atype):
        if atom == "NAME":
            return types.SimpleNamespace(value=self._title.encode("utf-8"))
        if atom == "STATE":
            return None
        return None

    def get_wm_name(self):
        return self._title

    def get_geometry(self):
        if self._geom is None:
            raise RuntimeError("BadWindow: window vanished")
        w, h = self._geom
        return _FakeXlibRect(w, h)

    def translate_coords(self, _dst, _x, _y):
        if self._origin is None:
            raise RuntimeError("BadWindow: window vanished")
        x, y = self._origin
        return _FakeTranslateCoords(x, y)


def test_linux_x11_window_rect_uses_translate_coords_not_parent_relative_geometry():
    """The bug `get_geometry()` alone would introduce: its x/y are relative to
    the immediate parent (often a WM reparenting frame), not the root window.
    `_window_rect` must resolve through `translate_coords` against root instead."""
    backend = LinuxX11Backend({"display": ":99"})
    backend._root = object()  # only identity matters - translate_coords ignores it
    win = _FakeXlibWindow(1, geom=(800, 600), origin=(1946, -2160))  # e.g. DISPLAY1

    rect = backend._window_rect(win)

    assert rect == (1946, -2160, 1946 + 800, -2160 + 600)


def test_linux_x11_window_rect_none_on_protocol_error():
    """A window that disappeared between `_NET_CLIENT_LIST` and this call must
    report `rect=None`, not crash the whole `list_windows()` call."""
    backend = LinuxX11Backend({"display": ":99"})
    backend._root = object()
    win = _FakeXlibWindow(1, geom=None, origin=None)

    assert backend._window_rect(win) is None
