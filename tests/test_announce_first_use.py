"""Unit tests for `ComputerTool._ensure_announced` itself: the gate that
replaced building the session-start disclosure inside `mount()` (see
`test_double_mount_defect.py` for the end-to-end reproduction of the defect
this closes, using the real amplifier_core loader/validator).

These tests exercise `_ensure_announced` directly against a `ComputerTool`
instance - no mount(), no coordinator - to pin down its own contract:
idempotent, thread-safe, and refusal is sticky for the life of the instance.
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "modules" / "tool-computer-use"))

import amplifier_module_tool_computer_use as cu
from amplifier_module_tool_computer_use.backend import BackendError, ScreenGeometry
from amplifier_module_tool_computer_use.coexistence_guard import CoexistenceGuard
from amplifier_module_tool_computer_use.presence import PresenceMonitor


def _guard(platform: str = "linux-x11", idle_ms: float = 999_999.0) -> CoexistenceGuard:
    presence = PresenceMonitor(idle_source=lambda: idle_ms, platform=platform)
    return CoexistenceGuard(presence=presence, release_all=lambda reason: [])


class _FakeBackend:
    is_remote = False
    name = "fake-local"

    def __init__(self) -> None:
        self.closed = False
        self.close_calls = 0

    def type_text(self, text: str) -> None:
        """`ComputerTool.__init__` inspects this signature."""

    def screen_geometry(self) -> ScreenGeometry:
        return ScreenGeometry(width=1920, height=1080)

    def list_monitors(self):
        raise BackendError("no monitor enumeration on this fake")

    def close(self) -> None:
        self.closed = True
        self.close_calls += 1


def _tool(backend: _FakeBackend | None = None) -> cu.ComputerTool:
    backend = backend or _FakeBackend()
    tool = cu.ComputerTool(backend, {})
    tool._coexistence_guard = _guard()
    return tool


# -- basic contract: not built at construction, built on first call ---------


def test_fresh_tool_has_not_announced_and_is_not_refused():
    tool = _tool()
    assert tool._announced is False
    assert tool._announce_refused is None
    assert tool._announcement is None


def test_first_call_builds_it_exactly_once(monkeypatch):
    calls = []
    monkeypatch.setattr(
        cu, "_build_announcement", lambda *_a, **_k: calls.append(1) or "handle"
    )
    tool = _tool()

    tool._ensure_announced()
    assert calls == [1]
    assert tool._announced is True
    assert tool._announcement == "handle"

    tool._ensure_announced()
    tool._ensure_announced()
    assert calls == [1], "a second/third call must reuse the decision, not re-ask"


# -- refusal is sticky --------------------------------------------------------


def test_refusal_is_sticky_across_repeated_calls_and_closes_backend_once(monkeypatch):
    calls = []

    def _refuse(*_a, **_k):
        calls.append(1)
        raise cu.AnnouncementRefused("declined")

    monkeypatch.setattr(cu, "_build_announcement", _refuse)
    backend = _FakeBackend()
    tool = _tool(backend)

    for _ in range(5):
        try:
            tool._ensure_announced()
            raised = False
        except cu.AnnouncementRefused:
            raised = True
        assert raised, "every call after a refusal must also refuse"

    assert calls == [1], (
        "a refusal must never re-attempt the dialog/overlay/RPC call on a "
        "later action - that would mean re-asking a human who already said no"
    )
    assert backend.close_calls == 1, (
        "the backend must be closed exactly once on refusal, not once per "
        "later refused call"
    )


def test_refusal_blocks_both_computer_and_desktop_execute(monkeypatch):
    """The structural guarantee the task calls for: once refused, NEITHER
    tool's execute() can reach the backend again - there is exactly one
    gate both call."""
    import asyncio

    monkeypatch.setattr(
        cu,
        "_build_announcement",
        lambda *_a, **_k: (_ for _ in ()).throw(cu.AnnouncementRefused("no")),
    )
    backend = _FakeBackend()
    computer = _tool(backend)
    desktop = cu.DesktopTool(computer)

    result1 = asyncio.get_event_loop().run_until_complete(
        computer.execute({"action": "screenshot"})
    )
    assert result1.success is False
    assert result1.error["type"] == "AnnouncementRefused"

    result2 = asyncio.get_event_loop().run_until_complete(
        desktop.execute({"action": "list_windows"})
    )
    assert result2.success is False
    assert result2.error["type"] == "AnnouncementRefused"


# -- concurrency: two actions racing to be "first" ---------------------------


def test_ensure_announced_is_thread_safe_only_one_dialog_for_a_race(monkeypatch):
    """Two actions issued by the model in the same turn run `_run()` (and
    now `_ensure_announced()`) via `asyncio.to_thread` - two different worker
    threads can race to be "first". A slow spy stands in for the real
    dialog/overlay/RPC call (which can genuinely block) and proves only ONE
    thread ever gets inside `_build_announcement`, with every other caller
    blocking on `self._announce_lock` until it is done, then reusing its
    result rather than re-asking."""
    call_count = {"n": 0}
    entered = threading.Event()
    release = threading.Event()

    def _slow_announce(*_a, **_k):
        call_count["n"] += 1
        entered.set()
        # Hold the "dialog" open - long enough that a second racing thread
        # would ALSO get in if the per-instance lock weren't doing its job.
        release.wait(timeout=2.0)
        return None

    monkeypatch.setattr(cu, "_build_announcement", _slow_announce)
    tool = _tool()
    errors: list[Exception] = []

    def _worker():
        try:
            tool._ensure_announced()
        except Exception as exc:  # pragma: no cover - would fail the test below
            errors.append(exc)

    t1 = threading.Thread(target=_worker)
    t2 = threading.Thread(target=_worker)
    t1.start()
    assert entered.wait(timeout=2.0), "t1 never entered _build_announcement"
    t2.start()
    time.sleep(0.05)  # give t2 a real chance to race in if the lock were broken
    release.set()
    t1.join(timeout=2.0)
    t2.join(timeout=2.0)

    assert errors == []
    assert call_count["n"] == 1, (
        "only one thread may ever call _build_announcement for a given "
        "ComputerTool instance, no matter how many actions race to be first"
    )
    assert tool._announced is True


def test_concurrent_refusal_is_seen_identically_by_every_racing_thread(monkeypatch):
    """The refusal counterpart of the race test above: if the winning thread
    gets refused, every other thread waiting on the lock must see the SAME
    refusal - never a second attempt, never a different outcome."""
    entered = threading.Event()
    release = threading.Event()
    call_count = {"n": 0}

    def _slow_refuse(*_a, **_k):
        call_count["n"] += 1
        entered.set()
        release.wait(timeout=2.0)
        raise cu.AnnouncementRefused("declined")

    monkeypatch.setattr(cu, "_build_announcement", _slow_refuse)
    backend = _FakeBackend()
    tool = _tool(backend)
    outcomes: list[str] = []
    lock = threading.Lock()

    def _worker():
        try:
            tool._ensure_announced()
            with lock:
                outcomes.append("ok")
        except cu.AnnouncementRefused:
            with lock:
                outcomes.append("refused")

    t1 = threading.Thread(target=_worker)
    t2 = threading.Thread(target=_worker)
    t1.start()
    assert entered.wait(timeout=2.0)
    t2.start()
    time.sleep(0.05)
    release.set()
    t1.join(timeout=2.0)
    t2.join(timeout=2.0)

    assert call_count["n"] == 1
    assert outcomes == ["refused", "refused"]
    assert backend.close_calls == 1
