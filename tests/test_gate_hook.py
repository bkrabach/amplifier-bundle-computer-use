"""Unit tests for the write-confirmation gate hook (`_make_gate_handler` in
hook-computer-use) - `docs/designs/remote-transport.md` \u00a710.4: "gate every
WRITE, or gate none". No real coordinator, no real backend, no network.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "modules" / "tool-computer-use"))
sys.path.insert(0, str(ROOT / "modules" / "hook-computer-use"))

import asyncio

import amplifier_module_hook_computer_use as hook_mod


class _FakeBackend:
    name = "remote-ssh:macos"


class _FakeComputerTool:
    def __init__(self, gate_writes: bool) -> None:
        self._gate_writes = gate_writes
        self._backend = _FakeBackend()


class _FakeCoordinator:
    def __init__(self, tools: dict) -> None:
        self._tools = tools

    def get(self, mount_point, name=None):
        if mount_point != "tools":
            return None
        return self._tools.get(name) if name else self._tools


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def test_gate_asks_user_for_a_mutating_action_when_gate_writes_is_on():
    coord = _FakeCoordinator({"computer": _FakeComputerTool(gate_writes=True)})
    handler = hook_mod._make_gate_handler(coord)

    result = _run(
        handler(
            "tool:pre",
            {"tool_name": "computer", "tool_input": {"action": "left_click"}},
        )
    )

    assert result.action == "ask_user"
    assert result.approval_default == "deny"


def test_gate_passes_through_reads_even_when_gate_writes_is_on():
    coord = _FakeCoordinator({"computer": _FakeComputerTool(gate_writes=True)})
    handler = hook_mod._make_gate_handler(coord)

    result = _run(
        handler(
            "tool:pre",
            {"tool_name": "computer", "tool_input": {"action": "screenshot"}},
        )
    )

    assert result.action == "continue"


def test_gate_off_never_asks():
    coord = _FakeCoordinator({"computer": _FakeComputerTool(gate_writes=False)})
    handler = hook_mod._make_gate_handler(coord)

    result = _run(
        handler(
            "tool:pre",
            {"tool_name": "computer", "tool_input": {"action": "left_click"}},
        )
    )

    assert result.action == "continue"


def test_gate_ignores_unrelated_tools():
    coord = _FakeCoordinator({"computer": _FakeComputerTool(gate_writes=True)})
    handler = hook_mod._make_gate_handler(coord)

    result = _run(
        handler(
            "tool:pre",
            {"tool_name": "read_file", "tool_input": {"action": "left_click"}},
        )
    )

    assert result.action == "continue"


def test_gate_handles_missing_tool_gracefully():
    coord = _FakeCoordinator({})
    handler = hook_mod._make_gate_handler(coord)

    result = _run(
        handler(
            "tool:pre",
            {"tool_name": "computer", "tool_input": {"action": "left_click"}},
        )
    )

    assert result.action == "continue"


def test_gate_applies_to_desktop_tool_mutating_actions_too():
    computer = _FakeComputerTool(gate_writes=True)

    class _FakeDesktopTool:
        _computer = computer

    coord = _FakeCoordinator({"computer": computer, "desktop": _FakeDesktopTool()})
    handler = hook_mod._make_gate_handler(coord)

    result = _run(
        handler(
            "tool:pre",
            {"tool_name": "desktop", "tool_input": {"action": "focus_window"}},
        )
    )

    assert result.action == "ask_user"
