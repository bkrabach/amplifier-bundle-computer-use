"""Regression tests for the band-lifetime fix (docs/designs/band-lifetime.md):
the disclosure band no longer lives for the tool's whole process lifetime
(the reported defect - a band up 6h37m after a single morning screenshot).

Scope, matching the adversarial review's exemption exactly:

1. The channel-keyed depth counter and Alt A inline lowering (no reaper
   thread, no trailing window `T`) - `test_f8_*`.
2. The held-input ledger, previously wired ONLY from `remote_agent.py`, now
   also wired into the LOCAL `left_mouse_down`/`left_mouse_up` path, and
   scoped per-channel instead of per-mount() - `test_local_ledger_*` and
   `test_channel_ledger_*`.
3. `WindowsOverlay.hide()`'s bounded, LOUD failure handling - a hung
   `Stop-Process` must never silently mark the band as torn down -
   `test_hide_*`.

No real backend, no real display server, no real PowerShell - matching
every other test file in this suite.
"""

from __future__ import annotations

import logging
import subprocess
import sys
import types
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "modules" / "tool-computer-use"))

import amplifier_module_tool_computer_use as cu
from amplifier_module_tool_computer_use.backend import BackendError, ScreenGeometry
from amplifier_module_tool_computer_use.ledger import HeldInputLedger
from amplifier_module_tool_computer_use.overlay_windows import WindowsOverlay


def _unique(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex}"


class _FakeBackend:
    """Minimal local backend - records mouse_down/mouse_up calls, matching
    `test_computer_tool_coexistence.py`'s `_FakeBackend` shape."""

    is_remote = False

    def __init__(self, name: str = "fake-local") -> None:
        self.name = name
        self.calls: list[tuple] = []

    def type_text(self, text: str) -> None:
        pass

    def screen_geometry(self) -> ScreenGeometry:
        return ScreenGeometry(1920, 1080, 0, 0)

    def list_monitors(self):
        raise BackendError("no monitor enumeration on this fake")

    def mouse_down(self, x, y, button="left") -> None:
        self.calls.append(("mouse_down", x, y, button))

    def mouse_up(self, x, y, button="left") -> None:
        self.calls.append(("mouse_up", x, y, button))

    def close(self) -> None:
        pass


class _FakeOverlay:
    """Stand-in for `LinuxOverlay`/`WindowsOverlay`: just enough surface
    (`show`/`hide`/`shown`) for `_live_band_handle` to recognize it as a
    live, re-raisable band."""

    def __init__(self) -> None:
        self.shown = False
        self.show_calls = 0
        self.hide_calls = 0

    def show(self) -> None:
        self.show_calls += 1
        self.shown = True

    def hide(self) -> None:
        self.hide_calls += 1
        self.shown = False


def _tool(backend: _FakeBackend) -> cu.ComputerTool:
    tool = cu.ComputerTool(backend, {})
    tool.resolve_display()
    return tool


# -- F8: a channel shared by two tools ---------------------------------------


def test_f8_one_tools_idle_depth_cannot_lower_a_band_the_other_is_driving_under():
    """FAILS WITHOUT THE FIX: before the channel-keyed depth counter, band
    state lived (conceptually) per instance - nothing stopped one tool from
    deciding "idle, lower the band" while a delegated sibling sharing the
    SAME overlay (`_announcement_decisions`) was actively driving under it.
    """
    channel_key = _unique("local:f8-test")
    tool_a = _tool(_FakeBackend(channel_key))
    tool_b = _tool(_FakeBackend(channel_key))

    overlay = _FakeOverlay()
    band_state = cu._get_channel_band_state(channel_key)
    ledger = cu._get_channel_ledger(channel_key)
    for tool in (tool_a, tool_b):
        tool._announcement = overlay
        tool._channel_key = channel_key
        tool._band_state = band_state
        tool._ledger = ledger

    token_a = tool_a._band_enter()
    assert overlay.shown is True
    assert overlay.show_calls == 1

    # Tool B starts a second, concurrent action on the SAME channel - the
    # overlay is already up, so this is a free no-op re-raise.
    token_b = tool_b._band_enter()
    assert overlay.show_calls == 1, "already up - must not re-raise"

    # Tool A's own action finishes and it goes idle - but tool B is STILL
    # driving. The band must stay up.
    tool_a._band_exit(token_a)
    assert overlay.shown is True, (
        "F8: tool A's own idle depth lowered a band tool B is still driving under"
    )
    assert overlay.hide_calls == 0

    # Only once tool B ALSO finishes is the channel genuinely idle.
    tool_b._band_exit(token_b)
    assert overlay.shown is False
    assert overlay.hide_calls == 1


