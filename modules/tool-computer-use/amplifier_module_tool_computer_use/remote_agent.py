"""Standalone remote computer-use agent - Phase 1.

This is the process that runs ON the target machine, reached over SSH by
`SshTransport`, and speaks the NDJSON wire protocol defined in `wire.py`. It
has ONE job: read one JSON request per line from stdin, execute it against
whichever platform `Backend` `registry.select_backend()` picks for THIS
machine (the exact same `MacOSBackend`/`LinuxX11Backend`/`WindowsBackend` that
already back the local path and its 83 tests - see
`docs/designs/remote-transport.md` \u00a72, "one implementation, two deployment
shapes"), and write one JSON response per line to stdout.

Deployment note: this module is designed to be `runpy.run_module()`'d after a
bootstrap stub extracts this package's files into a scratch directory and adds
it to `sys.path` - see `ssh_transport.py`. It has NO dependency on
`amplifier_core` (this whole package doesn't - see `pyproject.toml`), so it
runs standalone on a target that has never heard of Amplifier. Pillow IS a
real dependency (needed for `capture_scaled`) - `ssh_transport.py` provisions
it via `uv run --with pillow`, per the verified environment facts in the
design doc (\u00a73.1): both real targets have `uv` but neither has `pip3`
guaranteed nor Pillow pre-installed.

Stdout hygiene (\u00a76.4) happens FIRST, before any other import, because
PyObjC (macOS) and some X11 bindings can print to stdout on import, which
would corrupt the very first bytes of the protocol stream.
"""

from __future__ import annotations

import os
import sys

# -- \u00a76.4: stdout hygiene, before any other import ------------------------
# Duplicate the real stdout fd into a private "protocol channel" fd, then
# redirect fd 1 (stdout) to fd 2 (stderr). Everything after this line that
# writes to `print()`/`sys.stdout` lands on stderr (agent logs, diagnostics);
# only writes through `_PROTO` are the wire protocol. `ssh -T` (no pty) means
# no line-ending translation to worry about either side.
_PROTO = os.fdopen(os.dup(1), "w", encoding="utf-8")
os.dup2(2, 1)

import argparse
import base64
import io
import json
import logging
import signal
import sys as _sys
import threading
import traceback
from collections.abc import Callable
from typing import Any, ClassVar

# Package-relative imports: this module always runs as part of the
# `amplifier_module_tool_computer_use` (locally) or a same-shaped extracted
# package (remotely) - see `ssh_transport.PAYLOAD_MODULES`.
from .backend import Backend, BackendError
from .ledger import DEFAULT_DEADMAN_SECONDS, HeldInputLedger
from .registry import NoBackendAvailable, select_backend
from .wire import PROTOCOL_VERSION, Request, Response, classify_op

logging.basicConfig(
    level=logging.INFO,
    stream=sys.stderr,
    format="%(asctime)s agent %(levelname)s %(message)s",
)
logger = logging.getLogger("amplifier-cu-agent")


def _content_hash() -> str:
    """The sha256 the controller computed over the exact bytes it sent, stashed
    by the bootstrap stub - see `ssh_transport.py`'s `_BOOTSTRAP_STUB`. Reported
    back in the handshake so the controller can fail loud on any mismatch
    (\u00a76.1/\u00a77) - this is NOT recomputed here, it is the receiver's own
    independent hash of what it actually read off the wire, compared against
    the sender's hash of what it actually sent.
    """
    return os.environ.get("AMPLIFIER_CU_AGENT_SHA256", "")


def _probe_permissions(backend: Backend) -> dict[str, bool]:
    """Best-effort, backend-specific permission probe for the handshake.

    \u00a76.1: "Discovering a TCC denial at connect time is a clear error
    message; discovering it at first click is a mystery." macOS TCC grants
    attach to the *responsible* process, which can differ depending on the
    launch chain (`python3` directly vs. `uv run`) - so this probes for real,
    every connection, rather than trusting a value cached from a previous run.
    """
    permissions: dict[str, bool] = {}
    if backend.name != "macos":
        return permissions
    try:
        from . import macos as _macos  # local import: only exists/imports on Darwin

        permissions["accessibility"] = bool(_macos._ax_is_process_trusted())
    except Exception:
        logger.exception("permission probe: accessibility check failed")
        permissions["accessibility"] = False
    try:
        png = backend.capture()
        permissions["screen_recording"] = len(png) > 0 and png[:8] == (
            b"\x89PNG\r\n\x1a\n"
        )
    except Exception:
        logger.exception("permission probe: screen_recording check failed")
        permissions["screen_recording"] = False
    return permissions


