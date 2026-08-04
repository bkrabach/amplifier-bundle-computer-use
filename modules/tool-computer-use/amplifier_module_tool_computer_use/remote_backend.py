"""`RemoteBackend`: implements the `Backend` protocol by marshalling every call
across `SshTransport` to the SAME platform backend code running on the target -
`docs/remote-transport.md` \u00a75.

Owns request ids, op classification, and the one retry rule that matters:
WRITE ops are never retried (\u00a76.3) - a lost response does not mean the
action didn't land.
"""

from __future__ import annotations

import itertools
import logging
from typing import Any

from .backend import (
    BackendError,
    MonitorInfo,
    ProbeResult,
    ScreenGeometry,
    WindowList,
)
from .ssh_transport import AgentStderrError, SshConnectError, SshTransport
from .wire import Request, Response, classify_op

logger = logging.getLogger(__name__)


class RemoteTargetUnavailable(AgentStderrError):
    """A `target:` was explicitly configured and could not be reached.

    Deliberately NOT a subclass of `registry.NoBackendAvailable` - that type is
    caught by `mount()` and degrades to a silent skip, which is exactly the
    wrong behavior for a remote target (\u00a79 / acceptance item 7): "an
    unreachable target fails loud at mount and does not fall back to the
    controller's local desktop". Falling back silently to a local backend
    when a specific remote machine was asked for would mean the agent starts
    driving the WRONG desktop with zero indication - the worst possible
    outcome this design can produce (\u00a79).
    """


