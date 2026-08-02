"""Unit tests for `remote_agent.RemoteAgent` - the process that runs ON the
target. Exercised here against a `_FakeBackend` (no real desktop, no real
input) and plain `io.StringIO` pipes standing in for stdin/stdout, so this
proves dispatch, read_only enforcement, and - the most important property -
the held-input ledger's release-on-EOF guarantee, entirely offline.
"""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "modules" / "tool-computer-use"))

from amplifier_module_tool_computer_use.backend import (
    BackendError,
    MonitorInfo,
    ProbeResult,
    ScreenGeometry,
)
from amplifier_module_tool_computer_use.remote_agent import RemoteAgent


class _FakeBackend:
    name = "fake-remote-target"

    def __init__(self) -> None:
        self.moved_to: tuple[int, int] | None = None
        self.clicked: list[tuple] = []

    def probe(self) -> ProbeResult:
        return ProbeResult(True, "")

    def screen_geometry(self) -> ScreenGeometry:
        return ScreenGeometry(width=1920, height=1080)

    def list_monitors(self) -> list[MonitorInfo]:
        return [MonitorInfo(id="1", x=0, y=0, width=1920, height=1080, primary=True)]

    def capture(self, region=None) -> bytes:
        raise BackendError("not exercised in this test")

    def cursor_position(self) -> tuple[int, int]:
        return (5, 5)

    def move(self, x: int, y: int) -> None:
        self.moved_to = (x, y)

    def click(self, x, y, button="left", count=1) -> None:
        self.clicked.append((x, y, button, count))

    def type_text(self, text: str) -> None:
        pass

    def key(self, combo: str) -> None:
        pass


def _lines(*requests: dict) -> io.StringIO:
    return io.StringIO("".join(json.dumps(r) + "\n" for r in requests))


def test_handshake_is_the_first_unsolicited_line():
    agent = RemoteAgent(_FakeBackend(), read_only=False)
    stdin = _lines({"id": 1, "op": "bye", "args": {}})
    stdout = io.StringIO()

    agent.run(stdin, stdout)

    lines = stdout.getvalue().strip().splitlines()
    handshake = json.loads(lines[0])
    assert handshake["id"] == 0
    assert handshake["ok"] is True
    assert handshake["result"]["backend"] == "fake-remote-target"
    assert handshake["result"]["probe"]["available"] is True


def test_move_and_click_dispatch_to_the_backend():
    backend = _FakeBackend()
    agent = RemoteAgent(backend, read_only=False)
    stdin = _lines(
        {"id": 1, "op": "move", "args": {"x": 10, "y": 20}},
        {
            "id": 2,
            "op": "click",
            "args": {"x": 10, "y": 20, "button": "left", "count": 1},
        },
        {"id": 3, "op": "bye", "args": {}},
    )
    stdout = io.StringIO()

    agent.run(stdin, stdout)

    assert backend.moved_to == (10, 20)
    assert backend.clicked == [(10, 20, "left", 1)]
    lines = [json.loads(x) for x in stdout.getvalue().strip().splitlines()]
    assert lines[1] == {"id": 1, "ok": True, "result": None}
    assert lines[2] == {"id": 2, "ok": True, "result": None}


def test_read_only_blocks_write_ops_but_not_reads():
    backend = _FakeBackend()
    agent = RemoteAgent(backend, read_only=True)
    stdin = _lines(
        {"id": 1, "op": "move", "args": {"x": 1, "y": 1}},
        {"id": 2, "op": "screen_geometry", "args": {}},
        {"id": 3, "op": "bye", "args": {}},
    )
    stdout = io.StringIO()

    agent.run(stdin, stdout)

    assert backend.moved_to is None  # blocked
    lines = [json.loads(x) for x in stdout.getvalue().strip().splitlines()]
    assert lines[1]["ok"] is False
    assert "read_only" in lines[1]["error"]["message"]
    assert lines[2]["ok"] is True
    assert lines[2]["result"]["width"] == 1920