class UnsupportedOpError(BackendError):
    """This backend/platform combination does not implement `op` in Phase 1."""


class RemoteAgent:
    """Owns the ledger, the backend, and op dispatch. `run()` is the NDJSON
    read/execute/respond loop; everything else is a release trigger or an op
    handler."""

    def __init__(
        self,
        backend: Backend,
        *,
        deadman_seconds: float = DEFAULT_DEADMAN_SECONDS,
        read_only: bool = True,
    ) -> None:
        self.backend = backend
        self.read_only = read_only
        self._ledger = HeldInputLedger(
            deadman_seconds=deadman_seconds, on_release=self._on_release
        )
        self._stop_event = threading.Event()
        self._lock = threading.Lock()

    def _on_release(self, kind: str, token: str) -> None:
        # \u00a710.2/\u00a73.3: this exact line, `RELEASED:<token>`, is the
        # already-verified proof that release actually fired - printed to
        # stderr (never the protocol channel) so it survives even if stdout
        # is already gone by the time release happens.
        print(f"RELEASED:{token}", file=sys.stderr, flush=True)

    # -- lifecycle ------------------------------------------------------------

    def install_signal_handlers(self) -> None:
        def _handler(signum: int, _frame: Any) -> None:
            logger.warning("agent: signal %s received - releasing and exiting", signum)
            self._ledger.stop()
            _sys.exit(0)

        for sig in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
            try:
                signal.signal(sig, _handler)
            except (ValueError, OSError):  # pragma: no cover - platform-dependent
                pass

    def handshake(self) -> dict[str, Any]:
        probe = self.backend.probe()
        monitors = []
        try:
            monitors = [
                {
                    "id": m.id,
                    "x": m.x,
                    "y": m.y,
                    "width": m.width,
                    "height": m.height,
                    "primary": m.primary,
                }
                for m in self.backend.list_monitors()
            ]
        except BackendError as exc:
            logger.warning("handshake: list_monitors unavailable: %s", exc)
        capabilities = ["capture_scaled", "held_ledger", "audit", "read_only"]
        return {
            "protocol": PROTOCOL_VERSION,
            "agent_sha256": _content_hash(),
            "python": _sys.version.split()[0],
            "platform": _sys.platform,
            "backend": self.backend.name,
            "probe": {"available": probe.available, "reason": probe.reason},
            "capabilities": capabilities,
            "permissions": _probe_permissions(self.backend),
            "monitors": monitors,
        }

    def run(self, stdin: Any, stdout: Any) -> None:
        """The NDJSON read loop. `stdin`/`stdout` are injected (not `sys.stdin`/
        `_PROTO`) so this is directly unit-testable against plain pipes with a
        fake backend - see `tests/test_remote_agent_ledger.py`."""
        handshake = self.handshake()
        stdout.write(json.dumps({"id": 0, "ok": True, "result": handshake}) + "\n")
        stdout.flush()
        try:
            for line in stdin:
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                    req = Request(
                        id=int(data["id"]),
                        op=str(data["op"]),
                        args=data.get("args") or {},
                    )
                except Exception as exc:  # noqa: BLE001 - malformed line, not our bug
                    logger.warning("malformed request line: %s (%s)", line[:200], exc)
                    continue
                resp = self._dispatch(req)
                stdout.write(resp.encode().decode("utf-8"))
                stdout.flush()
                if req.op == "bye":
                    break
        finally:
            # \u00a710.2 primary path: stdin EOF (the loop above ends when the
            # iterator is exhausted, i.e. the pipe closed) triggers release
            # here, unconditionally, whether the loop ended via EOF, `bye`, or
            # an exception.
            self._ledger.stop()

    # -- dispatch ---------------------------------------------------------------

    def _dispatch(self, req: Request) -> Response:
        try:
            op_class = classify_op(req.op)
        except ValueError as exc:
            return Response(
                id=req.id, ok=False, error_type="UnknownOpError", error_message=str(exc)
            )
        if self.read_only and op_class == "write" and req.op != "release_all":
            return Response(
                id=req.id,
                ok=False,
                error_type="BackendError",
                error_message=f"op {req.op!r} blocked: agent enforces read_only "
                "(defence in depth - see docs/designs/remote-transport.md \u00a710.4)",
            )
        handler = self._HANDLERS.get(req.op)
        if handler is None:
            return Response(
                id=req.id,
                ok=False,
                error_type="UnsupportedOpError",
                error_message=f"op {req.op!r} not implemented in Phase 1",
            )
        try:
            result = handler(self, req.args)
            return Response(id=req.id, ok=True, result=result)
        except BackendError as exc:
            return Response(
                id=req.id,
                ok=False,
                error_type=type(exc).__name__,
                error_message=str(exc),
            )
        except Exception as exc:
            logger.exception("op %s raised", req.op)
            return Response(
                id=req.id,
                ok=False,
                error_type=type(exc).__name__,
                error_message=str(exc),
            )

    # -- op handlers --------------------------------------------------------

    def _op_probe(self, _args: dict[str, Any]) -> dict[str, Any]:
        p = self.backend.probe()
        return {"available": p.available, "reason": p.reason}

    def _op_screen_geometry(self, _args: dict[str, Any]) -> dict[str, Any]:
        g = self.backend.screen_geometry()
        return {
            "width": g.width,
            "height": g.height,
            "origin_x": g.origin_x,
            "origin_y": g.origin_y,
        }

    def _op_list_monitors(self, _args: dict[str, Any]) -> list[dict[str, Any]]:
        return [
            {
                "id": m.id,
                "x": m.x,
                "y": m.y,
                "width": m.width,
                "height": m.height,
                "primary": m.primary,
                "name": m.name,
            }
            for m in self.backend.list_monitors()
        ]

    def _op_cursor_position(self, _args: dict[str, Any]) -> dict[str, Any]:
        x, y = self.backend.cursor_position()
        return {"x": x, "y": y}

    def _op_presence_idle(self, _args: dict[str, Any]) -> dict[str, Any]:
        """Forward a presence-idle read to whichever real platform backend
        this agent is running (`docs/designs/coexistence.md` \u00a75) - the
        controller-side `RemoteBackend.presence_idle_ms()` calls this over
        the already-open NDJSON channel rather than duplicating any presence
        logic on the controller. `UnsupportedOpError` (not a guessed value)
        if this target's own backend has no `presence_idle_ms()` - never
        silently report a fabricated idle reading (\u00a79.6).
        """
        idle_source = getattr(self.backend, "presence_idle_ms", None)
        if idle_source is None:
            raise UnsupportedOpError(
                f"backend {self.backend.name!r} has no presence_idle_ms() - "
                "coexistence presence detection is not available on this target"
            )
        return {"idle_ms": float(idle_source())}

    def _op_capture_scaled(self, args: dict[str, Any]) -> dict[str, Any]:
        # C1: downscale HERE, on the target, before any bytes cross the wire -
        # never send the native capture and let the controller resize it.
        from PIL import Image  # lazy: only this op needs Pillow

        from .geometry import compute_display

        region = args.get("region")
        region_t = tuple(region) if region else None
        model_w = int(args.get("model_w", 1280))
        model_h = int(args.get("model_h", 800))
        max_edge = int(args.get("max_edge", 1280))
        max_pixels = int(args.get("max_pixels", 1_150_000))
        png = self.backend.capture(region=region_t)
        with Image.open(io.BytesIO(png)) as img:
            native_w, native_h = img.size
            img = img.convert("RGB")
            if region_t:
                tw, th = compute_display(img.width, img.height, max_edge, max_pixels)
                if (tw, th) != (img.width, img.height):
                    img = img.resize((tw, th), Image.Resampling.LANCZOS)
            else:
                img = img.resize((model_w, model_h), Image.Resampling.LANCZOS)
            buf = io.BytesIO()
            img.save(buf, format="PNG")
        return {
            "enc": "b64",
            "png": base64.standard_b64encode(buf.getvalue()).decode(),
            "w": img.width,
            "h": img.height,
            "native_w": native_w,
            "native_h": native_h,
            "scaled_on": "agent",
        }

    def _op_move(self, args: dict[str, Any]) -> None:
        self.backend.move(int(args["x"]), int(args["y"]))

    def _op_click(self, args: dict[str, Any]) -> None:
        self.backend.click(
            args.get("x"),
            args.get("y"),
            button=str(args.get("button", "left")),
            count=int(args.get("count", 1)),
        )

    def _op_type_text(self, args: dict[str, Any]) -> None:
        self.backend.type_text(str(args.get("text", "")))

    def _op_key(self, args: dict[str, Any]) -> None:
        self.backend.key(str(args["combo"]))

    def _op_hold(self, args: dict[str, Any]) -> dict[str, Any]:
        """Day-one ledger proof-of-mechanism (\u00a710.2): hold a single modifier
        key down, with NO matching up, tracked in the ledger until an explicit
        `release_all`, stdin EOF, a signal, or the deadman timer releases it.

        Not one of the ops Phase 2 adds to the wire surface (`hold_key`,
        `mouse_down`/`up`, `drag`) - this is the minimal, safest-possible
        primitive that actually exercises the guarantee end to end against a
        live desktop: a bare modifier key-down cannot click, type, or move
        anything, so it is safe to demonstrate on a machine a human is
        actively using. Matches the mechanic already proven in
        docs/designs/remote-transport.md \u00a73.3 (`{"op":"hold","held":["ctrl"]}`
        ... `RELEASED:ctrl`).
        """
        name = str(args["key"]).lower()
        release_fn = self._modifier_down(name)
        self._ledger.hold("key", name, release_fn)
        return {"held": self._ledger.held_tokens}

    def _op_release_all(self, _args: dict[str, Any]) -> dict[str, Any]:
        released = self._ledger.release_all(reason="release_all op")
        return {"released": released}

    def _op_bye(self, _args: dict[str, Any]) -> dict[str, Any]:
        return {"bye": True}

    _HANDLERS: ClassVar[dict[str, Callable[[RemoteAgent, dict[str, Any]], Any]]] = {
        "probe": _op_probe,
        "screen_geometry": _op_screen_geometry,
        "list_monitors": _op_list_monitors,
        "cursor_position": _op_cursor_position,
        "capture_scaled": _op_capture_scaled,
        "move": _op_move,
        "click": _op_click,
        "type_text": _op_type_text,
        "key": _op_key,
        "hold": _op_hold,
        # Coexistence presence read (docs/designs/coexistence.md §5). Missing
        # this entry is why a real SSH session got
        # `UnsupportedOpError: op 'presence_idle' not implemented in Phase 1`
        # on every remote presence read: `_op_presence_idle` was fully
        # implemented above and `wire.py` classified it READ, but `_dispatch`
        # looks ops up HERE, so the handler was unreachable. Every unit test
        # passed because they call `_op_presence_idle` directly or mock
        # `_call` - none exercised this table. The consequence on real
        # hardware was not a silent gap but a hard crash: the guard WAS
        # constructed (platform=windows-wsl2, guard_ms=20.0) and then raised
        # IdleUnreadableError on its first sample.
        "presence_idle": _op_presence_idle,
        "release_all": _op_release_all,
        "bye": _op_bye,
    }

    # -- modifier hold/release primitives (macOS + Linux X11 only) ----------
    #
    # Deliberately NOT routed through `Backend` - see `_op_hold`'s docstring.
    # This is the one place remote_agent.py steps outside "one implementation,
    # two deployment shapes", and only for a diagnostic/safety-proof op that
    # is not part of the shipped action surface Claude ever calls (that
    # surface goes through `Backend.key`/`hold_key`, unchanged).

    def _modifier_down(self, name: str) -> Callable[[], None]:
        if self.backend.name == "macos":
            return self._macos_modifier_down(name)
        if self.backend.name.startswith("linux"):
            return self._linux_modifier_down(name)
        raise UnsupportedOpError(
            f"'hold' is not implemented for backend {self.backend.name!r} in "
            "Phase 1 (macOS and Linux X11 only)"
        )

    def _macos_modifier_down(self, name: str) -> Callable[[], None]:
        from . import macos as _macos

        keycode = _MACOS_MODIFIER_KEYCODE.get(name)
        if keycode is None:
            raise BackendError(
                f"unknown modifier {name!r}; expected one of "
                f"{sorted(_MACOS_MODIFIER_KEYCODE)}"
            )
        Quartz = _macos.Quartz
        down = Quartz.CGEventCreateKeyboardEvent(None, keycode, True)
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, down)

        def _release() -> None:
            up = Quartz.CGEventCreateKeyboardEvent(None, keycode, False)
            Quartz.CGEventPost(Quartz.kCGHIDEventTap, up)

        return _release

    def _linux_modifier_down(self, name: str) -> Callable[[], None]:
        from Xlib import X
        from Xlib.ext import xtest

        from . import linux_x11 as _linux

        keysym_name = _LINUX_MODIFIER_KEYSYM.get(name)
        if keysym_name is None:
            raise BackendError(
                f"unknown modifier {name!r}; expected one of "
                f"{sorted(_LINUX_MODIFIER_KEYSYM)}"
            )
        display = getattr(self.backend, "_display")  # noqa: B009 - deliberate, same-package reach
        keysym = _linux._keysym_for_name(keysym_name)
        keycode = display.keysym_to_keycode(keysym)
        xtest.fake_input(display, X.KeyPress, keycode)
        display.sync()

        def _release() -> None:
            xtest.fake_input(display, X.KeyRelease, keycode)
            display.sync()

        return _release