class RemoteBackend:
    """`Backend` implementation that drives a target machine over SSH.

    `is_remote = True` is a plain class attribute other modules (`ComputerTool`,
    the gate hook) check via `getattr(backend, "is_remote", False)` - no
    isinstance check, no import coupling, so any future backend can opt into
    the same remote-safety defaults without this module needing to know about
    it (matches the Backend `Protocol`'s own duck-typing shape).
    """

    is_remote = True

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        cfg = config or {}
        self.name = f"remote-ssh:{cfg.get('_host', '?')}"
        self._transport: SshTransport | None = cfg.get("_transport")
        self._ids = itertools.count(1)
        self._connected = False
        # Coexistence (docs/coexistence.md \u00a75): the bare REMOTE
        # backend name (e.g. "windows-wsl2", "linux-x11", "macos") from the
        # handshake, as reported by the agent's own `self.backend.name` on
        # the target - NOT the composite `self.name` above (which is
        # "remote-ssh:<that value>" and is deliberately kept as the durable
        # halt-state / log key, unique per remote target). `_build_coexistence_guard`
        # (`__init__.py`) resolves the GUARD_MS band from THIS, never from
        # `self.name` - "remote-ssh:windows-wsl2" is not a `GUARD_MS` key and
        # never will be; the underlying platform's measured band is. `None`
        # until `connect()` completes a handshake.
        self.presence_platform: str | None = None

    # -- connection lifecycle (used by registry.select_backend) -------------

    def connect(
        self,
        *,
        required_permissions: tuple[str, ...] = (),
        connect_timeout: float = 30.0,
    ) -> dict[str, Any]:
        if self._transport is None:
            raise RemoteTargetUnavailable("no transport configured for RemoteBackend")
        try:
            handshake = self._transport.connect(
                required_permissions=required_permissions,
                connect_timeout=connect_timeout,
            )
        except SshConnectError as exc:
            raise RemoteTargetUnavailable(
                exc.message, agent_stderr=exc.agent_stderr
            ) from exc
        self._connected = True
        self.name = f"remote-ssh:{handshake.get('backend', '?')}"
        self.presence_platform = handshake.get("backend")
        return handshake

    def close(self) -> None:
        if self._transport is not None:
            self._transport.close()
        self._connected = False

    # -- request/response plumbing -------------------------------------------

    def _call(self, op: str, **args: Any) -> Any:
        if self._transport is None or not self._connected:
            raise BackendError(f"remote backend not connected (op={op!r})")
        req = Request(id=next(self._ids), op=op, args=args)
        op_class = classify_op(op)
        try:
            line = self._transport.send(req.encode())
        except SshConnectError as exc:
            # \u00a76.3: WRITE ops are NEVER retried, here or anywhere else in
            # this class - a lost response does not tell us whether the
            # action landed, and replaying it is how you get two clicks on
            # "Confirm". READ/CONTROL failures still just surface as an
            # error; nothing in this method silently retries any op class.
            raise BackendError(f"remote op {op!r} ({op_class}) failed: {exc}") from exc
        resp = Response.decode(line)
        if resp.id != req.id:
            raise BackendError(
                f"wire desync: sent id={req.id}, got id={resp.id} for op {op!r}"
            )
        if not resp.ok:
            raise BackendError(f"{resp.error_type}: {resp.error_message}")
        return resp.result

    # -- coexistence presence source (docs/coexistence.md §5) ---------

    def presence_idle_ms(self) -> float:
        """Milliseconds since the REMOTE machine last saw any input.

        Without this method `_build_coexistence_guard` (`__init__.py`) builds
        no guard at all, because it gates on `getattr(backend,
        "presence_idle_ms", None)`. That is exactly what happened before this
        existed: every `target: ssh://...` session - on every platform - ran
        with zero coexistence protection, which is the primary deployment
        shape (a headless controller driving a desktop over SSH). A live
        session against remote Windows confirmed it: `guard built: False`, and
        no halt record was ever written.

        The read is forwarded over the same per-target singleton transport
        every other op uses - no second connection, no new process. It is
        classified READ in `wire.py`, so the `_call` docstring's rule that
        WRITE ops are never retried does not loosen here.

        Deliberately NOT piggybacked onto an existing response: `PresenceMonitor`
        computes `last_input_at = now - idle_ms`, so a value captured during an
        earlier round trip yields an arithmetically wrong timestamp, and wrong
        in the permissive direction (it makes a human look older than they are).
        A stale presence read is a false negative in a safety gate, which is the
        one failure mode this whole mechanism exists to prevent. One extra
        round trip per guarded write, bounded by tailnet RTT, is the honest
        price.
        """
        result = self._call("presence_idle")
        value = result.get("idle_ms") if isinstance(result, dict) else result
        if value is None:
            raise BackendError(
                "remote agent returned no idle_ms for presence_idle - the "
                "target's backend has no presence source, so coexistence "
                "cannot be enforced for it"
            )
        return float(value)

    # -- Backend protocol -----------------------------------------------------

    def probe(self) -> ProbeResult:
        if not self._connected:
            return ProbeResult(False, "not connected")
        try:
            result = self._call("probe")
            return ProbeResult(bool(result.get("available")), result.get("reason", ""))
        except BackendError as exc:
            return ProbeResult(False, str(exc))

    def screen_geometry(self) -> ScreenGeometry:
        r = self._call("screen_geometry")
        return ScreenGeometry(
            width=r["width"],
            height=r["height"],
            origin_x=r.get("origin_x", 0),
            origin_y=r.get("origin_y", 0),
        )

    def list_monitors(self) -> list[MonitorInfo]:
        return [
            MonitorInfo(
                id=m["id"],
                x=m["x"],
                y=m["y"],
                width=m["width"],
                height=m["height"],
                primary=m.get("primary", False),
                name=m.get("name", ""),
            )
            for m in self._call("list_monitors")
        ]

    def capture(self, region: tuple[int, int, int, int] | None = None) -> bytes:
        # Phase 1 does not wire the raw `capture` op for routine use - C1's
        # whole point is that the SCALED path is what should be exercised on
        # the hot path. `capture_scaled` (below) is the capability
        # `imaging.capture_scaled_b64` prefers when it is present.
        raise BackendError(
            "RemoteBackend.capture() (native resolution over the wire) is not "
            "supported in Phase 1 - use capture_scaled (see C1 in "
            "docs/remote-transport.md)"
        )

    def capture_scaled(
        self,
        region: tuple[int, int, int, int] | None,
        model_size: tuple[int, int],
        max_edge: int,
        max_pixels: int,
    ) -> str:
        """C1: the capability `imaging.capture_scaled_b64` detects via the
        class-descriptor idiom and prefers over `capture()` + local PIL resize
        - the agent downscales BEFORE the bytes cross the wire."""
        model_w, model_h = model_size
        result = self._call(
            "capture_scaled",
            region=list(region) if region else None,
            model_w=model_w,
            model_h=model_h,
            max_edge=max_edge,
            max_pixels=max_pixels,
        )
        return result["png"]

    def cursor_position(self) -> tuple[int, int]:
        r = self._call("cursor_position")
        return r["x"], r["y"]

    def move(self, x: int, y: int) -> None:
        self._call("move", x=x, y=y)

    def click(
        self, x: int | None, y: int | None, button: str = "left", count: int = 1
    ) -> None:
        self._call("click", x=x, y=y, button=button, count=count)

    def mouse_down(self, x: int | None, y: int | None, button: str = "left") -> None:
        raise BackendError("mouse_down over the wire is Phase 2 - see design doc")

    def mouse_up(self, x: int | None, y: int | None, button: str = "left") -> None:
        raise BackendError("mouse_up over the wire is Phase 2 - see design doc")

    def drag(self, start: tuple[int, int] | None, end: tuple[int, int]) -> None:
        raise BackendError("drag over the wire is Phase 2 - see design doc")

    def scroll(self, x: int | None, y: int | None, direction: str, amount: int) -> None:
        raise BackendError("scroll over the wire is Phase 2 - see design doc")

    def key(self, combo: str) -> None:
        self._call("key", combo=combo)

    def hold_key(self, combo: str, duration: float) -> None:
        raise BackendError("hold_key over the wire is Phase 2 - see design doc")

    def type_text(self, text: str, guard: Any = None) -> None:
        """See `Backend.type_text` for the `guard` parameter's contract.
        Accepted for protocol conformance but not used: the remote agent
        runs its own copy of a platform backend on the target (\u00a75 of
        `docs/remote-transport.md`), so any intra-op presence
        detection belongs on that side, not here."""
        del guard
        self._call("type_text", text=text)

    def list_windows(self) -> WindowList:
        raise BackendError("list_windows over the wire is Phase 2 - see design doc")

    def focus_window(self, handle: str) -> None:
        raise BackendError("focus_window over the wire is Phase 2 - see design doc")

    def get_clipboard(self) -> str:
        raise BackendError("get_clipboard over the wire is Phase 2 - see design doc")

    def set_clipboard(self, text: str) -> None:
        raise BackendError("set_clipboard over the wire is Phase 2 - see design doc")

    # -- Phase-1-only diagnostic op: the ledger proof (see remote_agent.py) ---

    def hold(self, key: str) -> list[str]:
        result = self._call("hold", key=key)
        return list(result.get("held", []))

    def release_all(self) -> list[str]:
        result = self._call("release_all")
        return list(result.get("released", []))
