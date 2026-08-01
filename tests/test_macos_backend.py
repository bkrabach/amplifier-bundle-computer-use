"""Unit tests for `MacOSBackend` - runs on plain Linux CI with no Mac present.

Everything here is pure logic or a faked `Quartz` module - no real Core Graphics call
is ever made. Real end-to-end verification against a live Mac (Retina scale factor,
monitor enumeration, capture pixel content, Accessibility TCC status) lives in the
top-level report, not here - these tests guard the coordinate-conversion and combo-
parsing logic that verification depends on being correct, the same division of labor
`test_geometry.py` and `test_backend_monitors.py` already establish for the other two
backends.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "modules" / "tool-computer-use"))

import pytest
from amplifier_module_tool_computer_use import macos
from amplifier_module_tool_computer_use.backend import BackendError
from amplifier_module_tool_computer_use.macos import (
    _CG_FLAG_ALTERNATE,
    _CG_FLAG_COMMAND,
    _CG_FLAG_CONTROL,
    _CG_FLAG_SHIFT,
    MacOSBackend,
    _combo_flags_and_keycode,
)

# -- probe() -------------------------------------------------------------------
#
# This test suite runs on Linux (no Mac present, no pyobjc installed) - which means
# `MacOSBackend.probe()`'s very first check (platform) and its Quartz-import check
# are exercised for real here, not mocked. That is the point: these are exactly the
# two guards that must fire cleanly on every non-Mac CI/dev machine so this addition
# never breaks a Linux or Windows install (see module docstring's `_IMPORT_ERROR`
# discussion and D1 in `backend.py`).


def test_probe_unavailable_on_non_darwin_platform():
    backend = MacOSBackend({})
    result = backend.probe()
    assert result.available is False
    assert "darwin" in result.reason.lower() or "macos" in result.reason.lower()


def test_probe_unavailable_when_quartz_not_importable(monkeypatch):
    """On this box Quartz genuinely fails to import (no pyobjc installed) - this
    confirms `probe()` reports that specific, actionable reason rather than crashing,
    once the platform check itself is satisfied."""
    monkeypatch.setattr(macos.sys, "platform", "darwin")
    assert macos.Quartz is None  # true on any non-Mac dev/CI box
    backend = MacOSBackend({})
    result = backend.probe()
    assert result.available is False
    assert "quartz" in result.reason.lower()


def test_probe_never_raises_on_arbitrary_platform_string(monkeypatch):
    """`probe()` must never raise - even for an unexpected `sys.platform` value."""
    monkeypatch.setattr(macos.sys, "platform", "some-future-os")
    backend = MacOSBackend({})
    result = backend.probe()
    assert result.available is False


# -- registry probe order -------------------------------------------------------


def test_macos_registered_after_windows_and_linux_by_default():
    from amplifier_module_tool_computer_use.linux_x11 import LinuxX11Backend
    from amplifier_module_tool_computer_use.registry import BACKEND_FACTORIES
    from amplifier_module_tool_computer_use.windows import WindowsBackend

    assert BACKEND_FACTORIES.index(WindowsBackend) < BACKEND_FACTORIES.index(
        MacOSBackend
    )
    assert BACKEND_FACTORIES.index(LinuxX11Backend) < BACKEND_FACTORIES.index(
        MacOSBackend
    )


# -- combo parsing (key/hold_key) - pure logic, zero Quartz dependency ----------


def test_combo_single_key_no_modifiers():
    flags, keycode = _combo_flags_and_keycode("Return")
    assert flags == 0
    assert keycode == 0x24


def test_combo_single_modifier_plus_key():
    flags, keycode = _combo_flags_and_keycode("cmd+s")
    assert flags == _CG_FLAG_COMMAND
    assert keycode == 0x01  # kVK_ANSI_S


def test_combo_multiple_modifiers_combine_via_bitwise_or():
    flags, keycode = _combo_flags_and_keycode("ctrl+shift+a")
    assert flags == (_CG_FLAG_CONTROL | _CG_FLAG_SHIFT)
    assert keycode == 0x00  # kVK_ANSI_A


def test_combo_option_and_alt_are_the_same_modifier():
    f1, _ = _combo_flags_and_keycode("alt+a")
    f2, _ = _combo_flags_and_keycode("option+a")
    assert f1 == f2 == _CG_FLAG_ALTERNATE


def test_combo_case_insensitive():
    flags, keycode = _combo_flags_and_keycode("CMD+S")
    assert flags == _CG_FLAG_COMMAND
    assert keycode == 0x01


def test_combo_rejects_empty_string():
    with pytest.raises(BackendError, match="empty"):
        _combo_flags_and_keycode("")


def test_combo_rejects_unknown_key_name():
    with pytest.raises(BackendError, match="unknown key name"):
        _combo_flags_and_keycode("cmd+notarealkey")


def test_combo_rejects_modifiers_only():
    with pytest.raises(BackendError, match="only modifiers"):
        _combo_flags_and_keycode("cmd+shift")


def test_combo_function_key():
    _, keycode = _combo_flags_and_keycode("F1")
    assert keycode == 0x7A


# -- coordinate conversion: pixel <-> point, with a faked Quartz ----------------
#
# The single-display-Retina scenario below is exactly the configuration this backend
# was verified against for real (see the top-level report): the built-in display of
# a MacBook Pro, no external monitors. Mixed-DPI multi-monitor stitching is a
# documented, un-exercised limitation (see `_monitor_infos`'s docstring) - not tested
# here because pinning a "correct" answer for an inherently approximate case would
# be testing an opinion, not a contract.


class _FakeRect:
    def __init__(self, x: float, y: float, w: float, h: float) -> None:
        self.origin = types.SimpleNamespace(x=x, y=y)
        self.size = types.SimpleNamespace(width=w, height=h)


class _FakeQuartz:
    """Stand-in for the `Quartz` module, exposing only what `MacOSBackend`'s
    geometry/coordinate helpers touch."""

    def __init__(self, displays: list[dict]) -> None:
        self._displays = {d["id"]: d for d in displays}

    def CGGetActiveDisplayList(self, max_displays, _arr, _cnt):
        ids = list(self._displays.keys())
        return (0, ids, len(ids))

    def CGDisplayBounds(self, display_id):
        d = self._displays[display_id]
        return _FakeRect(*d["bounds"])

    def CGDisplayCopyDisplayMode(self, display_id):
        # Real Quartz returns an opaque CGDisplayModeRef; this fake just returns
        # the display_id itself as a token the two accessors below can look up.
        return display_id

    def CGDisplayModeGetPixelWidth(self, mode_token):
        return self._displays[mode_token]["pixel_w"]

    def CGDisplayModeGetPixelHeight(self, mode_token):
        return self._displays[mode_token]["pixel_h"]

    def CGMainDisplayID(self):
        return next(did for did, d in self._displays.items() if d.get("main"))

    def CGPointMake(self, x, y):
        return types.SimpleNamespace(x=x, y=y)


@pytest.fixture
def retina_backend(monkeypatch):
    """One display: 1440x900 points, 2880x1800 physical pixels - a 2x Retina
    backing scale, at the virtual-desktop origin. Mirrors a real MacBook Pro
    built-in display (exact point/pixel numbers vary by model; the 2x ratio and
    zero origin are what matters for this test)."""
    fake = _FakeQuartz(
        [
            {
                "id": 1,
                "bounds": (0, 0, 1440, 900),
                "pixel_w": 2880,
                "pixel_h": 1800,
                "main": True,
            }
        ]
    )
    monkeypatch.setattr(macos, "Quartz", fake)
    return MacOSBackend({})


def test_display_scale_is_measured_not_assumed(retina_backend):
    assert MacOSBackend._display_scale(1) == 2.0


def test_monitor_infos_reports_physical_pixel_dimensions(retina_backend):
    monitors = retina_backend._monitor_infos()
    assert len(monitors) == 1
    m = monitors[0]
    assert m.id == "1"
    assert (m.x, m.y) == (0, 0)
    assert (m.width, m.height) == (2880, 1800)  # physical pixels, not 1440x900 points
    assert m.primary is True


def test_list_monitors_matches_monitor_infos(retina_backend):
    assert retina_backend.list_monitors() == retina_backend._monitor_infos()


def test_covering_monitor_for_pixel_finds_the_only_display(retina_backend):
    m = retina_backend._covering_monitor_for_pixel(100, 100)
    assert m.id == "1"


def test_pixel_to_point_divides_by_backing_scale(retina_backend):
    """The core Retina trap this backend exists to get right: a physical-pixel
    SCREEN coordinate must be *halved* (divided by the 2x backing scale) before
    it is valid input to any `CGEvent*` call, which operates in logical points."""
    point = retina_backend._pixel_to_point(200, 100)
    assert (point.x, point.y) == (100.0, 50.0)


def test_point_to_pixel_multiplies_by_backing_scale():
    """The inverse: a point reported by `CGEventGetLocation` must be *doubled*
    to become a valid physical-pixel SCREEN coordinate (`cursor_position`'s
    contract)."""
    fake = _FakeQuartz(
        [
            {
                "id": 1,
                "bounds": (0, 0, 1440, 900),
                "pixel_w": 2880,
                "pixel_h": 1800,
                "main": True,
            }
        ]
    )
    backend = MacOSBackend({})
    import amplifier_module_tool_computer_use.macos as macos_mod

    macos_mod.Quartz = fake
    x, y = backend._point_to_pixel(100.0, 50.0)
    assert (x, y) == (200, 100)


def test_pixel_to_point_round_trips_through_point_to_pixel(retina_backend):
    for px, py in [(0, 0), (1, 1), (2879, 1799), (1440, 900)]:
        point = retina_backend._pixel_to_point(px, py)
        rx, ry = retina_backend._point_to_pixel(point.x, point.y)
        assert abs(rx - px) <= 1
        assert abs(ry - py) <= 1


def test_screen_geometry_single_display(retina_backend):
    geo = retina_backend.screen_geometry()
    assert (geo.width, geo.height) == (2880, 1800)
    assert (geo.origin_x, geo.origin_y) == (0, 0)


def test_list_monitors_fails_loud_on_zero_displays(monkeypatch):
    fake = _FakeQuartz([])
    monkeypatch.setattr(macos, "Quartz", fake)
    backend = MacOSBackend({})
    with pytest.raises(BackendError, match="zero active displays"):
        backend.list_monitors()


def test_covering_monitor_falls_back_to_primary_when_point_outside_all_monitors(
    monkeypatch,
):
    """Defensive fallback only (see `_covering_monitor_for_pixel` docstring): real
    callers always pass coordinates already clamped by `geometry.Display.to_screen`,
    but this must not raise or crash if it somehow receives an out-of-bounds point."""
    fake = _FakeQuartz(
        [
            {
                "id": 1,
                "bounds": (0, 0, 1440, 900),
                "pixel_w": 2880,
                "pixel_h": 1800,
                "main": True,
            }
        ]
    )
    monkeypatch.setattr(macos, "Quartz", fake)
    backend = MacOSBackend({})
    m = backend._covering_monitor_for_pixel(999999, 999999)
    assert m.id == "1"  # falls back to primary, does not raise


# -- probe() with a non-empty (mocked) display list -----------------------------


def test_probe_available_when_darwin_quartz_and_displays_present(monkeypatch):
    fake = _FakeQuartz(
        [
            {
                "id": 1,
                "bounds": (0, 0, 1440, 900),
                "pixel_w": 2880,
                "pixel_h": 1800,
                "main": True,
            }
        ]
    )
    monkeypatch.setattr(macos.sys, "platform", "darwin")
    monkeypatch.setattr(macos, "Quartz", fake)
    backend = MacOSBackend({})
    result = backend.probe()
    assert result.available is True


def test_probe_unavailable_when_zero_displays(monkeypatch):
    fake = _FakeQuartz([])
    monkeypatch.setattr(macos.sys, "platform", "darwin")
    monkeypatch.setattr(macos, "Quartz", fake)
    backend = MacOSBackend({})
    result = backend.probe()
    assert result.available is False
    assert "zero active displays" in result.reason
