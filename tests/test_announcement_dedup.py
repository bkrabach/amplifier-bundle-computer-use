"""Regression tests for the double-announce defect: a parent session's own
`mount()` and a delegated child session's `mount()` (`tool-delegate`
inherits the parent's tool config, including any `target:` - see
`amplifier_module_tool_delegate._merge_tools`) each used to independently
build a `ComputerTool` for the SAME physical machine and independently call
`_build_announcement()` - so a human who answered the FIRST session's
dialog got asked again, seconds later, by the second.

`shared_transport.py` already fixed the analogous problem for the SSH
connection itself (one agent process per target, shared, refcounted); these
tests prove the disclosure GATE gets the same one-decision-per-target
treatment, and reuses whatever the first mount() decided instead of asking
(or refusing) again.

Real-hardware verification of the remote path (proving this reaches an
actual target and that only ONE physical channel-open happens) lives
outside this file - see docs/designs/ and the session notes for the
Windows-via-WSL2 reproduction; these tests are the fast, deterministic,
no-SSH-required proof of the dedup logic itself.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "modules" / "tool-computer-use"))

import amplifier_module_tool_computer_use as cu
from amplifier_module_tool_computer_use.coexistence_guard import CoexistenceGuard
from amplifier_module_tool_computer_use.geometry import Display
from amplifier_module_tool_computer_use.presence import PresenceMonitor

_DISPLAY = Display(
    screen_width=1920, screen_height=1080, model_width=1280, model_height=720
)


def _guard(platform: str = "macos", idle_ms: float = 999_999.0) -> CoexistenceGuard:
    presence = PresenceMonitor(idle_source=lambda: idle_ms, platform=platform)
    return CoexistenceGuard(presence=presence, release_all=lambda reason: [])


class _FakeRemoteBackend:
    """Same shape as `test_remote_announcement_wiring.py`'s fake, plus a
    `user_host` attribute - the field this fix adds to the real
    `RemoteBackend` so two mounts against the SAME host (not merely the
    same platform) are recognized as the SAME channel."""

    is_remote = True

    def __init__(self, presence_platform: str, user_host: str) -> None:
        self.presence_platform = presence_platform
        self.user_host = user_host
        self.name = f"remote-ssh:{presence_platform}"
        self.announce_calls: list[dict] = []
        self._raise_result: dict = {}
        self._raise_exc: Exception | None = None

    def announce_raise(self, **kwargs):
        self.announce_calls.append(kwargs)
        if self._raise_exc is not None:
            raise self._raise_exc
        return self._raise_result


# -- the headline defect: two mounts, one physical dialog -------------------


def test_second_mount_for_the_same_host_does_not_reask_after_continue():
    """FAILS WITHOUT THE FIX: before this cache existed, a second `mount()`
    for the same remote macOS target called `announce_raise` again, showing
    a genuinely second dialog - even though the first was already answered
    'Continue'."""
    parent_backend = _FakeRemoteBackend("macos", user_host="user@alienware-r13")
    parent_backend._raise_result = {"button": "Continue", "gave_up": False}
    cu._build_announcement(parent_backend, _guard("macos"), {}, _DISPLAY)
    assert len(parent_backend.announce_calls) == 1

    # A delegated child session mounts a SEPARATE ComputerTool/backend
    # instance for the SAME host - exactly what `tool-delegate` inheriting
    # the parent's `target:` config produces.
    child_backend = _FakeRemoteBackend("macos", user_host="user@alienware-r13")
    child_backend._raise_result = {"button": "Pause", "gave_up": False}  # ignored
    result = cu._build_announcement(child_backend, _guard("macos"), {}, _DISPLAY)

    assert child_backend.announce_calls == [], (
        "the child mount() must NOT show a second dialog - the human "
        "already answered for this exact machine"
    )
    assert (
        result is None
    )  # the (reused) macOS decision: proceed, no handle to keep alive


def test_second_mount_for_the_same_host_reuses_a_refusal_without_reasking():
    """The other half of 'continue not honored': a declined channel must
    stay declined for every later mount() against the same host too -
    re-asking after a human already said no is the exact anti-pattern this
    fix exists to close."""
    parent_backend = _FakeRemoteBackend("macos", user_host="user@alienware-r13")
    parent_backend._raise_result = {"button": "Pause", "gave_up": False}
    try:
        cu._build_announcement(parent_backend, _guard("macos"), {}, _DISPLAY)
        raised = False
    except cu.AnnouncementRefused:
        raised = True
    assert raised

    child_backend = _FakeRemoteBackend("macos", user_host="user@alienware-r13")
    child_backend._raise_result = {
        "button": "Continue",
        "gave_up": False,
    }  # never reached
    try:
        cu._build_announcement(child_backend, _guard("macos"), {}, _DISPLAY)
        raised_again = False
    except cu.AnnouncementRefused:
        raised_again = True

    assert raised_again, "a refusal for this host must be reused, not re-decided"
    assert child_backend.announce_calls == [], (
        "the child mount() must not show a second dialog to obtain a "
        "decision this process already has"
    )


def test_different_hosts_are_independent_channels():
    """The fix must not over-cache: two DIFFERENT remote machines are two
    different physical channels and each gets asked."""
    mac_backend = _FakeRemoteBackend("macos", user_host="user@a-mac")
    mac_backend._raise_result = {"button": "Continue", "gave_up": False}
    cu._build_announcement(mac_backend, _guard("macos"), {}, _DISPLAY)

    other_mac_backend = _FakeRemoteBackend("macos", user_host="user@another-mac")
    other_mac_backend._raise_result = {"button": "Continue", "gave_up": False}
    cu._build_announcement(other_mac_backend, _guard("macos"), {}, _DISPLAY)

    assert len(mac_backend.announce_calls) == 1
    assert len(other_mac_backend.announce_calls) == 1, (
        "a different host must not be silently treated as already-answered "
        "just because another macOS target was - `backend.name` alone "
        "('remote-ssh:macos') is identical for both; user_host is what "
        "must disambiguate them"
    )


def test_local_backends_are_deduped_by_backend_name():
    """Local (non-remote) backends have no `user_host` - a controller
    process only ever drives one local desktop, so `backend.name` alone
    (e.g. 'linux-x11') is already a correct, unique-enough channel key."""

    class _FakeLocalBackend:
        is_remote = False

        def __init__(self, name: str) -> None:
            self.name = name

    key_a = cu._channel_identity(_FakeLocalBackend("linux-x11"))
    key_b = cu._channel_identity(_FakeLocalBackend("linux-x11"))
    key_c = cu._channel_identity(_FakeLocalBackend("windows-wsl2"))
    assert key_a == key_b
    assert key_a != key_c


def test_remote_channel_key_uses_user_host_not_the_composite_name():
    backend = _FakeRemoteBackend("macos", user_host="brkrabac@brians-macbook-pro-os")
    assert cu._channel_identity(backend) == "remote:brkrabac@brians-macbook-pro-os"


def test_remote_channel_key_falls_back_to_name_if_user_host_absent():
    """A future `Backend` that sets `is_remote = True` without a
    `user_host` attribute must not crash `_channel_identity` - it falls
    back to `backend.name`, same as before this fix existed."""

    class _BareRemoteBackend:
        is_remote = True
        name = "remote-ssh:some-future-platform"

    assert cu._channel_identity(_BareRemoteBackend()) == (
        "remote:remote-ssh:some-future-platform"
    )


# -- overlay channels: confirm the wrapper does not interfere with their ----
# -- OWN existing single-call-per-mount behavior for a first, uncached mount -


def test_overlay_channel_still_shown_on_first_mount(monkeypatch):
    calls = []

    class _FakeRemoteOverlayBackend(_FakeRemoteBackend):
        pass

    backend = _FakeRemoteOverlayBackend("linux-x11", user_host="user@a-linux-box")
    backend._raise_result = {
        "shown": True,
        "buttons": {"pause": [0, 0, 10, 10], "cancel": [20, 0, 30, 10]},
    }
    result = cu._build_announcement(backend, _guard("linux-x11"), {}, _DISPLAY)
    assert len(backend.announce_calls) == 1
    assert isinstance(result, cu._RemoteAnnouncementHandle)
    del calls  # unused, kept for readability of intent above