def test_alt_a_lowers_the_band_between_episodes_and_reraises_for_the_next():
    """The other half of Alt A: the band actually comes DOWN once idle (the
    reported defect - a single screenshot leaving the band up for 6h37m),
    and comes back UP for the next episode with no re-consent."""
    channel_key = _unique("local:alt-a-test")
    tool = _tool(_FakeBackend(channel_key))
    overlay = _FakeOverlay()
    tool._announcement = overlay
    tool._channel_key = channel_key
    tool._band_state = cu._get_channel_band_state(channel_key)
    tool._ledger = cu._get_channel_ledger(channel_key)

    token = tool._band_enter()
    assert overlay.shown is True
    tool._band_exit(token)
    assert overlay.shown is False, "band must come DOWN once idle - the reported defect"
    assert overlay.hide_calls == 1

    token2 = tool._band_enter()
    assert overlay.shown is True
    assert overlay.show_calls == 2, "the next episode re-raises without asking anyone"
    tool._band_exit(token2)
    assert overlay.shown is False


def test_band_stays_up_while_a_mouse_button_is_held_even_at_depth_zero():
    """\u00a74.2's second invariant clause: a held button means force is still
    applied even though no `execute()` is in flight (F10)."""
    channel_key = _unique("local:held-input-test")
    tool = _tool(_FakeBackend(channel_key))
    overlay = _FakeOverlay()
    ledger = cu._get_channel_ledger(channel_key)
    tool._announcement = overlay
    tool._channel_key = channel_key
    tool._band_state = cu._get_channel_band_state(channel_key)
    tool._ledger = ledger

    token = tool._band_enter()
    ledger.hold("mouse", "mouse:left", lambda: None)  # simulate a button still down
    tool._band_exit(token)

    assert overlay.shown is True, "a held input must keep the band up at depth 0"
    assert overlay.hide_calls == 0

    ledger.release("mouse:left")
    # Nothing re-triggers a lower automatically (no reaper) - the NEXT
    # execute() will observe depth 0 + empty ledger and lower normally.
    token2 = tool._band_enter()
    tool._band_exit(token2)
    assert overlay.shown is False


# -- held-input ledger: local wiring (finding #1) ----------------------------


def test_left_mouse_down_registers_a_hold_and_up_releases_it():
    """FAILS WITHOUT THE FIX: `HeldInputLedger.hold()` was called ONLY from
    `remote_agent.py` - a LOCAL `left_mouse_down` had zero enforcement."""
    backend = _FakeBackend()
    computer = _tool(backend)
    computer._ledger = HeldInputLedger()

    computer._run("left_mouse_down", {})
    assert computer._ledger.held_tokens == ["mouse:left"]
    assert backend.calls == [("mouse_down", None, None, "left")]

    computer._run("left_mouse_up", {})
    assert computer._ledger.held_tokens == []
    assert backend.calls == [
        ("mouse_down", None, None, "left"),
        ("mouse_up", None, None, "left"),
    ]


def test_release_all_now_actually_releases_a_locally_held_mouse_button():
    """The safety consequence of finding #1: before this fix, a guard's
    `release_all` (halt/pause/target-change/deadman) was a no-op for a
    locally-held button - the ledger never knew about it."""
    backend = _FakeBackend()
    computer = _tool(backend)
    computer._ledger = HeldInputLedger()

    computer._run("left_mouse_down", {})
    assert backend.calls == [("mouse_down", None, None, "left")]

    released = computer._ledger.release_all(reason="halted")

    assert released == ["mouse:left"]
    assert backend.calls[-1] == ("mouse_up", None, None, "left"), (
        "release_all's release_fn must default to the button's last-known "
        "position when no explicit mouse_up ever supplied fresh coordinates"
    )


def test_no_ledger_means_mouse_down_up_behavior_is_unchanged():
    """Backward compatibility: a backend with no coexistence guard (so no
    channel-scoped ledger) must behave exactly as before this fix."""
    backend = _FakeBackend()
    computer = _tool(backend)
    assert computer._ledger is None

    computer._run("left_mouse_down", {})
    computer._run("left_mouse_up", {})

    assert backend.calls == [
        ("mouse_down", None, None, "left"),
        ("mouse_up", None, None, "left"),
    ]


class _PresenceBackend:
    """A local backend with a presence detector, so `_build_coexistence_guard`
    actually builds a guard (and, with this fix, a channel-scoped ledger).
    `presence_platform` is fixed to a real `GUARD_MS` key while `name` stays
    unique per test - `_channel_identity` (local) keys off `name` alone."""

    is_remote = False
    presence_platform = "linux-x11"

    def __init__(self, name: str) -> None:
        self.name = name

    def presence_idle_ms(self) -> float:
        return 999_999.0

    def current_target(self):
        return None


