"""Unit tests for `tools.remote_probe` - no real remote host, no network.

Every `subprocess.Popen` call the module makes is replaced with a fake
process object so these tests prove the DEPLOY/RUN/COLLECT/TEARDOWN
sequencing, the `-n`-only-on-ssh-never-on-scp discipline, and the
poll-with-a-deadline timeout mechanism (never `.communicate()`, never the
shell `timeout` binary) entirely offline.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools import remote_probe


class _FakeStream:
    """Stands in for a Popen pipe: `.read(n)` returns the whole payload once,
    then `b""` (EOF) - exactly what the module's drain-thread loop expects."""

    def __init__(self, data: bytes) -> None:
        self._data = data
        self._served = False

    def read(self, _n: int = 4096) -> bytes:
        if self._served:
            return b""
        self._served = True
        return self._data


class _FakeProcess:
    """`poll()` returns `None` until `terminate()`/`kill()` is called (for
    simulating a hung remote command that a bounded timeout must reap), or
    immediately returns `returncode` if `never_hangs` is True."""

    def __init__(
        self,
        *,
        returncode: int = 0,
        stdout: bytes = b"",
        stderr: bytes = b"",
        never_hangs: bool = True,
    ):
        self.returncode = returncode
        self.stdout = _FakeStream(stdout)
        self.stderr = _FakeStream(stderr)
        self._never_hangs = never_hangs
        self.terminated = False
        self.killed = False

    def poll(self):
        if self._never_hangs:
            return self.returncode
        if self.terminated or self.killed:
            return self.returncode
        return None

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.killed = True


def _install_fake_popen(monkeypatch, dispatch):
    """`dispatch(cmd: list[str]) -> _FakeProcess` decides what each
    Popen(...) call returns, based on the real argv it was given."""
    calls: list[list[str]] = []

    def fake_popen(cmd, **kwargs):
        calls.append(cmd)
        return dispatch(cmd)

    monkeypatch.setattr(remote_probe.subprocess, "Popen", fake_popen)
    return calls


def test_ssh_invocations_use_n_and_scp_invocations_never_do(monkeypatch, tmp_path):
    """The exact discipline the task calls out: `-n` on ssh, never on scp."""
    script = tmp_path / "probe.sh"
    script.write_text("#!/bin/sh\necho hi\n", encoding="utf-8")
    script.chmod(0o755)

    def dispatch(cmd):
        if cmd[0] == "scp":
            assert "-n" not in cmd, f"scp must never receive -n: {cmd}"
            return _FakeProcess(returncode=0)
        assert cmd[0] == "ssh"
        assert cmd[1] == "-n", f"every ssh invocation must lead with -n: {cmd}"
        if "echo hi" in cmd[-1] or cmd[-1].strip().startswith("chmod"):
            return _FakeProcess(returncode=0, stdout=b"hi\n")
        return _FakeProcess(returncode=0)

    calls = _install_fake_popen(monkeypatch, dispatch)

    result = remote_probe.deploy_run_collect("example-host", script, timeout_s=5)

    assert result.exit_code == 0
    assert result.stdout == "hi\n"
    assert not result.timed_out
    # mkdir, scp deploy, run, teardown rm - at least these four ssh/scp calls.
    assert len(calls) >= 4
    scp_calls = [c for c in calls if c[0] == "scp"]
    assert len(scp_calls) == 1  # deploy only - no pull_paths requested


