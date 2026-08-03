"""Unit tests for `WindowsBackend.session_state()` (D-locked-screen, Windows side).

The real detection/refusal logic lives in `bridge.ps1`'s `Get-SessionState` +
its `SESSION_LOCKED:`/`NO_GUI_SESSION:` checks (no Windows target was reachable
to exercise that PowerShell directly - see the accompanying report). These
tests cover the Python-side contract: `session_state()` translates the
bridge's `{"locked": ..., "no_gui_session": ...}` JSON into the same
`(state, detail)` vocabulary `MacOSBackend._macos_session_state()` uses, via
the SAME `raw()` call path (mocked here) every other action already goes
through - no real `powershell.exe`, no real subprocess.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "modules" / "tool-computer-use"))

from amplifier_module_tool_computer_use.windows import WindowsBackend


def test_session_state_unlocked(monkeypatch):
    backend = WindowsBackend({})
    monkeypatch.setattr(
        backend,
        "raw",
        lambda action, **kw: {"ok": True, "locked": False, "no_gui_session": False},
    )

    state, detail = backend.session_state()

    assert state == "unlocked"
    assert detail


def test_session_state_locked(monkeypatch):
    backend = WindowsBackend({})
    monkeypatch.setattr(
        backend,
        "raw",
        lambda action, **kw: {
            "ok": True,
            "locked": True,
            "no_gui_session": False,
            "detail": "LogonUI.exe process present",
        },
    )

    state, detail = backend.session_state()

    assert state == "locked"
    assert "LogonUI" in detail


def test_session_state_no_gui_session(monkeypatch):
    backend = WindowsBackend({})
    monkeypatch.setattr(
        backend,
        "raw",
        lambda action, **kw: {
            "ok": True,
            "locked": False,
            "no_gui_session": True,
            "detail": "SystemInformation.VirtualScreen returned an empty rectangle",
        },
    )

    state, detail = backend.session_state()

    assert state == "no_gui_session"
    assert "VirtualScreen" in detail


def test_session_state_no_gui_session_wins_over_locked(monkeypatch):
    """If the bridge somehow reports both, no_gui_session is the more severe,
    more specific diagnosis (a locked session still HAS a GUI; a session with
    no GUI at all cannot be meaningfully described as merely "locked")."""
    backend = WindowsBackend({})
    monkeypatch.setattr(
        backend,
        "raw",
        lambda action, **kw: {"ok": True, "locked": True, "no_gui_session": True},
    )

    state, _ = backend.session_state()

    assert state == "no_gui_session"


def test_session_state_unknown_when_bridge_call_fails(monkeypatch):
    backend = WindowsBackend({})
    monkeypatch.setattr(
        backend,
        "raw",
        lambda action, **kw: {"ok": False, "error": "bridge produced no output"},
    )

    state, detail = backend.session_state()

    assert state == "unknown"
    assert "bridge produced no output" in detail


# -- capture()/mutating actions surface the bridge's SESSION_LOCKED/NO_GUI_SESSION
# error text through the existing, unmodified error-handling path -----------------
#
# bridge.ps1 itself refuses the underlying PowerShell action (see that file's
# `Get-SessionState` + the throw statements ahead of the switch) - these tests
# confirm windows.py's existing `raise BackendError(res.get("error", ...))`
# pattern (unchanged) faithfully surfaces that refusal rather than swallowing
# or rewording it, using the SAME mocked `raw()` seam as above.


def test_capture_surfaces_session_locked_error_from_bridge(monkeypatch):
    import pytest
    from amplifier_module_tool_computer_use.backend import BackendError

    backend = WindowsBackend({})
    monkeypatch.setattr(
        backend,
        "raw",
        lambda action, **kw: {
            "ok": False,
            "error": "SESSION_LOCKED: the Windows session is LOCKED (LogonUI.exe is running)",
        },
    )

    with pytest.raises(BackendError, match="SESSION_LOCKED"):
        backend.capture()


def test_click_surfaces_no_gui_session_error_from_bridge(monkeypatch):
    import pytest
    from amplifier_module_tool_computer_use.backend import BackendError

    backend = WindowsBackend({})
    monkeypatch.setattr(
        backend,
        "raw",
        lambda action, **kw: {
            "ok": False,
            "error": "NO_GUI_SESSION: no interactive GUI session is available",
        },
    )

    with pytest.raises(BackendError, match="NO_GUI_SESSION"):
        backend.click(10, 10)
