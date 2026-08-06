"""Proves the bootstrap stub's scratch dir is actually removed on exit - the
leak this guards against (2026-08-03): `w=tempfile.mkdtemp(...)` in
`ssh_transport._bootstrap_stub` was created and never removed by ANY exit
path. Confirmed on a real target: 64 leaked dirs / 26MB accumulated over
three days, oldest untouched since creation.

`test_stub_removes_its_own_scratch_dir_on_normal_exit` runs the EXACT stub
text `_bootstrap_stub()` produces, as a real subprocess - no SSH needed, the
stub is plain `python3 -c <text>` and SSH is only the transport that carries
it to a target. It proves the directory the stub creates is gone once the
process exits via the ordinary path (handshake, then EOF/`bye`). Without the
fix (no `atexit.register(...)` in the stub) this fails: the directory
survives the process exit. With the fix, it does not.

Runs entirely headless (no DISPLAY, no Xvfb, no real backend required) - see
CONTRIBUTING.md's test philosophy. On a machine with no backend available the
agent still runs `main()`'s no-backend branch, which is enough: `mkdtemp()`
in the stub runs unconditionally, before backend selection is even attempted,
so this exercises the exact code path that leaked regardless of what backend
(if any) this test machine has.

The SIGTERM-specific path (`RemoteAgent.install_signal_handlers()` calling
`sys.exit(0)`, which must still reach the same `atexit` hook) requires a real
backend to be running the agent's blocking read loop when the signal
arrives - reaching for that here would make this test only pass on a machine
with a real desktop, which CONTRIBUTING.md reserves for the ship gate, not
`tests/`. That path is instead verified against a live remote target with a
real SIGTERM - see the PR/issue description for that evidence.
"""

from __future__ import annotations

import contextlib
import glob
import json
import os
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "modules" / "tool-computer-use"))
PACKAGE_DIR = (
    ROOT / "modules" / "tool-computer-use" / "amplifier_module_tool_computer_use"
)

from amplifier_module_tool_computer_use import ssh_transport as ssh_transport_mod


def _agent_scratch_dirs() -> set[str]:
    return set(glob.glob(os.path.join(tempfile.gettempdir(), "amplifier-cu-agent-*")))


def _read_line_with_timeout(stream, timeout: float) -> bytes | None:
    """Same bounded-read pattern `SshTransport._read_line_with_timeout` uses -
    a plain blocking `readline()` on a pipe has no timeout of its own."""
    result: dict[str, bytes | None] = {"line": None}

    def _read() -> None:
        result["line"] = stream.readline() or None

    t = threading.Thread(target=_read, daemon=True)
    t.start()
    t.join(timeout)
    return result["line"]


def test_stub_removes_its_own_scratch_dir_on_normal_exit():
    """THE fix: the exact stub text sent to every real target must clean up
    the scratch dir it creates once the process it spawns exits normally.

    On a machine with no backend available (this test's headless dev-box/CI
    environment), `remote_agent.main()`'s no-backend branch writes the
    handshake and returns within the same handful of Python bytecodes - there
    is no blocking `agent.run()` read loop holding the process open in
    between. That means the scratch dir's entire lifetime, in this branch,
    can be over before a single post-handshake `glob()` in the *parent*
    process gets scheduled: a snapshot taken right after reading the
    handshake line reliably loses that race (confirmed: the directory is
    gone every time by then, even after an extra 50ms grace sleep).

    `mkdtemp()` itself runs early in the stub - well before backend
    selection, argv setup, or the handshake is even built - so the directory
    does exist for a real, observable span of wall-clock time; the problem is
    purely *when* the parent looks. A background thread that polls
    continuously from the moment the payload is sent (not a single
    point-in-time check after the read) reliably observes it regardless of
    how short-lived the no-backend branch's process turns out to be.
    """
    payload = ssh_transport_mod._build_payload(PACKAGE_DIR)
    stub = ssh_transport_mod._bootstrap_stub(deadman_seconds=5.0, read_only=True)

    before = _agent_scratch_dirs()
    proc = subprocess.Popen(
        [sys.executable, "-c", stub],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    seen_dirs: set[str] = set()
    stop_polling = threading.Event()

    def _poll_for_scratch_dir() -> None:
        # Tight loop, no sleep: the no-backend branch's post-handshake
        # window can be sub-millisecond, so any polling interval risks
        # missing it entirely. Bounded by `stop_polling` from the main
        # thread (set once the handshake has been read, or in `finally`).
        while not stop_polling.is_set():
            found = _agent_scratch_dirs() - before
            if found:
                seen_dirs.update(found)
                return

    poller = threading.Thread(target=_poll_for_scratch_dir, daemon=True)
    poller.start()
    try:
        assert proc.stdin is not None and proc.stdout is not None
        proc.stdin.write(f"{len(payload)}\n".encode())
        proc.stdin.write(payload)
        proc.stdin.flush()

        # Handshake line - proves the stub actually ran far enough to create
        # and populate its scratch dir, not just that a process was spawned.
        line = _read_line_with_timeout(proc.stdout, timeout=20.0)
        assert line is not None, (
            "no handshake from the stub subprocess - stderr: "
            f"{proc.stderr.read().decode(errors='replace') if proc.stderr else ''}"
        )
        handshake = json.loads(line)
        assert handshake["ok"] is True

        # Ordinary shutdown: ask for a clean stop if the agent is still
        # reading requests (real backend available), then close stdin (EOF) -
        # exactly what `SshTransport.close()` does. If the no-backend branch
        # already exited (headless, no display), these are no-ops on a
        # closed pipe.
        with contextlib.suppress(BrokenPipeError, OSError):
            proc.stdin.write(
                json.dumps({"id": 1, "op": "bye", "args": {}}).encode() + b"\n"
            )
            proc.stdin.flush()
        with contextlib.suppress(OSError):
            proc.stdin.close()

        returncode = proc.wait(timeout=10)
        assert returncode is not None

        # Give the poller a last moment to notice a dir created very late
        # (has-backend branch, torn down only by the `bye`/EOF above), then
        # stop it - the process has now fully exited either way.
        poller.join(timeout=1.0)
        stop_polling.set()
        poller.join(timeout=1.0)

        assert len(seen_dirs) == 1, (
            f"expected exactly one new scratch dir to ever appear, saw {seen_dirs}"
        )
        scratch_dir = seen_dirs.pop()

        assert not os.path.exists(scratch_dir), (
            f"scratch dir {scratch_dir!r} was not cleaned up after normal exit "
            "(this is the leak - see ssh_transport._bootstrap_stub)"
        )
    finally:
        stop_polling.set()
        poller.join(timeout=2.0)
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)
        # Belt and suspenders: never let a bug in the fix leave test-run
        # residue behind even if an assertion above already failed.
        for leftover in _agent_scratch_dirs() - before:
            import shutil as _shutil

            _shutil.rmtree(leftover, ignore_errors=True)
