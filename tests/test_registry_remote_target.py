"""Unit tests for C3 in `registry.select_backend`: `target` absent -> today's
local-probe behavior, unchanged; `target` present -> `RemoteBackend` is the
ONLY candidate, and an unreachable target fails loud (never falls back to a
local backend). No real SSH - `_build_ssh_transport` is monkeypatched.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "modules" / "tool-computer-use"))

import pytest
from amplifier_module_tool_computer_use import registry
from amplifier_module_tool_computer_use.remote_backend import (
    RemoteBackend,
    RemoteTargetUnavailable,
)


class _FakeTransport:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.closed = False

    def connect(self, *, required_permissions=(), connect_timeout=30.0):
        if self.fail:
            from amplifier_module_tool_computer_use.ssh_transport import SshConnectError

            raise SshConnectError("simulated: host unreachable")
        return {
            "protocol": 1,
            "agent_sha256": "irrelevant-for-this-fake",
            "python": "3.12.0",
            "platform": "darwin",
            "backend": "macos",
            "probe": {"available": True, "reason": ""},
            "capabilities": ["capture_scaled"],
            "permissions": {"accessibility": True, "screen_recording": True},
            "monitors": [],
        }

    def close(self) -> None:
        self.closed = True


def test_target_absent_is_the_unchanged_local_path(monkeypatch):
    """C3: no `target` key at all -> ordinary local probing, exactly as before -
    this must not even look at `_build_ssh_transport`."""
    called = {"value": False}

    def _boom(*a, **k):
        called["value"] = True
        raise AssertionError("must not be called when target is absent")

    monkeypatch.setattr(registry, "_build_ssh_transport", _boom)

    with pytest.raises(registry.NoBackendAvailable):
        # No local backend will be available in this Linux CI sandbox
        # (no X11 display, no powershell.exe, not darwin) - the important
        # assertion is that _build_ssh_transport was never touched.
        registry.select_backend({}, factories=())

    assert called["value"] is False


def test_target_present_returns_a_connected_remote_backend(monkeypatch):
    monkeypatch.setattr(
        registry, "_build_ssh_transport", lambda host, pkg, cfg: _FakeTransport()
    )

    backend = registry.select_backend({"target": "ssh://user@example-host"})

    assert isinstance(backend, RemoteBackend)
    assert backend.is_remote is True
    assert "macos" in backend.name


def test_target_present_never_falls_back_to_local_on_failure(monkeypatch):
    """Acceptance item 7: an unreachable, EXPLICITLY configured target must
    fail loud, not silently mount a local backend instead - the worst possible
    outcome (driving the wrong desktop) this design exists to prevent."""
    monkeypatch.setattr(
        registry,
        "_build_ssh_transport",
        lambda host, pkg, cfg: _FakeTransport(fail=True),
    )

    with pytest.raises(RemoteTargetUnavailable):
        registry.select_backend({"target": "ssh://user@down-host"})


def test_malformed_target_raises_a_clear_parse_error(monkeypatch):
    monkeypatch.setattr(
        registry, "_build_ssh_transport", lambda host, pkg, cfg: _FakeTransport()
    )
    with pytest.raises(ValueError, match="not a valid ssh://"):
        registry.select_backend({"target": "not-a-valid-target"})


def test_target_without_explicit_user_is_accepted():
    assert registry._parse_target("ssh://myhost") == "myhost"
    assert registry._parse_target("ssh://user@myhost") == "user@myhost"
