"""Unit tests for `Backend.list_monitors()` on both concrete backends.

Neither test touches a real desktop: `WindowsBackend.raw()` and
`LinuxX11Backend`'s X connection are faked, exercising only the parsing logic
that turns each platform's raw monitor data into `MonitorInfo` objects. Real
end-to-end verification against a live four-monitor Windows desktop lives in
the top-level report, not here - these tests guard the parsing logic that
verification depends on being correct, and run on plain Linux CI with no
Windows or X server present.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "modules" / "tool-computer-use"))

import pytest
from amplifier_module_tool_computer_use.backend import BackendError
from amplifier_module_tool_computer_use.linux_x11 import LinuxX11Backend
from amplifier_module_tool_computer_use.windows import WindowsBackend

# -- WindowsBackend.list_monitors -----------------------------------------------

# The real four-monitor `screen_info` payload shape this feature was built for
# (see top-level report): a virtual-desktop bounding box plus per-monitor
# `screens`, two of which have negative-origin bounds.
REAL_SCREEN_INFO = {
    "ok": True,
    "width": 9626,
    "height": 4323,
    "x": 1946,
    "y": -2163,
    "screens": [
        {"name": "DISPLAY3", "primary": True, "bounds": [0, 0, 3840, 2160]},
        {"name": "DISPLAY2", "primary": False, "bounds": [5760, 0, 3840, 2160]},
        {"name": "DISPLAY1", "primary": False, "bounds": [1946, -2160, 3840, 2160]},
        {"name": "DISPLAY4", "primary": False, "bounds": [5786, -2163, 3840, 2160]},
    ],
}


def test_windows_list_monitors_parses_real_layout(monkeypatch):
    backend = WindowsBackend({})
    monkeypatch.setattr(backend, "raw", lambda action, **kw: REAL_SCREEN_INFO)

    monitors = backend.list_monitors()

    assert [m.id for m in monitors] == ["DISPLAY3", "DISPLAY2", "DISPLAY1", "DISPLAY4"]
    primary = [m for m in monitors if m.primary]
    assert len(primary) == 1
    assert primary[0].id == "DISPLAY3"

    by_id = {m.id: m for m in monitors}
    assert (by_id["DISPLAY1"].x, by_id["DISPLAY1"].y) == (1946, -2160)
    assert (by_id["DISPLAY4"].x, by_id["DISPLAY4"].y) == (5786, -2163)
    for m in monitors:
        assert (m.width, m.height) == (3840, 2160)


def test_windows_list_monitors_fails_loud_when_screen_info_errors(monkeypatch):
    backend = WindowsBackend({})
    monkeypatch.setattr(
        backend, "raw", lambda action, **kw: {"ok": False, "error": "bridge down"}
    )
    with pytest.raises(BackendError, match="bridge down"):
        backend.list_monitors()


def test_windows_list_monitors_fails_loud_when_screens_missing(monkeypatch):
    """No fallback to a single synthetic monitor - an empty/missing 'screens'
    key means genuine inability to enumerate, not "assume one monitor"."""
    backend = WindowsBackend({})
    monkeypatch.setattr(
        backend,
        "raw",
        lambda action, **kw: {
            "ok": True,
            "width": 1920,
            "height": 1080,
            "x": 0,
            "y": 0,
        },
    )
    with pytest.raises(BackendError, match="no per-monitor data"):
        backend.list_monitors()


def test_windows_list_monitors_fails_loud_on_malformed_bounds(monkeypatch):
    backend = WindowsBackend({})
    monkeypatch.setattr(
        backend,
        "raw",
        lambda action, **kw: {
            "ok": True,
            "screens": [{"name": "DISPLAY1", "primary": True, "bounds": [0, 0]}],
        },
    )
    with pytest.raises(BackendError, match="malformed monitor entry"):
        backend.list_monitors()


# -- LinuxX11Backend.list_monitors -----------------------------------------------


class _FakeRandrMonitor:
    def __init__(self, name, primary, x, y, w, h) -> None:
        self.name = name
        self.primary = primary
        self.x = x
        self.y = y
        self.width_in_pixels = w
        self.height_in_pixels = h


class _FakeMonitorsReply:
    def __init__(self, monitors) -> None:
        self.monitors = monitors


class _FakeDisplay:
    def __init__(self, atom_names: dict[int, str]) -> None:
        self._atom_names = atom_names

    def get_atom_name(self, atom: int) -> str:
        return self._atom_names[atom]


class _FakeRoot:
    def __init__(self, reply: _FakeMonitorsReply | None, has_method: bool = True):
        self._reply = reply
        if has_method:
            self.xrandr_get_monitors = lambda is_active=True: self._reply


def _connected_backend(root: _FakeRoot, display: _FakeDisplay) -> LinuxX11Backend:
    backend = LinuxX11Backend({"display": ":99"})
    backend._ensure_connected = lambda: None  # type: ignore[method-assign]
    backend._root = root
    backend._display = display
    return backend


def test_linux_x11_list_monitors_parses_randr_reply():
    # Atoms are opaque ints on the wire; get_atom_name resolves them to names.
    atom_names = {101: "eDP-1", 102: "HDMI-1"}
    monitors_raw = [
        _FakeRandrMonitor(101, True, 0, 0, 1920, 1080),
        _FakeRandrMonitor(102, False, 1920, 0, 1920, 1080),
    ]
    root = _FakeRoot(_FakeMonitorsReply(monitors_raw))
    backend = _connected_backend(root, _FakeDisplay(atom_names))

    monitors = backend.list_monitors()

    assert [m.id for m in monitors] == ["eDP-1", "HDMI-1"]
    assert monitors[0].primary is True
    assert monitors[1].primary is False
    assert (monitors[1].x, monitors[1].y) == (1920, 0)


def test_linux_x11_list_monitors_handles_negative_origin():
    atom_names = {201: "DP-1", 202: "DP-2"}
    monitors_raw = [
        _FakeRandrMonitor(201, True, 0, 0, 3840, 2160),
        _FakeRandrMonitor(202, False, 1946, -2160, 3840, 2160),
    ]
    root = _FakeRoot(_FakeMonitorsReply(monitors_raw))
    backend = _connected_backend(root, _FakeDisplay(atom_names))

    monitors = backend.list_monitors()
    by_id = {m.id: m for m in monitors}
    assert (by_id["DP-2"].x, by_id["DP-2"].y) == (1946, -2160)


def test_linux_x11_list_monitors_fails_loud_when_randr_missing():
    """No RandR >= 1.5 support (or none at all) must fail loud, not silently
    pretend there is one monitor - the exact hard rule this feature must honor."""
    root = _FakeRoot(reply=None, has_method=False)
    backend = _connected_backend(root, _FakeDisplay({}))

    with pytest.raises(BackendError, match="RandR"):
        backend.list_monitors()


def test_linux_x11_list_monitors_fails_loud_on_zero_monitors():
    root = _FakeRoot(_FakeMonitorsReply([]))
    backend = _connected_backend(root, _FakeDisplay({}))

    with pytest.raises(BackendError, match="zero active monitors"):
        backend.list_monitors()


def test_linux_x11_list_monitors_survives_atom_lookup_failure():
    """A monitor with an unnamed/unlookupable RandR output atom still comes back
    as a real (if less descriptively named) monitor, not a crash."""

    class _RaisingDisplay(_FakeDisplay):
        def get_atom_name(self, atom: int) -> str:
            raise RuntimeError("no such atom")

    monitors_raw = [_FakeRandrMonitor(999, True, 0, 0, 1024, 768)]
    root = _FakeRoot(_FakeMonitorsReply(monitors_raw))
    backend = _connected_backend(root, _RaisingDisplay({}))

    monitors = backend.list_monitors()
    assert len(monitors) == 1
    assert monitors[0].id == "monitor-0"  # positional fallback id, not a crash
