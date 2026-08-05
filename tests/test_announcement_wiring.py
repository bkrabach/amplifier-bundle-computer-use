"""Regression test for the defect this fix closes: `overlay_linux.LinuxOverlay`,
`overlay_windows.WindowsOverlay`, and `announce_macos.announce` existed, were
covered by 392 lines of unit tests, and had ZERO production callers - nothing
in `mount()` (or anywhere else) ever imported or invoked them. A flag nobody
sets is the same as dead code.

These tests exercise `_build_announcement` - the function `mount()` calls
right after building the coexistence guard - and assert the banner
constructors are ACTUALLY invoked and ACTUALLY shown. Before this fix,
`amplifier_module_tool_computer_use` had no `_build_announcement` function,
no `LinuxOverlay`/`WindowsOverlay`/`macos_announce` imports, and no call site
in `mount()` at all - every test below would fail with an `AttributeError`
(the module has no `_build_announcement`) or a `ModuleNotFoundError`-shaped
import failure, not merely a wrong assertion. That is the exact defect class
the existing 392 lines of overlay/announce unit tests never caught, because
they only ever tested the three modules in isolation.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "modules" / "tool-computer-use"))

import amplifier_module_tool_computer_use as cu
from amplifier_module_tool_computer_use.coexistence_guard import CoexistenceGuard
from amplifier_module_tool_computer_use.geometry import Display
from amplifier_module_tool_computer_use.linux_x11 import LinuxX11Backend
from amplifier_module_tool_computer_use.presence import PresenceMonitor
from amplifier_module_tool_computer_use.windows import WindowsBackend

_DISPLAY = Display(
    screen_width=1920, screen_height=1080, model_width=1280, model_height=720
)


def _guard(platform: str = "linux-x11") -> CoexistenceGuard:
    presence = PresenceMonitor(idle_source=lambda: 999_999.0, platform=platform)
    return CoexistenceGuard(presence=presence, release_all=lambda reason: [])


# -- Linux: the persistent ambient overlay must actually be shown -----------


def test_linux_overlay_is_actually_constructed_and_shown(monkeypatch):
    shown = {"called": False}

    class _SpyOverlay:
        def __init__(self, display, **kwargs):
            self.display = display
            self.kwargs = kwargs

        def show(self):
            shown["called"] = True

    monkeypatch.setattr(cu, "LinuxOverlay", _SpyOverlay)

    backend = LinuxX11Backend({})
    backend._display = object()  # bypass a real X11 connection - wiring test only

    result = cu._build_announcement(backend, _guard(), {}, _DISPLAY)

    assert shown["called"] is True, (
        "LinuxOverlay.show() was never called - the banner exists but is "
        "unreachable, exactly the defect this test guards against"
    )
    assert isinstance(result, _SpyOverlay)


def test_linux_overlay_shares_the_guards_exclusion_zone(monkeypatch):
    captured = {}

    class _SpyOverlay:
        def __init__(self, display, **kwargs):
            captured.update(kwargs)

        def show(self):
            pass

    monkeypatch.setattr(cu, "LinuxOverlay", _SpyOverlay)

    backend = LinuxX11Backend({})
    backend._display = object()
    guard = _guard()

    cu._build_announcement(backend, guard, {}, _DISPLAY)

    assert captured["exclusion"] is guard.exclusion


def test_linux_overlay_disabled_by_config_is_not_constructed(monkeypatch):
    constructed = {"called": False}

    class _SpyOverlay:
        def __init__(self, display, **kwargs):
            constructed["called"] = True

        def show(self):
            pass

    monkeypatch.setattr(cu, "LinuxOverlay", _SpyOverlay)

    backend = LinuxX11Backend({})
    backend._display = object()

    result = cu._build_announcement(
        backend, _guard(), {"coexistence": {"announce": False}}, _DISPLAY
    )

    assert constructed["called"] is False
    assert result is None


# -- Windows: the standalone detached-process overlay must actually be shown -


def test_windows_overlay_is_actually_constructed_and_shown(monkeypatch):
    shown = {"called": False}
    hidden = {"called": False}

    class _SpyOverlay:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def show(self):
            shown["called"] = True

        def hide(self):
            hidden["called"] = True

    monkeypatch.setattr(cu, "WindowsOverlay", _SpyOverlay)
    registered = {}
    monkeypatch.setattr(
        cu.atexit, "register", lambda fn: registered.setdefault("fn", fn)
    )

    backend = WindowsBackend({})
    guard = _guard("windows-wsl2")

    result = cu._build_announcement(backend, guard, {}, _DISPLAY)

    assert shown["called"] is True, (
        "WindowsOverlay.show() was never called - the banner exists but is "
        "unreachable, exactly the defect this test guards against"
    )
    assert isinstance(result, _SpyOverlay)
    # Teardown must be registered somewhere (this codebase has no explicit
    # session-end hook for tool modules - atexit is the honest minimal stand-in).
    assert "fn" in registered
    registered["fn"]()
    assert hidden["called"] is True


# -- macOS: the announce-and-acknowledge dialog must actually be invoked ----


def test_macos_announce_is_actually_called_and_honored(monkeypatch):
    from amplifier_module_tool_computer_use.announce_macos import AnnounceResult
    from amplifier_module_tool_computer_use.macos import MacOSBackend

    called = {"count": 0}

    def _fake_announce(message, *, timeout_seconds, osascript_path="osascript"):
        called["count"] += 1
        assert str(timeout_seconds) in message  # §7.3: timeout must be disclosed
        return AnnounceResult(button="Continue", gave_up=False, raw_stdout="")

    monkeypatch.setattr(cu, "macos_announce", _fake_announce)

    backend = MacOSBackend({})
    result = cu._build_announcement(backend, _guard("macos"), {}, _DISPLAY)

    assert called["count"] == 1, (
        "announce_macos.announce() was never called - the banner exists but "
        "is unreachable, exactly the defect this test guards against"
    )
    assert result is None  # one-shot channel, nothing to keep alive


def test_macos_announce_declined_refuses_to_mount(monkeypatch):
    from amplifier_module_tool_computer_use.announce_macos import AnnounceResult
    from amplifier_module_tool_computer_use.macos import MacOSBackend

    monkeypatch.setattr(
        cu,
        "macos_announce",
        lambda message, *, timeout_seconds, osascript_path="osascript": AnnounceResult(
            button="Pause", gave_up=False, raw_stdout=""
        ),
    )

    backend = MacOSBackend({})
    try:
        cu._build_announcement(backend, _guard("macos"), {}, _DISPLAY)
        raised = False
    except cu.AnnouncementRefused:
        raised = True
    assert raised, "a human explicitly declining the dialog must refuse to mount"


def test_macos_announce_gave_up_while_quiet_proceeds(monkeypatch):
    from amplifier_module_tool_computer_use.announce_macos import AnnounceResult
    from amplifier_module_tool_computer_use.macos import MacOSBackend

    monkeypatch.setattr(
        cu,
        "macos_announce",
        lambda message, *, timeout_seconds, osascript_path="osascript": AnnounceResult(
            button=None, gave_up=True, raw_stdout=""
        ),
    )

    backend = MacOSBackend({})
    # idle_source reports a very long idle -> presence samples QUIET.
    result = cu._build_announcement(backend, _guard("macos"), {}, _DISPLAY)
    assert result is None  # proceeded, nothing to raise


def test_macos_announce_gave_up_while_human_active_refuses(monkeypatch):
    from amplifier_module_tool_computer_use.announce_macos import AnnounceResult
    from amplifier_module_tool_computer_use.macos import MacOSBackend

    monkeypatch.setattr(
        cu,
        "macos_announce",
        lambda message, *, timeout_seconds, osascript_path="osascript": AnnounceResult(
            button=None, gave_up=True, raw_stdout=""
        ),
    )

    backend = MacOSBackend({})
    # idle_source reports 0ms idle with no prior injection recorded -> HUMAN_ACTIVE.
    presence = PresenceMonitor(idle_source=lambda: 0.0, platform="macos")
    guard = CoexistenceGuard(presence=presence, release_all=lambda reason: [])

    try:
        cu._build_announcement(backend, guard, {}, _DISPLAY)
        raised = False
    except cu.AnnouncementRefused:
        raised = True
    assert raised, (
        "a timeout with a human detected present must never be treated as "
        "consent (§7.3 rules 2-3)"
    )


# -- no channel: a backend with no announcement mechanism is loud, not silent -


def test_backend_with_no_channel_logs_loudly_not_silently(caplog):
    class _FakeBackend:
        name = "some-future-backend"

    result = cu._build_announcement(_FakeBackend(), _guard(), {}, _DISPLAY)
    assert result is None
    assert any(
        "no announcement channel implemented" in rec.message for rec in caplog.records
    )
