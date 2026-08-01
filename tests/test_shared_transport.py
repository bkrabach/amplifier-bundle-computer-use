"""Unit tests for `shared_transport`: per-target singleton sharing, refcounted
teardown, and dead-transport eviction/replacement.

Runs entirely on Linux with no remote host and no real SSH: every fake
transport below is a plain in-process double that records calls and lets the
test dictate success/failure, exactly like `test_registry_remote_target.py`'s
`_FakeTransport`. The behavior under test - one shared `SshTransport` per
target key, refcounted teardown, and "a broken entry is retired, not silently
reconnected" - is entirely about `shared_transport.py`'s own bookkeeping, not
about SSH itself.
"""

from __future__ import annotations

import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "modules" / "tool-computer-use"))

import pytest
from amplifier_module_tool_computer_use import shared_transport
from amplifier_module_tool_computer_use.ssh_transport import SshConnectError


class _FakeTransport:
    """Records every call. `fail_send`/`fail_connect` let a test flip a
    switch mid-test to simulate the underlying SSH connection dying."""

    def __init__(self, name: str = "fake") -> None:
        self.name = name
        self.connect_calls = 0
        self.send_calls = 0
        self.close_calls = 0
        self.fail_connect = False
        self.fail_send = False

    def connect(self, *, required_permissions=(), connect_timeout=30.0):
        self.connect_calls += 1
        if self.fail_connect:
            raise SshConnectError(f"simulated connect failure for {self.name}")
        return {
            "protocol": 1,
            "agent_sha256": "irrelevant-for-this-fake",
            "python": "3.12.0",
            "platform": "linux",
            "backend": "linux-x11",
            "probe": {"available": True, "reason": ""},
            "capabilities": [],
            "permissions": {},
            "monitors": [],
        }

    def send(self, line: bytes, *, timeout: float = 30.0) -> bytes:
        self.send_calls += 1
        if self.fail_send:
            raise SshConnectError(f"simulated send failure for {self.name}")
        return b'{"id": 1, "ok": true, "result": {}}'

    def close(self) -> None:
        self.close_calls += 1


@pytest.fixture(autouse=True)
def _clear_registry():
    """Every test starts and ends with an empty shared-transport registry -
    this module holds process-wide state (`shared_transport._registry`), and
    tests must not leak entries into each other."""
    shared_transport._registry.clear()
    yield
    shared_transport._registry.clear()


def test_two_acquires_of_the_same_target_share_one_underlying_transport():
    """The exact bug this fix exists for: two independent consumers (e.g. a
    parent session's own tools mount and a delegated agent's child-session
    mount) resolving to the SAME target must get the SAME transport - only
    ONE real `connect()` (one SSH subprocess, one remote agent process)."""
    fake = _FakeTransport()
    key = ("ssh", "user@host-a")

    handle_a = shared_transport.acquire_shared_transport(key, lambda: fake)
    handle_b = shared_transport.acquire_shared_transport(key, lambda: fake)

    handle_a.connect()
    handle_b.connect()  # second consumer, same target

    assert fake.connect_calls == 1, "only the FIRST connect() should reach SSH"
    assert handle_a.handshake is handle_b.handshake

    handle_a.close()
    handle_b.close()


def test_different_targets_stay_completely_isolated():
    """Two different hosts must never share a transport, even if acquired in
    the same process at the same time."""
    fake_a = _FakeTransport("host-a")
    fake_b = _FakeTransport("host-b")

    handle_a = shared_transport.acquire_shared_transport(
        ("ssh", "user@host-a"), lambda: fake_a
    )
    handle_b = shared_transport.acquire_shared_transport(
        ("ssh", "user@host-b"), lambda: fake_b
    )

    handle_a.connect()
    handle_b.connect()

    assert fake_a.connect_calls == 1
    assert fake_b.connect_calls == 1
    assert shared_transport._registry_size() == 2

    handle_a.close()
    assert fake_a.close_calls == 1
    assert fake_b.close_calls == 0  # host-b entirely untouched

    handle_b.close()
    assert fake_b.close_calls == 1


def test_refcounted_teardown_last_release_wins():
    """An earlier `close()` must never kill a transport a sibling handle is
    still using - only the LAST release actually tears it down."""
    fake = _FakeTransport()
    key = ("ssh", "user@shared-host")

    h1 = shared_transport.acquire_shared_transport(key, lambda: fake)
    h2 = shared_transport.acquire_shared_transport(key, lambda: fake)
    h3 = shared_transport.acquire_shared_transport(key, lambda: fake)
    h1.connect()

    h1.close()
    assert fake.close_calls == 0, "two other handles still hold this transport"
    h2.close()
    assert fake.close_calls == 0, "one other handle still holds this transport"
    h3.close()
    assert fake.close_calls == 1, "last release must tear down the transport"

    # A subsequent acquire for the SAME key (after full release) must build a
    # brand-new transport, not resurrect the torn-down one.
    fake2 = _FakeTransport()
    h4 = shared_transport.acquire_shared_transport(key, lambda: fake2)
    h4.connect()
    assert fake2.connect_calls == 1
    assert fake.connect_calls == 1  # the old transport was never reused
    h4.close()