#: Apple HIToolbox virtual keycodes for the four modifier keys, left-hand
#: variants (`kVK_*` constants) - public, stable, Apple-documented values, the
#: same class of constant `macos.py`'s own `_KEYCODE` table already hardcodes.
_MACOS_MODIFIER_KEYCODE = {
    "shift": 0x38,  # kVK_Shift
    "ctrl": 0x3B,  # kVK_Control
    "control": 0x3B,
    "alt": 0x3A,  # kVK_Option
    "option": 0x3A,
    "cmd": 0x37,  # kVK_Command
    "command": 0x37,
}

_LINUX_MODIFIER_KEYSYM = {
    "shift": "Shift_L",
    "ctrl": "Control_L",
    "control": "Control_L",
    "alt": "Alt_L",
    "cmd": "Super_L",
    "super": "Super_L",
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Amplifier remote computer-use agent")
    parser.add_argument(
        "--deadman-seconds", type=float, default=DEFAULT_DEADMAN_SECONDS
    )
    parser.add_argument(
        "--read-only",
        type=str,
        default="true",
        help="'true' (default, safe) or 'false' - agent-side enforcement, "
        "defence in depth alongside the controller's own gate (\u00a710.4)",
    )
    args = parser.parse_args(argv)

    try:
        backend = select_backend({})
    except NoBackendAvailable as exc:
        _PROTO.write(
            json.dumps(
                {
                    "id": 0,
                    "ok": True,
                    "result": {
                        "protocol": PROTOCOL_VERSION,
                        "agent_sha256": _content_hash(),
                        "python": _sys.version.split()[0],
                        "platform": _sys.platform,
                        "backend": "none",
                        "probe": {"available": False, "reason": str(exc)},
                        "capabilities": [],
                        "permissions": {},
                        "monitors": [],
                    },
                }
            )
            + "\n"
        )
        _PROTO.flush()
        logger.error("no backend available: %s", exc)
        return 1

    agent = RemoteAgent(
        backend,
        deadman_seconds=args.deadman_seconds,
        read_only=str(args.read_only).lower() != "false",
    )
    agent.install_signal_handlers()
    try:
        agent.run(sys.stdin, _PROTO)
    except Exception:  # noqa: BLE001 - top-level agent loop guard; must never
        # let an unexpected exception escape without releasing held input.
        logger.error("agent loop crashed:\n%s", traceback.format_exc())
        agent._ledger.stop()
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
