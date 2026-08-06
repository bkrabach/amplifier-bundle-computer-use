"""Defect 2 fix: `mount()` must never leave a session silently without
computer-use - whatever the reason it could not obtain a working backend, a
`computer_use_unavailable` diagnostic tool is mounted instead, so the model
sees WHY in its own tool declarations (sent with every request) rather than
computer-use simply being absent with no trace.

Covers all three branches that previously (silently, or via an exception that
`amplifier_core._session_init` swallows at WARNING level one layer up) left
nothing mounted at all: `NoBackendAvailable` (D1, no local backend and no
`target` configured), a malformed `target` (`ValueError`), and an explicitly
configured but unreachable remote `target` (`RemoteTargetUnavailable`).
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "modules" / "tool-computer-use"))

import pytest
from amplifier_core.testing import MockCoordinator
from amplifier_module_tool_computer_use import ComputerUseUnavailableTool
from amplifier_module_tool_computer_use import mount as cu_mount
from amplifier_module_tool_computer_use.registry import NoBackendAvailable
from amplifier_module_tool_computer_use.remote_backend import RemoteTargetUnavailable


def _mounted_stub(coordinator: MockCoordinator) -> ComputerUseUnavailableTool:
    entries = [e for e in coordinator.mount_history if e["mount_point"] == "tools"]
    assert len(entries) == 1, f"expected exactly one tool mount, got {entries}"
    tool = entries[0]["module"]
    assert isinstance(tool, ComputerUseUnavailableTool)
    return tool


@pytest.mark.asyncio
async def test_no_backend_available_mounts_a_visible_stub_not_silence(
    monkeypatch, caplog
):
    import amplifier_module_tool_computer_use as cu

    def _raise(cfg):
        raise NoBackendAvailable([("linux-x11", "no DISPLAY")])

    monkeypatch.setattr(cu, "select_backend", _raise)
    coordinator = MockCoordinator()

    with caplog.at_level(logging.ERROR):
        manifest = await cu_mount(coordinator, {})

    assert manifest["provides"] == ["computer_use_unavailable"]
    tool = _mounted_stub(coordinator)
    assert tool.name == "computer_use_unavailable"
    assert "no DISPLAY" in tool.description
    assert any(
        rec.levelno >= logging.ERROR and "NOT MOUNTING" in rec.message
        for rec in caplog.records
    )


@pytest.mark.asyncio
async def test_malformed_target_mounts_a_visible_stub_not_silence(monkeypatch):
    import amplifier_module_tool_computer_use as cu

    def _raise(cfg):
        raise ValueError("config.target='user@host' is not a valid ssh:// target")

    monkeypatch.setattr(cu, "select_backend", _raise)
    coordinator = MockCoordinator()

    manifest = await cu_mount(coordinator, {"target": "user@host"})

    assert manifest["provides"] == ["computer_use_unavailable"]
    tool = _mounted_stub(coordinator)
    assert "invalid configuration" in tool.description
    assert "not a valid ssh://" in tool.description


@pytest.mark.asyncio
async def test_remote_target_unavailable_mounts_a_visible_stub_not_silence(
    monkeypatch,
):
    """The exact defect from the live repro: an explicitly configured
    `target:` that could not be reached used to raise `RemoteTargetUnavailable`
    straight out of `mount()` uncaught - which `amplifier_core._session_init`
    then swallows with a `logger.warning` and continues the session with
    nothing mounted and nothing in the model's context. `mount()` must now
    catch this itself and register the same visible stub."""
    import amplifier_module_tool_computer_use as cu

    def _raise(cfg):
        raise RemoteTargetUnavailable(
            "no handshake from user@down-host within 30.0s "
            "(agent may have crashed during bootstrap)"
        )

    monkeypatch.setattr(cu, "select_backend", _raise)
    coordinator = MockCoordinator()

    manifest = await cu_mount(coordinator, {"target": "ssh://user@down-host"})

    assert manifest["provides"] == ["computer_use_unavailable"]
    tool = _mounted_stub(coordinator)
    assert "no handshake" in tool.description


@pytest.mark.asyncio
async def test_unexpected_select_backend_exception_still_raises(monkeypatch):
    """Only the known "computer-use is not usable this session" exceptions
    degrade to the visible stub. Anything else is a real bug and must still
    fail loud, exactly as before this fix."""
    import amplifier_module_tool_computer_use as cu

    def _raise(cfg):
        raise RuntimeError("something actually broke")

    monkeypatch.setattr(cu, "select_backend", _raise)
    coordinator = MockCoordinator()

    with pytest.raises(RuntimeError, match="something actually broke"):
        await cu_mount(coordinator, {})

    assert coordinator.mount_history == []


@pytest.mark.asyncio
async def test_stub_execute_always_fails_honestly_and_never_hangs():
    tool = ComputerUseUnavailableTool("no backend available on this platform")
    result = await tool.execute({"action": "screenshot"})
    assert result.success is False
    assert result.error is not None
    assert result.error["type"] == "ComputerUseUnavailable"
    assert "no backend available" in result.error["message"]


def test_stub_name_is_distinct_from_the_real_tools():
    """Deliberately NOT named 'computer'/'desktop' - see the class docstring:
    reusing those names would blur "broken" with "never existed" and would
    also route this stub through hook-computer-use's `computer`/`desktop`
    pattern-matching (gate, halt-notice) with no reason to."""
    tool = ComputerUseUnavailableTool("reason")
    assert tool.name not in ("computer", "desktop")
