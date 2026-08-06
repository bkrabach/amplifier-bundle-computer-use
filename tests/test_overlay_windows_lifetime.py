"""Regression tests for the overlay leak report
("Computer-Use Overlay Leak Report (for Brian) - part 2.md"): the Windows
coexistence overlay used to (1) self-relaunch as a fully detached process
with ONLY `atexit` for cleanup - a hook that never runs on `SIGKILL`, a
crash, or a closed terminal - and (2) never verify the detached child
actually came up, so a launch that silently failed left `show()` looking
like a success.

CI-side only: no Windows target, no PowerShell, no subprocess spawned -
`subprocess.Popen`/`subprocess.run` are monkeypatched throughout, matching
`test_overlay_windows.py`'s own established pattern. Real-hardware proof
(the actual SIGKILL case, and the events-file readiness race) lives in the
task's evidence log, not here - see that module's docstring for why this
project draws that line.

Every test below is written to FAIL against the pre-fix code (the
`subprocess.run(..., stdin=subprocess.DEVNULL)` / self-relaunch-via
`-Detached` / no readiness check implementation) and PASS against the
current one.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "modules" / "tool-computer-use"))

from amplifier_module_tool_computer_use import overlay_windows
from amplifier_module_tool_computer_use.overlay_windows import WindowsOverlay

#: `show()` generates a fresh token per launch via `uuid.uuid4().hex` and
#: only accepts a "ready" event carrying that exact token (see
#: `_wait_for_ready`'s docstring for why - neither PID matching nor file
#: mtime freshness survived real-hardware testing). Tests that need to
#: pre-write a matching "ready" event fix `uuid.uuid4` to return this.
FIXED_TOKEN = "test-token-0000"


def _pin_token(monkeypatch) -> None:
    monkeypatch.setattr(
        overlay_windows.uuid, "uuid4", lambda: types.SimpleNamespace(hex=FIXED_TOKEN)
    )


def _ready_event(pid: int = 424242) -> str:
    return json.dumps({"event": "ready", "pid": pid, "token": FIXED_TOKEN}) + "\n"


class _FakeStream:
    """Stand-in for a Popen pipe endpoint - just needs `.close()` and,
    for stderr, `.read()`."""

    def __init__(self, data: bytes = b"") -> None:
        self._data = data
        self.closed = False

    def close(self) -> None:
        self.closed = True

    def read(self) -> bytes:
        return self._data


class _FakePopen:
    """Stand-in for `subprocess.Popen` - records what it was called with,
    and lets each test script exit/kill/ready behavior explicitly."""

    last_instance: _FakePopen | None = None

    def __init__(self, argv, **kwargs) -> None:  # noqa: ANN001 - test double
        self.argv = argv
        self.kwargs = kwargs
        self.pid = 424242
        self.returncode: int | None = None
        self.killed = False
        self.waited = False
        self.stdin = _FakeStream()
        self.stdout = (
            kwargs.get("stdout")
            if kwargs.get("stdout")
            not in (
                subprocess.DEVNULL,
                subprocess.PIPE,
            )
            else None
        )
        self.stderr = (
            _FakeStream(b"") if kwargs.get("stderr") == subprocess.PIPE else None
        )
        _FakePopen.last_instance = self

    def poll(self):
        return self.returncode

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9

    def wait(self, timeout=None) -> int:  # noqa: ANN001
        self.waited = True
        return self.returncode or 0


def _make_overlay(**kwargs) -> WindowsOverlay:
    # A real, existing file so `_which_powershell` resolves it - the actual
    # binary is never invoked (`subprocess.Popen`/`subprocess.run` are
    # monkeypatched in every test below).
    return WindowsOverlay(screen_width=1920, powershell_path=sys.executable, **kwargs)


def _no_op_sweep(self) -> None:  # noqa: ANN001 - monkeypatch target
    pass


# -- lifetime binding: a live stdin pipe, not a detached self-relaunch -------


def test_show_launches_via_popen_not_blocking_run(monkeypatch, tmp_path):
    """The pre-fix code called `subprocess.run(...)` - a BLOCKING call that
    cannot represent a process meant to outlive `show()`. Launching via
    `subprocess.Popen` is what makes a live, held-open stdin pipe possible
    at all."""
    monkeypatch.setattr(WindowsOverlay, "_sweep_legacy_orphans", _no_op_sweep)
    monkeypatch.setattr(subprocess, "Popen", _FakePopen)
    _pin_token(monkeypatch)
    monkeypatch.setattr(
        subprocess, "run", lambda *a, **k: subprocess.CompletedProcess([], 0, "", "")
    )
    overlay = _make_overlay()
    events_file = tmp_path / "overlay-events.ndjson"
    monkeypatch.setattr(overlay, "_events_path_wsl", lambda: str(events_file))
    events_file.write_text(_ready_event(), encoding="utf-8")

    overlay.show()

    assert _FakePopen.last_instance is not None
    assert overlay.pid == 424242


def test_show_holds_a_piped_stdin_open_not_devnull(monkeypatch, tmp_path):
    """The exact defect this closes: `stdin=subprocess.DEVNULL` (the old
    code) cannot be watched for EOF by anything - a closed /dev/null read
    end tells the child nothing about whether its parent is alive.
    `stdin=subprocess.PIPE`, held open by THIS process, is what lets the
    overlay's own stdin watcher detect this process dying for ANY reason
    (see overlay_windows.ps1's module docstring)."""
    monkeypatch.setattr(WindowsOverlay, "_sweep_legacy_orphans", _no_op_sweep)
    monkeypatch.setattr(subprocess, "Popen", _FakePopen)
    _pin_token(monkeypatch)
    monkeypatch.setattr(
        subprocess, "run", lambda *a, **k: subprocess.CompletedProcess([], 0, "", "")
    )
    overlay = _make_overlay()
    events_file = tmp_path / "overlay-events.ndjson"
    monkeypatch.setattr(overlay, "_events_path_wsl", lambda: str(events_file))
    events_file.write_text(_ready_event(), encoding="utf-8")

    overlay.show()

    assert _FakePopen.last_instance is not None
    assert _FakePopen.last_instance.kwargs.get("stdin") == subprocess.PIPE, (
        "overlay stdin must be a live PIPE this process holds open - "
        "DEVNULL (or anything else) cannot be watched for EOF and leaves "
        "the overlay with no way to detect its parent dying"
    )


def test_hide_closes_the_stdin_pipe(monkeypatch, tmp_path):
    """Closing stdin here is what triggers the overlay's own EOF-driven
    self-teardown as a SECOND, independent signal alongside the explicit
    Stop-Process call - not just relying on one or the other."""
    monkeypatch.setattr(WindowsOverlay, "_sweep_legacy_orphans", _no_op_sweep)
    monkeypatch.setattr(subprocess, "Popen", _FakePopen)
    _pin_token(monkeypatch)
    monkeypatch.setattr(
        subprocess, "run", lambda *a, **k: subprocess.CompletedProcess([], 0, "", "")
    )
    overlay = _make_overlay()
    events_file = tmp_path / "overlay-events.ndjson"
    monkeypatch.setattr(overlay, "_events_path_wsl", lambda: str(events_file))
    events_file.write_text(_ready_event(), encoding="utf-8")
    overlay.show()
    proc = _FakePopen.last_instance
    assert proc is not None

    overlay.hide()

    assert proc.stdin.closed is True


# -- readiness verification: a launch that never comes up is a failure ------


def test_show_kills_a_process_that_never_reaches_ready(monkeypatch, tmp_path):
    """The pre-fix code treated `subprocess.run` returning rc=0 with a
    parsed PID as success - full stop. It never checked whether the
    detached child actually finished starting up, which is exactly the
    silent failure mode the leak report's live sampling caught (child
    processes spawning and dying before ever writing their own `ready`
    event). A launch that never reaches ready must be killed and reported,
    not left running unverified."""
    monkeypatch.setattr(WindowsOverlay, "_sweep_legacy_orphans", _no_op_sweep)
    monkeypatch.setattr(subprocess, "Popen", _FakePopen)
    monkeypatch.setattr(
        subprocess, "run", lambda *a, **k: subprocess.CompletedProcess([], 0, "", "")
    )
    overlay = _make_overlay(timeout=0.3)
    # No events file is ever created - this process never reaches ready.
    monkeypatch.setattr(
        overlay, "_events_path_wsl", lambda: str(tmp_path / "never-written.ndjson")
    )

    try:
        overlay.show()
        raised = False
    except Exception:
        raised = True

    assert raised, "show() must raise when the overlay never reaches ready"
    proc = _FakePopen.last_instance
    assert proc is not None
    assert proc.killed is True, (
        "a never-ready launch must be killed, not left running as an "
        "orphan the next call has no record of"
    )
    assert overlay.shown is False
    assert overlay.pid is None


def test_show_treats_early_process_exit_as_immediate_failure(monkeypatch, tmp_path):
    """A process that exits on its own before ever reaching ready (e.g. an
    Add-Type compile error) must fail fast, not wait out the full
    timeout."""
    monkeypatch.setattr(WindowsOverlay, "_sweep_legacy_orphans", _no_op_sweep)

    class _ExitsImmediately(_FakePopen):
        def __init__(self, argv, **kwargs):  # noqa: ANN001
            super().__init__(argv, **kwargs)
            self.returncode = 1

    monkeypatch.setattr(subprocess, "Popen", _ExitsImmediately)
    overlay = _make_overlay(timeout=10.0)
    monkeypatch.setattr(
        overlay, "_events_path_wsl", lambda: str(tmp_path / "never-written.ndjson")
    )

    start = time.monotonic()
    try:
        overlay.show()
    except Exception:
        pass
    elapsed = time.monotonic() - start

    assert elapsed < 2.0, (
        "an already-exited process must fail fast, not wait out the full "
        f"configured timeout (took {elapsed:.2f}s)"
    )


# -- startup sweep: self-heal against pre-existing orphans -------------------


def test_show_sweeps_for_legacy_detached_orphans_before_launching(
    monkeypatch, tmp_path
):
    """`show()` must look for (and remove) any pre-existing overlay
    process still carrying the OLD `-Detached` command-line shape before
    launching a new one - the self-healing recommendation from the leak
    report, scoped conservatively to a signature only the pre-fix code
    could ever have produced (see `_sweep_legacy_orphans`'s docstring for
    why this never touches a live sibling session's overlay)."""
    calls = []

    def _fake_run(argv, **kwargs):  # noqa: ANN001
        if argv and argv[0] == "wslpath":
            # The (unrelated) launch-args path translation, not the sweep.
            return subprocess.CompletedProcess(
                argv, 0, "C:\\fake\\overlay_windows.ps1", ""
            )
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, "KILLED=999\n", "")

    monkeypatch.setattr(subprocess, "run", _fake_run)
    monkeypatch.setattr(subprocess, "Popen", _FakePopen)
    _pin_token(monkeypatch)
    overlay = WindowsOverlay(screen_width=1920, powershell_path=sys.executable)
    events_file = tmp_path / "overlay-events.ndjson"
    monkeypatch.setattr(overlay, "_events_path_wsl", lambda: str(events_file))
    events_file.write_text(_ready_event(), encoding="utf-8")

    overlay.show()

    assert len(calls) == 1, "exactly one sweep query must run before launch"
    sweep_command = " ".join(calls[0])
    assert "overlay_windows.ps1" in sweep_command
    assert "-Detached" in sweep_command