def test_channel_ledger_is_shared_across_every_mount_for_the_same_channel():
    """FAILS WITHOUT THE FIX: `_build_coexistence_guard` used to build a
    FRESH `HeldInputLedger()` on every call - a parent mount() and a
    delegated child mount() sharing one overlay each got their OWN,
    disconnected ledger (finding #2)."""
    channel_name = _unique("linux-x11-ledger-test")
    backend_a = _PresenceBackend(channel_name)
    backend_b = _PresenceBackend(channel_name)

    guard_a = cu._build_coexistence_guard(backend_a, {})
    guard_b = cu._build_coexistence_guard(backend_b, {})
    assert guard_a is not None
    assert guard_b is not None

    shared_ledger = cu._get_channel_ledger(cu._channel_identity(backend_a))
    shared_ledger.hold("mouse", "mouse:left", lambda: None)

    # guard_b's release_all must see the hold registered via the SHARED
    # channel ledger, not just holds made through guard_b's own path.
    released = guard_b.release_all("test")
    assert released == ["mouse:left"]


# -- WindowsOverlay.hide(): bounded, loud failure (finding #3) ---------------

FIXED_TOKEN = "test-token-band-lifetime"


def _pin_token(monkeypatch) -> None:
    from amplifier_module_tool_computer_use import overlay_windows

    monkeypatch.setattr(
        overlay_windows.uuid, "uuid4", lambda: types.SimpleNamespace(hex=FIXED_TOKEN)
    )


class _FakeStream:
    def __init__(self, data: bytes = b"") -> None:
        self._data = data

    def close(self) -> None:
        pass

    def read(self) -> bytes:
        return self._data


class _FakePopen:
    last_instance: _FakePopen | None = None

    def __init__(self, argv, **kwargs) -> None:  # noqa: ANN001 - test double
        self.argv = argv
        self.pid = 424242
        self.returncode: int | None = None
        self.stdin = _FakeStream()
        self.stdout = None
        self.stderr = (
            _FakeStream(b"") if kwargs.get("stderr") == subprocess.PIPE else None
        )
        _FakePopen.last_instance = self

    def poll(self):
        return self.returncode

    def kill(self) -> None:
        self.returncode = -9

    def wait(self, timeout=None) -> int:  # noqa: ANN001
        return self.returncode or 0


def _no_op_sweep(self) -> None:  # noqa: ANN001 - monkeypatch target
    pass


def _shown_overlay(monkeypatch, tmp_path) -> WindowsOverlay:
    """Build a WindowsOverlay in the `shown` state with no real Windows/
    PowerShell dependency, matching test_overlay_windows_lifetime.py's
    established pattern."""
    import json

    monkeypatch.setattr(WindowsOverlay, "_sweep_legacy_orphans", _no_op_sweep)
    monkeypatch.setattr(subprocess, "Popen", _FakePopen)
    # `show()` also calls `_translate()` (path translation, a real
    # `subprocess.run(["wslpath", ...])` call) - benign during setup; each
    # test below re-patches `subprocess.run` for the `hide()` call it's
    # actually testing.
    monkeypatch.setattr(
        subprocess, "run", lambda *a, **k: subprocess.CompletedProcess([], 0, "", "")
    )
    _pin_token(monkeypatch)
    overlay = WindowsOverlay(screen_width=1920, powershell_path=sys.executable)
    events_file = tmp_path / "overlay-events.ndjson"
    monkeypatch.setattr(overlay, "_events_path_wsl", lambda: str(events_file))
    events_file.write_text(
        json.dumps({"event": "ready", "pid": 424242, "token": FIXED_TOKEN}) + "\n",
        encoding="utf-8",
    )
    overlay.show()
    assert overlay.shown is True
    return overlay


def test_hide_does_not_silently_mark_the_band_hidden_when_stop_process_hangs(
    monkeypatch, tmp_path, caplog
):
    """FAILS WITHOUT THE FIX: a hung/timed-out `Stop-Process` used to be
    logged at DEBUG and `hide()` still unconditionally marked the overlay
    `shown=False` - claiming the band was down when it could not actually
    be confirmed. This is exactly the "silent per-channel deadlock/silent
    degradation" the adversarial review flagged (finding #3), applied to
    the one real blocking call in this teardown path."""
    overlay = _shown_overlay(monkeypatch, tmp_path)

    def _hanging_stop_process(argv, **kwargs):
        raise subprocess.TimeoutExpired(cmd=argv, timeout=kwargs.get("timeout", 15))

    monkeypatch.setattr(subprocess, "run", _hanging_stop_process)

    with caplog.at_level(logging.ERROR):
        overlay.hide()

    assert overlay.shown is True, (
        "a hung Stop-Process must never silently mark the overlay hidden - "
        "fail toward 'still up', the safe direction (F6's own rule)"
    )
    assert overlay.pid is not None
    error_records = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert error_records, "a hung Stop-Process must be LOUD, never silent"
    assert any("STILL BE VISIBLE" in r.message for r in error_records)


def test_hide_still_tears_down_cleanly_when_stop_process_succeeds(
    monkeypatch, tmp_path
):
    """Sanity check alongside the failure test above: the ordinary path is
    unaffected by this fix."""
    overlay = _shown_overlay(monkeypatch, tmp_path)
    monkeypatch.setattr(
        subprocess, "run", lambda *a, **k: subprocess.CompletedProcess([], 0, "", "")
    )

    overlay.hide()

    assert overlay.shown is False
    assert overlay.pid is None
