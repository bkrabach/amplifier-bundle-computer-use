"""Proves that a bootstrap/handshake failure's exception CARRIES the agent's
captured stderr rather than merely telling the operator to go look for it.

Real incident this guards against (2026-07-28): a macOS target's handshake
timed out because `uv` could not fetch `pyobjc-framework-Quartz` from PyPI
(a package-feed policy change). `ssh_transport.py` had already read and
logged the agent's stderr - the exact root cause - and then raised
`SshConnectError("... - see stderr)")` with nothing attached. The operator
had to bolt on `logging.basicConfig(level=DEBUG)` to find a message the
process had already read. See BACKLOG/PR description for the full narrative.
"""

from __future__ import annotations

import io
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "modules" / "tool-computer-use"))
PACKAGE_DIR = (
    ROOT / "modules" / "tool-computer-use" / "amplifier_module_tool_computer_use"
)

import pytest
from amplifier_module_tool_computer_use import ssh_transport as ssh_transport_mod
from amplifier_module_tool_computer_use.remote_backend import (
    RemoteBackend,
    RemoteTargetUnavailable,
)
from amplifier_module_tool_computer_use.ssh_transport import (
    SshConnectError,
    SshTransport,
)

# The real stderr from the incident this fix targets.
_UV_FETCH_STDERR = (
    b"error: Failed to fetch:\n"
    b"  https://files.pythonhosted.org/packages/aa/bb/pyobjc_framework"
    b"_Quartz-10.3-cp312-cp312-macosx.whl\n"
    b"  Caused by: Request failed after 3 retries\n"
    b"  Caused by: client error (Connect)\n"
    b"  Caused by: Socket is not connected (os error 57)\n"
)


class _FakeStderr:
    """One-shot `.read(n)` returning the whole payload, then empty - matches
    `_drain_stderr_on_failure`'s single `read(4096)` call."""

    def __init__(self, data: bytes) -> None:
        self._data = data
        self._served = False

    def read(self, _n: int = 4096) -> bytes:
        if self._served:
            return b""
        self._served = True
        return self._data


class _FakeStdout:
    """`readline()` always returns nothing - simulates an agent that never
    produces a handshake line (bootstrap crashed before printing one)."""

    def readline(self) -> bytes:
        return b""


class _FakeConnectProc:
    """Stands in for the `subprocess.Popen` handle `SshTransport.connect()`
    holds: writable stdin (payload deploy), a stdout that never yields a
    handshake line, and a stderr pre-loaded with the agent's crash output."""

    def __init__(self, stderr_data: bytes) -> None:
        self.stdin = io.BytesIO()
        self.stdout = _FakeStdout()
        self.stderr = _FakeStderr(stderr_data)


def _install_fake_popen(monkeypatch, stderr_data: bytes) -> None:
    def fake_popen(cmd, **kwargs):
        return _FakeConnectProc(stderr_data)

    monkeypatch.setattr(ssh_transport_mod.subprocess, "Popen", fake_popen)
    # Skip the real `uv` discovery probe (its own subprocess.run call) - it
    # is not what this test is proving.
    monkeypatch.setattr(
        ssh_transport_mod, "_resolve_uv_command", lambda user_host, ssh_path="ssh": "uv"
    )


def test_handshake_timeout_carries_the_agents_stderr_not_just_a_reference(
    monkeypatch,
):
    """THE fix: the exception must contain the actual root cause, not send
    the operator to go find it themselves."""
    _install_fake_popen(monkeypatch, _UV_FETCH_STDERR)
    transport = SshTransport("user@macos-host", package_dir=PACKAGE_DIR)

    with pytest.raises(SshConnectError) as excinfo:
        transport.connect(connect_timeout=0.2)

    exc = excinfo.value
    # The attribute exists and holds the real content - not None, not empty.
    assert exc.agent_stderr, "SshConnectError must carry the captured stderr"
    assert "Failed to fetch" in exc.agent_stderr
    assert "os error 57" in exc.agent_stderr
    # And it must actually be IN the exception's string form - what a bare
    # `logger.error("%s", exc)` or an uncaught-traceback print will show -
    # not merely referenced by name.
    rendered = str(exc)
    assert "Failed to fetch" in rendered
    assert "os error 57" in rendered


def test_handshake_timeout_message_no_longer_dangles_a_bare_stderr_reference(
    monkeypatch,
):
    """The old message text told the operator to go read stderr elsewhere.
    Once the content is actually attached, that dangling reference must be
    gone - there is nothing left to go looking for."""
    _install_fake_popen(monkeypatch, _UV_FETCH_STDERR)
    transport = SshTransport("user@macos-host", package_dir=PACKAGE_DIR)

    with pytest.raises(SshConnectError) as excinfo:
        transport.connect(connect_timeout=0.2)

    assert "see stderr" not in str(excinfo.value)


def test_handshake_timeout_with_no_captured_stderr_is_honest_about_it(
    monkeypatch,
):
    """No fallback, no fabricated content: when nothing could be captured,
    `agent_stderr` is None and the message says only what actually happened."""
    _install_fake_popen(monkeypatch, b"")
    transport = SshTransport("user@macos-host", package_dir=PACKAGE_DIR)

    with pytest.raises(SshConnectError) as excinfo:
        transport.connect(connect_timeout=0.2)

    exc = excinfo.value
    assert exc.agent_stderr is None
    assert "agent stderr" not in str(exc)


def test_remote_target_unavailable_propagates_the_same_stderr(monkeypatch):
    """`RemoteBackend.connect()` wraps `SshConnectError` in
    `RemoteTargetUnavailable` (remote_backend.py) - the wrap must not drop
    the payload the inner exception now carries."""

    class _FailingTransport:
        def connect(self, **kwargs):
            raise SshConnectError(
                "no handshake from macos-host within 30.0s "
                "(agent may have crashed during bootstrap)",
                agent_stderr="error: Failed to fetch: ...\nos error 57",
            )

    backend = RemoteBackend(
        {"_host": "user@macos-host", "_transport": _FailingTransport()}
    )

    with pytest.raises(RemoteTargetUnavailable) as excinfo:
        backend.connect()

    exc = excinfo.value
    assert exc.agent_stderr == "error: Failed to fetch: ...\nos error 57"
    assert "os error 57" in str(exc)


def test_summarize_stderr_redacts_embedded_url_credentials():
    raw = (
        "error: Failed to fetch:\n"
        "  https://deploy:s3cr3t-token@packages.example.com/private/feed.whl\n"
        "  Caused by: 401 Unauthorized\n"
    )
    summary = ssh_transport_mod._summarize_stderr(raw)
    assert "s3cr3t-token" not in summary
    assert "deploy" not in summary
    assert "packages.example.com" in summary  # host stays - it's not secret


def test_summarize_stderr_truncates_to_head_and_tail():
    lines = [f"line-{i}" for i in range(200)]
    raw = "\n".join(lines)
    summary = ssh_transport_mod._summarize_stderr(raw)
    assert "line-0" in summary
    assert "line-199" in summary
    assert "line-100" not in summary  # middle noise is dropped
    assert "omitted" in summary
    assert len(summary.splitlines()) < 30
