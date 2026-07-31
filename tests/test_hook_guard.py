"""Unit test for the D3 fix: a tool whose `native_tool_spec` raises must never
break a provider request.

Before the fix, `_promote_tools` gated on `hasattr(tool, "native_tool_spec")`.
Python 3's `hasattr` swallows *only* `AttributeError` - any other exception raised by
the property escapes `hasattr` itself, before the surrounding `try/except` even
starts, and an uncaught traceback took down every single provider request in every
session (this is exactly what D2's blocking-property bridge call produced in
practice: a `BridgeError` from a subprocess timeout, on every request).

This test proves the fix directly, with no mocks of code we don't own: the real
`hook_mod._promote_tools`, a real `ToolSpec`, and a fake coordinator/tool.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "modules" / "tool-computer-use"))
sys.path.insert(0, str(ROOT / "modules" / "hook-computer-use"))

import amplifier_module_hook_computer_use as hook_mod
from amplifier_core.message_models import ToolSpec


class _RaisingBackendError(RuntimeError):
    """Stands in for `backend.BackendError` without importing the tool module."""


class _ToolWithRaisingNativeSpec:
    """A tool whose `native_tool_spec` property raises something other than
    `AttributeError` - simulating D2's blocking bridge call failing."""

    name = "computer"

    @property
    def native_tool_spec(self):
        raise _RaisingBackendError("backend unreachable")


class _OrdinaryFunctionTool:
    """A tool with no `native_tool_spec` at all - the common case, must be
    completely unaffected by the guard's shape."""

    name = "read_file"


class _WorkingNativeTool:
    """A tool whose `native_tool_spec` works normally - the existing happy path,
    must still be promoted."""

    name = "computer"

    @property
    def native_tool_spec(self):
        return {
            "type": "computer_20251124",
            "name": "computer",
            "display_width_px": 1280,
            "display_height_px": 720,
        }


class _FakeCoordinator:
    def __init__(self, tools: dict) -> None:
        self._tools = tools

    def get(self, mount_point, name=None):
        if mount_point != "tools":
            return None
        return self._tools.get(name) if name else self._tools


def test_promote_tools_survives_a_native_tool_spec_that_raises_non_attribute_error():
    coord = _FakeCoordinator({"computer": _ToolWithRaisingNativeSpec()})
    specs = [ToolSpec(name="computer", description="d", parameters={})]

    # Must not raise - this is the actual bug: hasattr() let the exception through
    # *before* the try/except that was supposed to catch it.
    promoted, betas = hook_mod._promote_tools(coord, specs)

    assert len(promoted) == 1
    assert betas == []
    # Falls back to the ordinary function-tool spec, unmodified.
    assert promoted[0].name == "computer"
    assert not hasattr(promoted[0], "native_payload") or not promoted[0].native_payload


def test_promote_tools_leaves_ordinary_tools_untouched():
    coord = _FakeCoordinator({"read_file": _OrdinaryFunctionTool()})
    specs = [ToolSpec(name="read_file", description="d", parameters={})]

    promoted, betas = hook_mod._promote_tools(coord, specs)

    assert len(promoted) == 1
    assert betas == []
    assert promoted[0].name == "read_file"


def test_promote_tools_still_promotes_a_working_native_tool():
    coord = _FakeCoordinator({"computer": _WorkingNativeTool()})
    specs = [ToolSpec(name="computer", description="d", parameters={})]

    promoted, _betas = hook_mod._promote_tools(coord, specs)

    assert len(promoted) == 1
    dumped = promoted[0].model_dump(exclude_none=True)
    assert dumped.get("type") == "computer_20251124"


def test_promote_tools_survives_a_missing_tool_lookup():
    """`coordinator.get` returning None (tool not mounted, or lookup failing) must
    not be confused with a tool that raises - both must degrade to "not native"."""
    coord = _FakeCoordinator({})
    specs = [ToolSpec(name="computer", description="d", parameters={})]

    promoted, betas = hook_mod._promote_tools(coord, specs)

    assert len(promoted) == 1
    assert betas == []