def test_sweep_failure_never_blocks_show(monkeypatch, tmp_path):
    """The sweep is best-effort: if it fails for any reason, `show()` must
    still proceed to launch the real overlay rather than refusing to show
    anything."""

    def _raising_run(argv, **k):  # noqa: ANN001
        if argv and argv[0] == "wslpath":
            return subprocess.CompletedProcess(
                argv, 0, "C:\\fake\\overlay_windows.ps1", ""
            )
        raise OSError("boom")

    monkeypatch.setattr(subprocess, "run", _raising_run)
    monkeypatch.setattr(subprocess, "Popen", _FakePopen)
    _pin_token(monkeypatch)
    overlay = WindowsOverlay(screen_width=1920, powershell_path=sys.executable)
    events_file = tmp_path / "overlay-events.ndjson"
    monkeypatch.setattr(overlay, "_events_path_wsl", lambda: str(events_file))
    events_file.write_text(_ready_event(), encoding="utf-8")

    overlay.show()  # must not raise

    assert overlay.shown is True


# -- attribution: cleanup is a lookup, not a command-line string match -------


def test_show_writes_an_attribution_file_hide_removes_it(monkeypatch, tmp_path):
    monkeypatch.setattr(WindowsOverlay, "_sweep_legacy_orphans", _no_op_sweep)
    monkeypatch.setattr(subprocess, "Popen", _FakePopen)
    _pin_token(monkeypatch)
    monkeypatch.setattr(
        subprocess, "run", lambda *a, **k: subprocess.CompletedProcess([], 0, "", "")
    )
    overlay = _make_overlay()
    events_file = tmp_path / "overlay-events.ndjson"
    monkeypatch.setattr(overlay, "_events_path_wsl", lambda: str(events_file))
    events_file.write_text(_ready_event(), encoding="utf-8")

    overlay.show()

    attribution = tmp_path / "overlay-424242.json"
    assert attribution.exists(), (
        "cleanup must be possible via a direct lookup (pid -> attribution "
        "file), not by enumerating every powershell.exe command line"
    )
    payload = json.loads(attribution.read_text(encoding="utf-8"))
    assert payload["pid"] == 424242

    overlay.hide()

    assert not attribution.exists()
