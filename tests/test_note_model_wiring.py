"""Unit tests proving `note_model()` is actually wired from hook-computer-use's
`provider.complete()` wrapper into the mounted `computer` tool (Plan A1,
`docs/designs/phase2-plans.md`).

`ComputerTool.note_model()` existed with zero callers before this fix - its own
docstring and a comment in tool-computer-use's `__init__.py` (~line 190) both
assert that hook-computer-use calls it on every `provider:request` with the
model actually about to be used. It never did: `_tool_version` was resolved
once at mount from `config["model"]` and never corrected, even though the
exact defect `tool_versions.py` exists to prevent (a model/tool_version
mismatch 400s *every* request) requires exactly this correction to fire.

These tests FAIL against the pre-fix `_wrap_provider` (no `note_model` call at
all - `test_wrapped_complete_forwards_request_model_to_note_model_...` is the
one that demonstrates the gap) and PASS once the wrapped `complete()` forwards
`request.model` to the mounted `computer` tool's `note_model()`.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "modules" / "tool-computer-use"))
sys.path.insert(0, str(ROOT / "modules" / "hook-computer-use"))

import amplifier_module_hook_computer_use as hook_mod
import pytest
from amplifier_module_tool_computer_use import ComputerTool


class _FakeBackend:
    """Minimal stand-in satisfying `ComputerTool.__init__`'s only two backend
    reads: `is_remote` (attribute, defaults False if absent) and `type_text`
    (its signature is inspected once at construction)."""

    is_remote = False

    def type_text(self, text: str) -> None:  # pragma: no cover - never invoked
        pass


class _AnthropicProviderNoStream:
    """Today's real shape: `complete()` plus a working
    `_derive_native_tool_betas()` (PR #79) - must wrap successfully."""

    __module__ = "amplifier_module_provider_anthropic"

    async def complete(self, request, **kwargs):
        return "ok"

    def _derive_native_tool_betas(self, tools):
        return ["computer-use-2025-11-24"] if tools else []


class _FakeRequest:
    def __init__(self, model: str | None) -> None:
        self.messages: list = []
        self.model = model


class _FakeCoordinator:
    """Same shape used across the existing hook-computer-use test suite
    (test_gate_hook.py, test_hook_stream_guard.py): `get("tools", name)`
    resolves a mounted tool by name; anything else (e.g. `"orchestrator"`)
    returns `None`, so `_fail_if_native_tool_passthrough_unsupported`'s
    orchestrator probe finds nothing to probe and skips it."""

    def __init__(self, tools: dict) -> None:
        self._tools = tools

    def get(self, mount_point, name=None):
        if mount_point != "tools":
            return None
        return self._tools.get(name) if name else self._tools


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def test_note_model_is_never_called_without_the_fix_baseline_sanity():
    """Sanity check on ComputerTool itself: mount-time config alone resolves
    `_tool_version` once and never corrects it on its own - only `note_model()`
    does. Establishes the baseline the rest of this file proves
    hook-computer-use now drives for real."""
    computer = ComputerTool(_FakeBackend(), {"model": "claude-opus-5"})
    assert computer._tool_version == "computer_20251124"


def test_wrapped_complete_forwards_request_model_to_note_model_and_corrects_tool_version(
    caplog: pytest.LogCaptureFixture,
):
    """The core reachable defect from Plan A1: mount-time config says
    claude-opus-5 (-> computer_20251124), but the model actually about to
    receive THIS request is claude-sonnet-4-5 (-> computer_20250124). Driving
    the wrapped `provider.complete()` must correct `_tool_version` - this is
    exactly the live-session scenario `note_model`'s own docstring promises,
    and before this fix could never happen because nothing ever called it.
    """
    computer = ComputerTool(_FakeBackend(), {"model": "claude-opus-5"})
    assert computer._tool_version == "computer_20251124"  # mount-time baseline

    coord = _FakeCoordinator({"computer": computer})
    provider = _AnthropicProviderNoStream()
    assert hook_mod._wrap_provider(provider, coord, max_inline=3) is True

    caplog.set_level(logging.WARNING, logger="amplifier_module_hook_computer_use")
    request = _FakeRequest(model="claude-sonnet-4-5-20250929")
    result = _run(provider.complete(request))

    assert result == "ok"
    assert computer._tool_version == "computer_20250124"
    assert any(
        "correcting" in rec.message and "computer_20250124" in rec.message
        for rec in caplog.records
    )


def test_wrapped_complete_never_raises_when_request_has_no_model_attribute():
    """A request shape with no `model` attribute at all must not break the
    request - `note_model(None)` is a documented, tested no-op/keep-previous
    case (see tests/test_tool_versions.py)."""
    computer = ComputerTool(_FakeBackend(), {"model": "claude-opus-5"})
    coord = _FakeCoordinator({"computer": computer})
    provider = _AnthropicProviderNoStream()
    hook_mod._wrap_provider(provider, coord, max_inline=3)

    class _RequestNoModel:
        messages: list = []

    result = _run(provider.complete(_RequestNoModel()))
    assert result == "ok"
    assert computer._tool_version == "computer_20251124"  # unchanged, no flapping


def test_wrapped_complete_tolerates_a_coordinator_that_cannot_find_the_tool():
    """No `computer` tool mounted (e.g. lookup races mount order, or this
    session never mounted computer-use at all) - must degrade to a no-op,
    never raise mid-request."""
    coord = _FakeCoordinator({})
    provider = _AnthropicProviderNoStream()
    hook_mod._wrap_provider(provider, coord, max_inline=3)

    result = _run(provider.complete(_FakeRequest(model="claude-sonnet-4-5-20250929")))
    assert result == "ok"