def test_close_is_idempotent():
    """`close()` may be called more than once (e.g. an explicit close plus a
    module cleanup callable both running) - only the first call decrements
    the shared refcount."""
    fake = _FakeTransport()
    key = ("ssh", "user@idempotent-host")

    h1 = shared_transport.acquire_shared_transport(key, lambda: fake)
    h2 = shared_transport.acquire_shared_transport(key, lambda: fake)

    h1.close()
    h1.close()  # double-close: must not decrement twice
    assert fake.close_calls == 0, "h2 still holds a reference"

    h2.close()
    assert fake.close_calls == 1


def test_dead_transport_is_not_handed_out_again():
    """A `send()` failure must mark the shared entry broken and evict it -
    the NEXT `acquire_shared_transport()` for the same key must build a
    fresh transport, never reuse the broken one."""
    fake = _FakeTransport()
    key = ("ssh", "user@flaky-host")

    handle = shared_transport.acquire_shared_transport(key, lambda: fake)
    handle.connect()
    fake.fail_send = True
    with pytest.raises(SshConnectError, match="simulated send failure"):
        handle.send(b'{"id": 1, "op": "probe", "args": {}}')

    assert shared_transport._registry_size() == 0, "broken entry must be evicted"

    fresh = _FakeTransport()
    new_handle = shared_transport.acquire_shared_transport(key, lambda: fresh)
    new_handle.connect()
    assert fresh.connect_calls == 1
    assert fake.connect_calls == 1  # the dead transport was never reconnected


def test_a_handle_still_holding_the_broken_entry_fails_loud_not_silently():
    """The other half of the dead-transport contract: a SECOND handle that
    was already holding a reference to the now-broken entry must see a clear,
    immediate failure on its own next call - never a silent, transparent
    reconnect that would spin up a fresh agent process (and a fresh, empty
    ledger) out from under it while it still believes inputs are held."""
    fake = _FakeTransport()
    key = ("ssh", "user@shared-flaky-host")

    handle_a = shared_transport.acquire_shared_transport(key, lambda: fake)
    handle_b = shared_transport.acquire_shared_transport(key, lambda: fake)
    handle_a.connect()

    fake.fail_send = True
    with pytest.raises(SshConnectError):
        handle_a.send(b'{"id": 1, "op": "probe", "args": {}}')

    # handle_b never itself called send(), but it shares the SAME entry -
    # its next call must fail loud and explicitly, not silently reconnect.
    with pytest.raises(SshConnectError, match="retired"):
        handle_b.send(b'{"id": 2, "op": "probe", "args": {}}')
    with pytest.raises(SshConnectError, match="retired"):
        handle_b.connect()

    # Only the ORIGINAL fake ever saw a connect - nothing silently replaced it.
    assert fake.connect_calls == 1


def test_connect_failure_marks_entry_broken_and_evicts():
    """A failure during the very first `connect()` (not just a later `send()`)
    must also retire the entry - a target that fails to connect at all must
    not be handed out to the next consumer as if it were healthy."""
    fake = _FakeTransport()
    fake.fail_connect = True
    key = ("ssh", "user@unreachable-host")

    handle = shared_transport.acquire_shared_transport(key, lambda: fake)
    with pytest.raises(SshConnectError, match="simulated connect failure"):
        handle.connect()

    assert shared_transport._registry_size() == 0


def test_concurrent_acquire_and_connect_from_multiple_threads_share_one_connect():
    """`ComputerTool.execute()` dispatches through `asyncio.to_thread`, so
    concurrent acquire+connect from real threads is not hypothetical. Many
    threads racing to acquire+connect the SAME target must still result in
    exactly one real `connect()` call."""
    fake = _FakeTransport()
    key = ("ssh", "user@race-host")
    barrier = threading.Barrier(8)
    handles: list[shared_transport.SharedTransportHandle] = []
    errors: list[BaseException] = []
    lock = threading.Lock()

    def _worker() -> None:
        try:
            barrier.wait(timeout=5)
            h = shared_transport.acquire_shared_transport(key, lambda: fake)
            h.connect()
            with lock:
                handles.append(h)
        except BaseException as exc:  # noqa: BLE001 - surfaced via `errors`
            with lock:
                errors.append(exc)

    threads = [threading.Thread(target=_worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert not errors, f"unexpected errors from worker threads: {errors}"
    assert fake.connect_calls == 1, "8 racing acquires must still connect once"
    assert len(handles) == 8

    for h in handles:
        h.close()
    assert fake.close_calls == 1, "refcount must have reached exactly 8 -> 0"
