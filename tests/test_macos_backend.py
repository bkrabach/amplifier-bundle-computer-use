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


# -- coexistence: presence_idle_ms / current_target (TASK 1) -------------------
#
# Everything below is a faked `Quartz` module - no real Core Graphics call is
# ever made, and no macOS hardware is touched (see the accompanying report:
# these behaviors are reasoned from documented Apple behavior and unit-tested
# here, but have not been independently re-verified against a live Mac).


class _FakePresenceQuartz:
    """Stand-in for `Quartz`, exposing only what `presence_idle_ms`/
    `current_target` touch."""

    def __init__(
        self, idle_seconds: float = 0.0, windows: list[dict] | None = None
    ) -> None:
        self.idle_seconds = idle_seconds
        self.windows = windows if windows is not None else []
        self.last_event_source_state = None
        self.last_event_type = None

    def CGEventSourceSecondsSinceLastEventType(self, state, event_type):
        self.last_event_source_state = state
        self.last_event_type = event_type
        return self.idle_seconds

    kCGEventSourceStateHIDSystemState = "HIDSystemState"
    kCGAnyInputEventType = "AnyInputEventType"

    def CGWindowListCopyWindowInfo(self, options, relative_to):
        return list(self.windows)

    kCGWindowListOptionOnScreenOnly = 1
    kCGWindowListExcludeDesktopElements = 16
    kCGNullWindowID = 0


def test_presence_idle_ms_converts_seconds_to_milliseconds(monkeypatch):
    fake = _FakePresenceQuartz(idle_seconds=0.0123)
    monkeypatch.setattr(macos, "Quartz", fake)
    backend = MacOSBackend({})

    idle_ms = backend.presence_idle_ms()

    assert idle_ms == pytest.approx(12.3, abs=0.001)


def test_presence_idle_ms_uses_hid_system_state_and_any_input_event(monkeypatch):
    """The two constants matter: HID system state (not combined-session
    state) is what a real human's hardware AND this backend's own
    `CGEventPost` calls both feed - see `presence_idle_ms`'s docstring."""
    fake = _FakePresenceQuartz(idle_seconds=1.0)
    monkeypatch.setattr(macos, "Quartz", fake)
    backend = MacOSBackend({})

    backend.presence_idle_ms()

    assert fake.last_event_source_state == fake.kCGEventSourceStateHIDSystemState
    assert fake.last_event_type == fake.kCGAnyInputEventType


def test_current_target_returns_frontmost_normal_layer_window(monkeypatch):
    fake = _FakePresenceQuartz(
        windows=[
            {"kCGWindowLayer": 0, "kCGWindowNumber": 4242},
            {"kCGWindowLayer": 0, "kCGWindowNumber": 99},
        ]
    )
    monkeypatch.setattr(macos, "Quartz", fake)
    backend = MacOSBackend({})

    assert backend.current_target() == "4242"


def test_current_target_skips_non_normal_layer_windows():
    """Menu bar / dock / overlay windows (`kCGWindowLayer != 0`) must not be
    reported as the frontmost target - matches `list_windows()`'s own
    filtering."""
    fake = _FakePresenceQuartz(
        windows=[
            {"kCGWindowLayer": 25, "kCGWindowNumber": 1},  # e.g. the dock
            {"kCGWindowLayer": 0, "kCGWindowNumber": 777},
        ]
    )
    import amplifier_module_tool_computer_use.macos as macos_mod

    macos_mod.Quartz = fake
    backend = MacOSBackend({})

    assert backend.current_target() == "777"


def test_current_target_returns_none_when_enumeration_fails():
    """§8.6: a read failure must report `None` (-> `TargetBinding` reports
    "unverified") rather than raising or guessing a handle."""

    class _BoomQuartz:
        def CGWindowListCopyWindowInfo(self, options, relative_to):
            raise RuntimeError("Screen Recording revoked")

        kCGWindowListOptionOnScreenOnly = "OnScreenOnly"
        kCGWindowListExcludeDesktopElements = "ExcludeDesktopElements"
        kCGNullWindowID = 0

    import amplifier_module_tool_computer_use.macos as macos_mod

    macos_mod.Quartz = _BoomQuartz()
    backend = MacOSBackend({})

    assert backend.current_target() is None


def test_current_target_returns_none_when_zero_windows():
    fake = _FakePresenceQuartz(windows=[])
    import amplifier_module_tool_computer_use.macos as macos_mod

    macos_mod.Quartz = fake
    backend = MacOSBackend({})

    assert backend.current_target() is None


# -- coexistence: per-character type_text guard wiring (TASK 1) ---------------


class _FakeTypeQuartz:
    """Stand-in for `Quartz`, exposing only what `type_text` (and the
    `_ensure_input_trusted` gate it calls) touch."""

    kCGHIDEventTap = "HIDEventTap"

    def __init__(self) -> None:
        self.posted_strings: list[str] = []

    def CGEventCreateKeyboardEvent(self, source, keycode, key_down):
        return {"keycode": keycode, "key_down": key_down, "unicode": ""}

    def CGEventKeyboardSetUnicodeString(self, event, length, text):
        event["unicode"] = text

    def CGEventPost(self, tap, event):
        self.posted_strings.append(event["unicode"])


class _FakeGuard:
    """Records before_event()/after_event() call order - no real
    `CoexistenceGuard`/`PresenceMonitor` needed to prove the per-character
    wiring shape itself."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def before_event(self) -> None:
        self.calls.append("before")

    def after_event(self) -> None:
        self.calls.append("after")


def test_type_text_posts_one_event_pair_per_character(monkeypatch):
    fake = _FakeTypeQuartz()
    monkeypatch.setattr(macos, "Quartz", fake)
    monkeypatch.setattr(MacOSBackend, "_ensure_input_trusted", lambda self: None)
    backend = MacOSBackend({})

    backend.type_text("ab")

    # Two characters -> two down events + two up events, one CGEvent PAIR
    # per character (not one CGEvent for the whole string) - this is what
    # makes per-keystroke guard checks meaningful at all (§5.2).
    assert fake.posted_strings == ["a", "a", "b", "b"]


def test_type_text_calls_guard_before_and_after_each_character(monkeypatch):
    fake = _FakeTypeQuartz()
    monkeypatch.setattr(macos, "Quartz", fake)
    monkeypatch.setattr(MacOSBackend, "_ensure_input_trusted", lambda self: None)
    backend = MacOSBackend({})
    guard = _FakeGuard()

    backend.type_text("xyz", guard=guard)

    assert guard.calls == [
        "before",
        "after",
        "before",
        "after",
        "before",
        "after",
    ]


def test_type_text_with_no_guard_skips_every_guard_call(monkeypatch):
    """Omitting `guard` (the default) must not invoke anything guard-shaped -
    existing callers with no guard are unaffected."""
    fake = _FakeTypeQuartz()
    monkeypatch.setattr(macos, "Quartz", fake)
    monkeypatch.setattr(MacOSBackend, "_ensure_input_trusted", lambda self: None)
    backend = MacOSBackend({})

    backend.type_text("hi")  # guard=None (default) - must not raise

    assert fake.posted_strings == ["h", "h", "i", "i"]