def test_full_sequence_deploy_run_pull_teardown(monkeypatch, tmp_path):
    script = tmp_path / "probe.sh"
    script.write_text("#!/bin/sh\necho payload\n", encoding="utf-8")
    pull_dest = tmp_path / "pulled"

    def dispatch(cmd):
        if cmd[0] == "scp":
            # Deploy scp (2 args: local, host:remote) vs pull scp (host:remote, local)
            src, dst = cmd[-2], cmd[-1]
            if src.startswith("example-host:"):
                # Pulling a result file back - write it so the caller finds it.
                Path(dst).write_text("remote-result-contents", encoding="utf-8")
            return _FakeProcess(returncode=0)
        return _FakeProcess(returncode=0, stdout=b"payload\n")

    _install_fake_popen(monkeypatch, dispatch)

    result = remote_probe.deploy_run_collect(
        "example-host",
        script,
        pull_paths=["/tmp/remote/result.txt"],
        pull_dest=pull_dest,
        timeout_s=5,
    )

    assert result.ok
    assert "result.txt" in result.pulled_paths["/tmp/remote/result.txt"].name
    assert result.pulled_paths["/tmp/remote/result.txt"].read_text(
        encoding="utf-8"
    ) == ("remote-result-contents")


def test_deploy_failure_raises_before_running_anything(monkeypatch, tmp_path):
    script = tmp_path / "probe.sh"
    script.write_text("#!/bin/sh\necho hi\n", encoding="utf-8")

    def dispatch(cmd):
        if cmd[0] == "scp":
            return _FakeProcess(returncode=1, stderr=b"scp: permission denied")
        return _FakeProcess(returncode=0)

    _install_fake_popen(monkeypatch, dispatch)

    try:
        remote_probe.deploy_run_collect("example-host", script, timeout_s=5)
    except remote_probe.RemoteProbeError as exc:
        assert "scp deploy" in str(exc)
    else:
        raise AssertionError("expected RemoteProbeError on scp failure")


def test_run_ssh_never_blocks_past_its_deadline_on_a_hung_process(monkeypatch):
    """The core anti-6h55m-hang property: a process that never exits on its
    own is terminated (or killed) once the deadline passes, and `_run_ssh`
    returns promptly - it never calls a blocking `.communicate()`/`.wait()`
    with no bound."""
    hung = _FakeProcess(returncode=17, never_hangs=False)
    monkeypatch.setattr(remote_probe.subprocess, "Popen", lambda *a, **k: hung)

    start = time.monotonic()
    out, err, rc, timed_out = remote_probe._run_ssh(
        ["ssh", "-n", "example-host", "sleep 999"], timeout_s=0.3, poll_interval_s=0.05
    )
    elapsed = time.monotonic() - start

    assert timed_out is True
    assert hung.terminated is True
    assert rc == 17  # the fake reports its returncode once "terminated"
    # Bounded: should return within a couple seconds of the 0.3s deadline,
    # never hang indefinitely.
    assert elapsed < 5.0


def test_resolve_powershell_over_ssh_finds_the_real_interop_path(monkeypatch):
    """Mirrors the WSL lesson: PATH does not carry /mnt/c in a non-login
    shell, so this probes explicit candidates rather than trusting PATH."""

    def dispatch(cmd):
        assert cmd[0] == "ssh" and cmd[1] == "-n"
        return _FakeProcess(
            returncode=0,
            stdout=b"/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe\n",
        )

    _install_fake_popen(monkeypatch, dispatch)

    resolved = remote_probe.resolve_powershell_over_ssh("windows-host")
    assert resolved == "/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe"


def test_resolve_powershell_over_ssh_fails_loud_when_not_found(monkeypatch):
    def dispatch(cmd):
        return _FakeProcess(returncode=1, stderr=b"command not found")

    _install_fake_popen(monkeypatch, dispatch)

    try:
        remote_probe.resolve_powershell_over_ssh("linux-host")
    except remote_probe.RemoteProbeError as exc:
        assert "could not find powershell.exe" in str(exc)
    else:
        raise AssertionError("expected RemoteProbeError")


def test_missing_local_script_raises_before_touching_the_network(monkeypatch):
    def dispatch(cmd):
        raise AssertionError(
            "must not attempt any ssh/scp call for a missing local script"
        )

    _install_fake_popen(monkeypatch, dispatch)

    try:
        remote_probe.deploy_run_collect(
            "example-host", "/no/such/script.sh", timeout_s=5
        )
    except remote_probe.RemoteProbeError as exc:
        assert "not found" in str(exc)
    else:
        raise AssertionError("expected RemoteProbeError")