def test_unknown_op_returns_an_error_not_a_crash():
    agent = RemoteAgent(_FakeBackend(), read_only=False)
    stdin = _lines(
        {"id": 1, "op": "delete_everything", "args": {}},
        {"id": 2, "op": "bye", "args": {}},
    )
    stdout = io.StringIO()

    agent.run(stdin, stdout)  # must not raise

    lines = [json.loads(x) for x in stdout.getvalue().strip().splitlines()]
    assert lines[1]["ok"] is False


def test_ledger_releases_on_stdin_eof_even_with_no_bye():
    """THE core safety proof, run entirely offline: a held modifier with the
    stream simply ending (EOF, no `release_all`/`bye` sent - simulating an SSH
    client dying mid-session) must still be released, and released exactly
    once, before `run()` returns."""
    backend = _FakeBackend()
    agent = RemoteAgent(backend, read_only=False)
    stdin = _lines({"id": 1, "op": "hold", "args": {"key": "ctrl"}})  # then EOF, no bye
    stdout = io.StringIO()

    # No platform key-down primitive available for _FakeBackend's name -
    # RemoteAgent._modifier_down would raise for an unrecognised backend name.
    # Patch it to a fake hold/release pair so this test proves the LEDGER's
    # release guarantee without depending on Quartz/Xlib being installed.
    released: list[str] = []
    agent._modifier_down = lambda name: lambda: released.append(name)  # type: ignore[method-assign]

    agent.run(stdin, stdout)

    assert released == ["ctrl"], (
        "ledger must release on stdin EOF with no explicit release"
    )


def test_release_all_op_releases_everything_immediately():
    backend = _FakeBackend()
    agent = RemoteAgent(backend, read_only=False)
    released: list[str] = []
    agent._modifier_down = lambda name: lambda: released.append(name)  # type: ignore[method-assign]
    stdin = _lines(
        {"id": 1, "op": "hold", "args": {"key": "shift"}},
        {"id": 2, "op": "release_all", "args": {}},
        {"id": 3, "op": "bye", "args": {}},
    )
    stdout = io.StringIO()

    agent.run(stdin, stdout)

    assert released == ["shift"]
    lines = [json.loads(x) for x in stdout.getvalue().strip().splitlines()]
    assert lines[2]["result"]["released"] == ["shift"]


def test_every_op_handler_is_reachable_via_the_dispatch_table():
    """Every `_op_*` method must be wired into `_HANDLERS`.

    A handler that exists but is not in this table is DEAD CODE that fails at
    runtime with `UnsupportedOpError` while every unit test passes - because
    the tests call `_op_*` directly or mock `_call`, never exercising the
    lookup `_dispatch` actually uses.

    That is not hypothetical. `_op_presence_idle` shipped fully implemented,
    correctly classified READ in `wire.py`, and unreachable. A real SSH session
    against live hardware got:

        BackendError: UnsupportedOpError: op 'presence_idle' not implemented

    and the coexistence guard - which HAD been correctly constructed
    (platform=windows-wsl2, guard_ms=20.0) - raised IdleUnreadableError on its
    very first sample. So the failure mode was a hard crash on the first
    guarded write against any remote target, not a silent gap.

    This test is structural on purpose: it catches the whole class, not the one
    instance, and it cannot be satisfied by adding another mock.
    """
    from amplifier_module_tool_computer_use import remote_agent as ra

    agent_cls = ra.RemoteAgent
    handlers = agent_cls._HANDLERS
    op_methods = {
        name for name in dir(agent_cls) if name.startswith("_op_") and name != "_op_"
    }
    wired = {fn.__name__ for fn in handlers.values()}
    orphans = op_methods - wired
    assert not orphans, (
        f"handler(s) defined but unreachable via _HANDLERS: {sorted(orphans)} - "
        "they will fail at runtime with UnsupportedOpError while unit tests pass"
    )
