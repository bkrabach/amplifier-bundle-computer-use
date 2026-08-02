"""Unit tests: TASK 3.2/3.3 - `clipboard_read_policy` (an explicit, named,
always-audited gate on `get_clipboard`, distinct from `read_only`) and the
digest-not-plaintext audit discipline extended to `set_clipboard`.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "modules" / "tool-computer-use"))

import pytest
from amplifier_module_tool_computer_use import ComputerTool, DesktopTool
from amplifier_module_tool_computer_use.backend import BackendError, ScreenGeometry


class _FakeClipboardBackend:
    name = "linux-x11"

    def __init__(self, clipboard_text: str = "hello world") -> None:
        self._clipboard = clipboard_text
        self.set_clipboard_calls: list[str] = []

    def screen_geometry(self) -> ScreenGeometry:
        return ScreenGeometry(4, 4, 0, 0)

    def list_monitors(self):
        raise BackendError("no monitor enumeration on this fake")

    def cursor_position(self) -> tuple[int, int]:
        return (0, 0)

    def type_text(self, text, guard=None) -> None:  # pragma: no cover - unused
        pass

    def get_clipboard(self) -> str:
        return self._clipboard

    def set_clipboard(self, text: str) -> None:
        self.set_clipboard_calls.append(text)
        self._clipboard = text

    def close(self) -> None:  # pragma: no cover
        pass


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _make_desktop(cfg: dict | None = None, clipboard_text: str = "hello world"):
    backend = _FakeClipboardBackend(clipboard_text)
    computer = ComputerTool(backend, cfg or {})
    computer.resolve_display()
    desktop = DesktopTool(computer)
    return desktop, computer, backend


# -- default policy: "allow" locally, matching pre-existing behavior --------


def test_default_policy_is_allow_for_local_and_full_content_is_returned():
    desktop, computer, _backend = _make_desktop()
    assert computer._clipboard_read_policy == "allow"

    result = _run(desktop.execute({"action": "get_clipboard"}))

    assert result.success is True
    assert result.output == "hello world"


def test_default_policy_is_redact_for_a_remote_target():
    class _RemoteBackend(_FakeClipboardBackend):
        is_remote = True

    backend = _RemoteBackend("a secret token")
    computer = ComputerTool(backend, {"read_only": False})
    computer.resolve_display()
    assert computer._is_remote is True
    assert computer._clipboard_read_policy == "redact"


# -- explicit policy: redact --------------------------------------------------


def test_redact_policy_never_returns_plaintext():
    desktop, _computer, _backend = _make_desktop(
        {"clipboard_read_policy": "redact"}, clipboard_text="super-secret-password"
    )

    result = _run(desktop.execute({"action": "get_clipboard"}))

    assert result.success is True
    assert "super-secret-password" not in result.output
    assert "redacted" in result.output
    assert "21 chars" in result.output  # len("super-secret-password") == 21


# -- explicit policy: block ----------------------------------------------------


def test_block_policy_refuses_without_touching_the_backend():
    desktop, _computer, _backend = _make_desktop(
        {"clipboard_read_policy": "block"}, clipboard_text="never read this"
    )

    result = _run(desktop.execute({"action": "get_clipboard"}))

    assert result.success is False
    assert "block" in result.error["message"]


# -- invalid policy value: fail loud at construction, not silently ------------


def test_invalid_clipboard_policy_raises_at_construction():
    backend = _FakeClipboardBackend()
    with pytest.raises(ValueError, match="clipboard_read_policy"):
        ComputerTool(backend, {"clipboard_read_policy": "sure-why-not"})


# -- audit logging: every read is logged, even under the permissive default --


def test_get_clipboard_is_always_audit_logged_with_a_digest_not_plaintext(caplog):
    desktop, _computer, _backend = _make_desktop(
        clipboard_text="a just-copied password"
    )

    with caplog.at_level(logging.INFO):
        _run(desktop.execute({"action": "get_clipboard"}))

    audit_lines = [r.message for r in caplog.records if "audit" in r.message]
    assert len(audit_lines) == 1
    assert "get_clipboard" in audit_lines[0]
    assert "a just-copied password" not in audit_lines[0]
    assert "sha256=" in audit_lines[0]


def test_set_clipboard_logs_a_digest_not_the_plaintext(caplog):
    desktop, _computer, backend = _make_desktop()

    with caplog.at_level(logging.INFO):
        result = _run(
            desktop.execute({"action": "set_clipboard", "text": "another-secret-value"})
        )

    assert result.success is True
    assert backend.set_clipboard_calls == ["another-secret-value"]
    audit_lines = [r.message for r in caplog.records if "audit" in r.message]
    assert len(audit_lines) == 1
    assert "set_clipboard" in audit_lines[0]
    assert "another-secret-value" not in audit_lines[0]
    assert "sha256=" in audit_lines[0]


# -- set_clipboard now goes through the same guard discipline as _run -------


def test_set_clipboard_is_guarded_like_other_mutating_actions():
    desktop, computer, backend = _make_desktop()

    calls: list[str] = []

    class _SpyGuard:
        def check_start_permission(self):
            calls.append("check_start_permission")

        def bind_target(self):
            calls.append("bind_target")

        def before_event(self, *, coord=None):
            calls.append("before_event")

        def after_event(self):
            calls.append("after_event")

        def release_target(self):
            calls.append("release_target")

    computer._coexistence_guard = _SpyGuard()  # type: ignore[assignment]

    _run(desktop.execute({"action": "set_clipboard", "text": "x"}))

    assert calls == [
        "check_start_permission",
        "bind_target",
        "before_event",
        "after_event",
        "release_target",
    ]
    assert backend.set_clipboard_calls == ["x"]


def test_read_only_still_blocks_get_clipboard_entirely_before_policy_is_even_consulted():
    """Pre-existing behavior (`_READ_ONLY_BLOCKED`) must be unaffected by
    this addition - `read_only` is the coarser, prior gate."""
    backend = _FakeClipboardBackend("secret")
    computer = ComputerTool(backend, {"read_only": True})
    computer.resolve_display()
    desktop = DesktopTool(computer)

    result = _run(desktop.execute({"action": "get_clipboard"}))

    assert result.success is False
    assert "read_only" in result.error["message"]
