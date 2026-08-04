"""Unit tests: `unattended_writes_ok` and `gate_writes` are two answers to the
SAME policy question (`docs/designs/remote-transport.md` §10.4) and must agree.

Before this fix, `DesktopTool.execute()`'s own fail-safe check for
`MUTATING_DESKTOP` actions (`focus_window`, `set_clipboard`) denied
unconditionally whenever `gate_writes` was True - with NO escape hatch of any
kind. That made the action unreachable even when:

  - the operator explicitly set `unattended_writes_ok: true` (hook-computer-use
    config), or
  - a human interactively approved the write via the `tool:pre` gate hook's
    `ask_user` prompt.

Real incident this closes: an agent driving a real remote Windows desktop with
`unattended_writes_ok: true` set was refused with "action 'focus_window'
requires confirmation (gate_writes) but no gate hook is registered to grant
it" - even though a gate hook *was* registered and had already decided to
allow the write.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "modules" / "tool-computer-use"))
sys.path.insert(0, str(ROOT / "modules" / "hook-computer-use"))

import amplifier_module_hook_computer_use as hook_mod
from amplifier_module_tool_computer_use import ComputerTool, DesktopTool
from amplifier_module_tool_computer_use.backend import BackendError, ScreenGeometry


class _FakeDesktopBackend:
    name = "remote-ssh:windows"
    is_remote = True

    def __init__(self) -> None:
        self.focus_calls: list[str] = []
        self._clipboard = ""

    def screen_geometry(self) -> ScreenGeometry:
        return ScreenGeometry(4, 4, 0, 0)

    def list_monitors(self):
        raise BackendError("no monitor enumeration on this fake")

    def cursor_position(self) -> tuple[int, int]:
        return (0, 0)

    def focus_window(self, handle: str) -> None:
        self.focus_calls.append(handle)

    def type_text(self, text, guard=None) -> None:  # pragma: no cover - unused
        pass

    def get_clipboard(self) -> str:
        return self._clipboard

    def set_clipboard(self, text: str) -> None:
        self._clipboard = text

    def close(self) -> None:  # pragma: no cover
        pass


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _make_desktop(cfg: dict | None = None):
    backend = _FakeDesktopBackend()
    computer = ComputerTool(backend, cfg or {})
    computer.resolve_display()
    desktop = DesktopTool(computer)
    return desktop, computer, backend


# -- the bug: gate_writes=True was an unconditional wall, no escape hatch ----


def test_gate_writes_blocks_focus_window_by_default_on_a_remote_target():
    """Baseline: with nothing else configured, `gate_writes` defaults on for a
    remote, non-read_only target and `focus_window` is refused. This is
    correct and must remain true after the fix (see the refusal-message test
    below for the exact remedy requirement)."""
    desktop, computer, _backend = _make_desktop({"read_only": False})
    assert computer._gate_writes is True

    result = _run(desktop.execute({"action": "focus_window", "handle": "abc"}))

    assert result.success is False
    assert "gate_writes" in result.error["message"]


def test_unattended_writes_ok_satisfies_gate_writes_for_focus_window():
    """THE FIX: once the hook has synced `unattended_writes_ok` onto the
    ComputerTool (see test_gate_hook.py for the hook-side half of this), the
    same fail-safe check in `DesktopTool.execute()` must let the write
    through - not deny it a second time after the hook already approved it.

    Fails without the fix: `_unattended_writes_ok` does not exist as an
    escape hatch at all, and the write is denied unconditionally.
    """
    desktop, computer, backend = _make_desktop({"read_only": False})
    assert computer._gate_writes is True
    computer._unattended_writes_ok = True  # what the gate hook syncs, per-call

    result = _run(desktop.execute({"action": "focus_window", "handle": "abc"}))

    assert result.success is True
    assert backend.focus_calls == ["abc"]


def test_unattended_writes_ok_satisfies_gate_writes_for_set_clipboard():
    desktop, computer, backend = _make_desktop({"read_only": False})
    computer._unattended_writes_ok = True

    result = _run(desktop.execute({"action": "set_clipboard", "text": "hello"}))

    assert result.success is True
    assert backend._clipboard == "hello"


def test_unattended_writes_ok_defaults_false_never_a_silent_escape_hatch():
    """No hook mounted, no config touched `_unattended_writes_ok` at all -
    it must default to False, so a bundle composed without hook-computer-use
    gets the safe (denying) behavior, never a silent bypass."""
    desktop, computer, _backend = _make_desktop({"read_only": False})
    assert computer._unattended_writes_ok is False

    result = _run(desktop.execute({"action": "focus_window", "handle": "abc"}))

    assert result.success is False


def test_explicit_gate_writes_false_still_works_unaffected():
    """The existing explicit opt-out path (`gate_writes: false`, logged as a
    deliberate choice) must be untouched by this change."""
    desktop, computer, backend = _make_desktop(
        {"read_only": False, "gate_writes": False}
    )
    assert computer._gate_writes is False

    result = _run(desktop.execute({"action": "focus_window", "handle": "abc"}))

    assert result.success is True
    assert backend.focus_calls == ["abc"]


# -- refusal message names the exact remedy, not just a doc pointer ----------


def test_refusal_message_names_every_concrete_remedy():
    """Three doc-pointer defects have already shipped in this repo where an
    error named a source the reader could not reach in the moment - the
    message itself must carry the remedies, not just point at a doc."""
    desktop, _computer, _backend = _make_desktop({"read_only": False})

    result = _run(desktop.execute({"action": "focus_window", "handle": "abc"}))

    message = result.error["message"]
    assert "unattended_writes_ok" in message
    assert "gate_writes: false" in message or "gate_writes=false" in message
    assert "hook-computer-use" in message
    # still points at the design rationale, but as a supplement, not the sole remedy
    assert "remote-transport.md" in message


# -- the hook side: syncing must happen before `execute()` can ever run -----


class _FakeCoordinator:
    def __init__(self, tools: dict) -> None:
        self._tools = tools

    def get(self, mount_point, name=None):
        if mount_point != "tools":
            return None
        return self._tools.get(name) if name else self._tools


def test_gate_hook_syncs_unattended_writes_ok_onto_the_computer_tool(monkeypatch):
    """The hook is the single source of truth for `unattended_writes_ok`
    (hook-computer-use's own config, per its `mount()` docstring). It must
    sync that value onto the SAME `ComputerTool` instance `DesktopTool`
    holds, on every `tool:pre` call, strictly before `execute()` runs for
    that call - so the tool-level fail-safe check agrees with the hook's
    decision instead of contradicting it."""
    monkeypatch.setattr(hook_mod, "_interactive_approval_possible", lambda: False)
    computer = ComputerTool(_FakeDesktopBackend(), {"read_only": False})
    computer.resolve_display()
    assert computer._unattended_writes_ok is False  # not yet synced

    handler = hook_mod._make_gate_handler(
        _FakeCoordinator({"computer": computer}), unattended_writes_ok=True
    )
    result = _run(
        handler(
            "tool:pre",
            {"tool_name": "computer", "tool_input": {"action": "left_click"}},
        )
    )

    assert result.action == "continue"
    assert computer._unattended_writes_ok is True


def test_gate_hook_syncs_false_too_never_stale_true_from_a_prior_call(monkeypatch):
    """Sync must happen on every call (not just once) so a later call with a
    handler configured `unattended_writes_ok=False` cannot inherit a stale
    `True` left behind by an earlier one."""
    monkeypatch.setattr(hook_mod, "_interactive_approval_possible", lambda: False)
    computer = ComputerTool(_FakeDesktopBackend(), {"read_only": False})
    computer.resolve_display()
    computer._unattended_writes_ok = True  # stale value from a hypothetical prior call

    handler = hook_mod._make_gate_handler(
        _FakeCoordinator({"computer": computer}), unattended_writes_ok=False
    )
    _run(
        handler(
            "tool:pre",
            {"tool_name": "computer", "tool_input": {"action": "left_click"}},
        )
    )

    assert computer._unattended_writes_ok is False
