"""Amplifier tool module: `computer` - Anthropic native computer-use, on whichever
desktop this machine can actually reach.

The tool mounts under the name `computer` so the orchestrator can execute the
`tool_use` blocks Claude emits for its built-in computer tool. `hook-computer-use`
promotes this tool's declaration to the *native* Anthropic tool type on the wire and
turns screenshot markers into real image content blocks.

Without the hook the tool still works as an ordinary function tool (Claude drives it
from the JSON schema below), it just cannot show Claude the screen.

Platform backend
-----------------
This module no longer assumes Windows. `mount()` probes every configured backend
(`registry.select_backend`) *before* registering any tool - D1: if nothing can serve
this machine, `computer`/`desktop` are not mounted at all, and the reason is logged
plainly. See `backend.py` for the protocol and why it is shaped the way it is.

Display geometry is resolved once, right after a backend is selected, and cached for
the life of the tool (D2): `native_tool_spec` used to call a bridge property that
shelled out to PowerShell with a 30s timeout on *every* provider request. It now
reads a plain in-memory value and can never block.
"""

from __future__ import annotations

import asyncio
import atexit
import base64
import hashlib
import inspect
import json
import logging
import os
import socket
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from amplifier_core.models import ToolResult

from .announce_macos import DEFAULT_TIMEOUT_SECONDS as MACOS_ANNOUNCE_TIMEOUT_SECONDS
from .announce_macos import AnnounceError, AnnounceResult
from .announce_macos import announce as macos_announce
from .backend import Backend, BackendError, MonitorInfo
from .coexistence_guard import CoexistenceGuard, HaltedError
from .exclusion import Rect
from .geometry import Display, ImageSpace, compute_display
from .halt_state import (
    load_halt,
    make_durable_halt_poll,
    record_halt,
    resolve_resume_command,
)
from .imaging import capture_scaled_b64
from .ledger import HeldInputLedger
from .linux_x11 import LinuxX11Backend
from .macos import MacOSBackend
from .monitors import PRIMARY, VIRTUAL_DESKTOP, attribute_monitor, select_monitor
from .overlay_linux import LinuxOverlay
from .overlay_windows import WindowsOverlay
from .presence import (
    GUARD_MS,
    Confidence,
    IdleUnreadableError,
    PresenceMonitor,
    PresenceSnapshot,
    PresenceState,
)
from .providers import dialect_for_tool_type, read_call
from .registry import NoBackendAvailable, select_backend
from .tool_versions import (
    beta_header_for,
    require_static_pairing,
    resolve_tool_version,
)
from .type_pacing import resolve_type_pacing_ms
from .windows import WindowsBackend

logger = logging.getLogger(__name__)

__version__ = "0.2.0"

#: Marker key the companion hook looks for in tool output.
MARKER = "__amplifier_computer_use__"

SHOT_DIR = Path.home() / ".amplifier" / "computer-use" / "shots"
SHOT_TTL_SECONDS = 2 * 60 * 60

#: Security hardening (adversarial review, no prior gating beyond the parent
#: directory's inherited umask): screenshots of a driven desktop are
#: sensitive content on a shared/multi-user controller box - a world- or
#: group-readable shot directory lets any other local account read them for
#: the full TTL window. `0700`/`0600` restrict both the per-session directory
#: and every file in it to this process's own user, regardless of umask
#: (umask only affects the mode `mkdir`/`open` request initially - it is not
#: itself a floor, so an explicit `os.chmod` after creation is what actually
#: guarantees this rather than merely hoping the umask happens to be strict).
_PRIVATE_DIR_MODE = 0o700
_PRIVATE_FILE_MODE = 0o600


def _text_digest(text: str) -> str:
    """A short, stable, non-reversible fingerprint for an audit log line -
    never the plaintext itself. Matches the discipline
    `docs/designs/remote-transport.md` \u00a710 specifies for `type_text`
    (`args_digest`, not `args`) - this bundle previously did not actually
    implement that logging for ANY action; this is the shared primitive
    behind extending it to every write op that carries free-form text
    (`type`, `set_clipboard`)."""
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:16]


def _bytes_digest(data: bytes) -> str:
    """Same fingerprint primitive as `_text_digest`, for binary payloads
    (screenshot/zoom PNG bytes) - \u00a73 of the task: \"add at least a content
    hash entry for captures.\""""
    return hashlib.sha256(data).hexdigest()[:16]


ACTIONS = [
    "screenshot",
    "zoom",
    "cursor_position",
    "mouse_move",
    "left_click",
    "right_click",
    "middle_click",
    "double_click",
    "triple_click",
    "left_mouse_down",
    "left_mouse_up",
    "left_click_drag",
    "scroll",
    "key",
    "hold_key",
    "type",
    "wait",
    "screen_info",
    "list_windows",
    "focus_window",
]

#: Actions that change the user's machine. Used for the confirm/read-only gate.
MUTATING = {
    "mouse_move",
    "left_click",
    "right_click",
    "middle_click",
    "double_click",
    "triple_click",
    "left_mouse_down",
    "left_mouse_up",
    "left_click_drag",
    "scroll",
    "key",
    "hold_key",
    "type",
    "focus_window",
}

_CLICK_ACTIONS = {
    "left_click": ("left", 1),
    "right_click": ("right", 1),
    "middle_click": ("middle", 1),
    "double_click": ("left", 2),
    "triple_click": ("left", 3),
}


def _prune_shots() -> None:
    """Delete expired screenshot files across every per-session subdirectory.

    Screenshots now live under `SHOT_DIR/<session_id>/*.png` (one subdirectory
    per `ComputerTool` instance - see `ComputerTool.__init__`'s `_session_id`
    and `execute()`) rather than one flat shared directory, so this glob is
    `*/*.png`, not `*.png`. Also best-effort removes session directories that
    are now empty, so a long-lived controller does not accumulate one empty
    directory per past session forever.
    """
    cutoff = time.time() - SHOT_TTL_SECONDS
    try:
        for old in SHOT_DIR.glob("*/*.png"):
            if old.stat().st_mtime < cutoff:
                old.unlink(missing_ok=True)
        for session_dir in SHOT_DIR.glob("*"):
            if session_dir.is_dir():
                try:
                    session_dir.rmdir()  # no-op (raises, caught) if not empty
                except OSError:
                    pass
    except OSError:  # pragma: no cover - best-effort housekeeping
        pass


class ComputerTool:
    """Executes Anthropic computer-tool actions against whatever desktop the
    selected `Backend` can reach."""

    def __init__(self, backend: Backend, config: dict[str, Any] | None = None) -> None:
        cfg = config or {}
        self._backend = backend
        # Kept for `_ensure_announced` (see that method): the announcement is
        # no longer built at mount() time, so the config it needs must be
        # available later, at first real use.
        self._cfg: dict[str, Any] = cfg
        self._max_edge = int(cfg.get("max_edge", 1280))
        self._max_pixels = int(cfg.get("max_pixels", 1_150_000))
        self._enable_zoom = bool(cfg.get("enable_zoom", True))

        # -- model <-> tool_version coupling fix -----------------------------
        # `_configured_tool_version`/`_model_hint` are the raw config values;
        # `_tool_version` is the live, resolved value `native_tool_spec` reads.
        # Resolved once here (may raise ToolVersionError - see tool_versions.py
        # module docstring for why mount time is allowed to fail loud) and then
        # kept current by `note_model()`, called by hook-computer-use on every
        # `provider:request` with the model actually about to be used.
        self._configured_tool_version: str | None = cfg.get("tool_version")
        self._model_hint: str | None = cfg.get("model")
        self._tool_version: str = require_static_pairing(
            self._model_hint, self._configured_tool_version
        )

        # -- remote-target safety posture ------------------------------------
        # `is_remote` is a plain attribute (not an isinstance/import check) so
        # any Backend can opt in without this module depending on
        # RemoteBackend's concrete type - only RemoteBackend sets it True
        # today (see remote_backend.py).
        self._is_remote = bool(getattr(backend, "is_remote", False))
        read_only_cfg = cfg.get("read_only")
        if read_only_cfg is None:
            # Unconfigured default: ON for remote (a machine you are, by
            # definition, not looking at - see docs/designs/remote-transport.md
            # \u00a714), unchanged (OFF) for local - preserves every existing
            # local-mode caller's behavior exactly.
            self._read_only = self._is_remote
        else:
            self._read_only = bool(read_only_cfg)
        gate_cfg = cfg.get("gate_writes")
        if gate_cfg is None:
            # "Destructive" is undecidable from a click (Delete looks like any
            # other click) - the only two honest options are gate-every-write
            # or gate-none (\u00a710.4). Default is gate-every-write, but only
            # matters when read_only is off (read_only already blocks every
            # write outright) and only for remote targets - local behavior is
            # unaffected. Flipping read_only off on a remote target therefore
            # can never silently produce "full write access + no gate": the
            # gate turns on in the same step, unless explicitly disabled below.
            self._gate_writes = self._is_remote and not self._read_only
        else:
            self._gate_writes = bool(gate_cfg)
            if self._is_remote and not self._gate_writes and not self._read_only:
                logger.warning(
                    "computer-use: gate_writes explicitly disabled for a remote, "
                    "non-read_only target - every write action will execute with "
                    "no confirmation gate. This is a deliberate, logged opt-out, "
                    "not a default."
                )
        # `unattended_writes_ok` and `gate_writes` are two answers to the SAME
        # policy question (\u00a710.4: "gate every write, or gate none") - not two
        # independent mechanisms. The config knob itself lives in
        # hook-computer-use (see that module's `mount()` docstring for why:
        # it is the module that answers `ask_user`/EOF-avoidance too, so it
        # owns the one place an operator sets it). `hook-computer-use`'s gate
        # handler syncs the live value onto THIS attribute on every
        # `tool:pre` call for `computer`/`desktop`, strictly before this
        # tool's own `execute()` runs for that same call - so by the time
        # `DesktopTool.execute()`'s fail-safe check reads it below, it
        # reflects the exact same decision the hook already made, instead of
        # silently re-deciding and contradicting it. Defaults False: with no
        # gate hook mounted (or one that has not run yet), this stays False
        # and the fail-safe denies - never a silent, un-authored escape
        # hatch.
        self._unattended_writes_ok: bool = False
        # A SEPARATE per-call signal from `_unattended_writes_ok` above -
        # deliberately, not the same flag wearing a second meaning. That one
        # answers "is nobody at the keyboard, and is that explicitly OK?" -
        # always False when a human interactively approves via `ask_user`,
        # since the whole point of that path is a human WAS asked. Reusing
        # it for the interactive case would make its name lie about which
        # of the two questions it is answering (see this module's own
        # incident history: names and return values that did not match
        # reality). This is the interactive counterpart: "did a human just
        # grant THIS specific call via `ask_user`?" `hook-computer-use`'s
        # gate handler resets it to False at the top of every `tool:pre`
        # call (same unconditional-sync point as `_unattended_writes_ok`,
        # so a stale True from an earlier approved call can never survive
        # into a later one), then sets it True only when it is about to
        # hand this exact call's decision to `ask_user`. That is safe to do
        # before the human actually answers: `ask_user` is a blocking gate
        # (`HOOKS_API.md` - priority 2, same tier as `deny`) - if the human
        # declines or times out, this tool's `execute()` is never called
        # for that call at all, so no observer ever reads a `True` paired
        # with a decline. Defaults False: with no gate hook mounted (or one
        # that has not run yet), this stays False and the fail-safe denies
        # - same "never a silent, un-authored escape hatch" rule as above.
        self._interactive_write_approved: bool = False
        # Per-monitor targeting (see monitors.py): default is "primary", i.e. one
        # real monitor, not the virtual-desktop bounding box around all of them.
        # Set to monitors.VIRTUAL_DESKTOP to opt into the old whole-desktop
        # behavior, or to a specific monitor id from list_monitors().
        self._target_monitor: str = str(cfg.get("target_monitor") or PRIMARY)
        # Whether `_target_monitor` was actually asked for (config, or a runtime
        # `select_monitor()` call) vs. just being the unconfigured default. See
        # `_resolve_display_for_target` - this is what decides whether monitor
        # enumeration failing is allowed to fall back to virtual-desktop mode
        # (default, silent-to-the-user machines) or must fail loud (an explicit
        # request the caller is entitled to know failed).
        self._target_monitor_explicit: bool = bool(cfg.get("target_monitor"))
        self._monitors: list[MonitorInfo] = []
        # None in virtual-desktop mode; otherwise the MonitorInfo `display` is
        # currently scoped to. See `_resolve_display_for_target`.
        self._current_monitor: MonitorInfo | None = None
        # D2: resolved once (by mount(), right after backend selection) and cached -
        # never touched on the request hot path. See `resolve_display`.
        self._display: Display | None = None

        # -- security hardening: per-session screenshot scoping ---------------
        # A fresh id per `ComputerTool` instance (i.e. per mount, in practice
        # per session) - `execute()` writes screenshots under
        # `SHOT_DIR/self._session_id/`, not the flat shared directory every
        # session previously wrote into together. See `execute()` and
        # `_prune_shots()`.
        self._session_id: str = uuid.uuid4().hex

        # -- security hardening: clipboard read policy ------------------------
        # `get_clipboard` (docs/DesktopTool) previously had no gate
        # beyond `read_only` - a full clipboard read flows verbatim into
        # `ToolResult.output`, then the model provider's API, then a durable
        # transcript, and a clipboard can carry things a screenshot never
        # shows (a just-copied password, an unseen paste buffer). This is a
        # DISTINCT, explicit policy from `read_only`/`gate_writes` (a read,
        # not a write) - see `DesktopTool.execute()`'s `get_clipboard` branch
        # for where it is enforced and audit-logged.
        #   - "allow": clipboard content returned verbatim (the only
        #     behavior that existed before this hardening pass).
        #   - "redact": length + a short content digest only, never the text
        #     itself - the same digest-not-plaintext discipline this pass
        #     also applies to `type_text`/`set_clipboard` audit logging.
        #   - "block": the action fails, same shape as `read_only` blocking
        #     a write.
        # Default mirrors this module's existing `read_only`/`gate_writes`
        # precedent exactly (safer for remote - a machine you are, by
        # definition, not looking at; unchanged for local, preserving every
        # existing local caller's behavior): "allow" locally, "redact" for a
        # remote target - UNLESS the operator already blocked clipboard
        # reads entirely via `read_only` (`_READ_ONLY_BLOCKED` in
        # `DesktopTool`), in which case this policy is moot.
        clipboard_policy_cfg = cfg.get("clipboard_read_policy")
        if clipboard_policy_cfg is None:
            self._clipboard_read_policy = "redact" if self._is_remote else "allow"
        else:
            self._clipboard_read_policy = str(clipboard_policy_cfg)
            if self._clipboard_read_policy not in {"allow", "redact", "block"}:
                raise ValueError(
                    "config 'clipboard_read_policy' must be one of "
                    f"'allow'/'redact'/'block', got {clipboard_policy_cfg!r}"
                )

        # -- human/agent coexistence (docs/designs/coexistence.md) -----------
        # Built by `mount()` (`_build_coexistence_guard`), never constructed
        # here directly - it needs the concrete backend instance, which does
        # not exist yet at `__init__` time (`ComputerTool.__init__` receives
        # an already-constructed `backend`, so in practice this only matters
        # for readability: the guard is assigned right after construction).
        # `None` on any platform where a proven per-platform GUARD band does
        # not (yet) exist - see `presence.GUARD_MEASURED` - so this feature
        # never claims detection it cannot back with evidence.
        self._coexistence_guard: CoexistenceGuard | None = None
        # The session-start disclosure channel `_ensure_announced` builds for
        # this backend (`_build_announcement`) on this session's FIRST real
        # action - an overlay object to keep alive for the tool's lifetime,
        # or `None` for a one-shot channel (macOS's dialog) or a backend with
        # no channel at all. Held here (not just a local) so it is not
        # garbage-collected out from under its own background poll thread.
        self._announcement: Any | None = None
        # -- session-start disclosure: gated at FIRST REAL USE, not mount() --
        # See `_ensure_announced` for the full rationale (docs/designs/
        # coexistence.md \u00a77, and the double-mount defect this closes: a
        # protocol-compliance probe calls mount() on a throwaway
        # MockCoordinator before every real session - see amplifier_core's
        # validation.tool.ToolValidator._check_protocol_compliance). This
        # lock is per-INSTANCE (not the module-level `_announcement_lock`,
        # which guards the cross-instance/cross-process channel cache) - it
        # serializes this instance's own check-then-act sequence against two
        # actions racing to be "first".
        self._announce_lock: threading.Lock = threading.Lock()
        self._announced: bool = False
        # Sticky for the life of this instance (this mount, this session):
        # once set, every later call to `_ensure_announced` - from any
        # thread, for `computer` OR `desktop` - re-raises this SAME error
        # immediately, without touching the backend again. This is what
        # makes a refusal mean "stop driving" structurally: `_ensure_announced`
        # is the one gate both tools' `execute()` call before doing anything
        # else, so there is no second door a refused session can get through.
        self._announce_refused: AnnouncementRefused | None = None
        # Remote-only (docs/designs/coexistence.md \u00a78.1/\u00a79.1): the target's
        # own persistent overlay is invisible to this controller until asked
        # (see `_sync_remote_announcement_state`) - nothing observes the
        # target's real input events directly. Edge-triggered, not level:
        # each flips true at most once per session, so a human clicking
        # Pause/Cancel is applied to this session's guard exactly once, not
        # once per guarded write for the remainder of the session.
        self._remote_pause_seen: bool = False
        self._remote_cancel_seen: bool = False
        # Defect 1 (halt surfacing): every `HaltedError` this session hits is
        # recorded here (`execute()`), and `hook-computer-use` reads this
        # list on every `tool:post` to inject a standing reminder into the
        # model's own context - a halted session must not be able to close
        # out a turn without the fact in front of it. Never cleared during a
        # session; a session in which a halt fired stays flagged for the
        # rest of that session, on purpose (see hook module docstring).
        self.halt_notices: list[dict[str, Any]] = []
        # Cached once so the hot path (`_run`, the `type` action) never pays
        # an `inspect.signature` call per keystroke - only backends that
        # accept `type_text(text, guard=...)` (Linux X11 today) get
        # per-keystroke intra-op detection; others fall back to plain
        # `type_text(text)`, unaffected.
        self._backend_type_text_supports_guard: bool = (
            "guard" in inspect.signature(backend.type_text).parameters
        )

        # -- type_text pacing (measured safety gap, see type_pacing.py) ------
        # `None` (default) = auto: `type_pacing.AUTO_PACING_MS` when a
        # coexistence guard is active for this `type` call, `0` (full speed,
        # unchanged) when it is not - see `resolve_type_pacing_ms`. An
        # explicit integer overrides auto in both directions, including `0`
        # to force full speed even with a guard active (logged once at
        # WARNING when it fires - see `_run`'s `type` action).
        pacing_cfg = cfg.get("type_pacing_ms")
        if pacing_cfg is None:
            self._type_pacing_ms: int | None = None
        else:
            try:
                parsed_pacing = int(pacing_cfg)
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    "config 'type_pacing_ms' must be an integer number of "
                    f"milliseconds, or omitted for auto - got {pacing_cfg!r}"
                ) from exc
            if parsed_pacing < 0:
                raise ValueError(
                    f"config 'type_pacing_ms' must be >= 0 - got {parsed_pacing!r}"
                )
            self._type_pacing_ms = parsed_pacing

    # -- display resolution (D2) -------------------------------------------------
    def resolve_display(self, refresh: bool = False) -> Display:
        """Resolve and cache display geometry for the current target monitor.

        Called once by `mount()`, right after the backend is selected. The other
        callers are the `screen_info` action, which passes `refresh=True` as its
        explicit, deliberate refresh path (e.g. after a resolution change), and
        `select_monitor()`, which re-resolves for a *different* target. Those are
        the only places this ever talks to the backend again after mount.
        """
        if self._display is not None and not refresh:
            return self._display
        self._display = self._resolve_display_for_target(
            self._target_monitor, allow_fallback=not self._target_monitor_explicit
        )
        return self._display

    def _resolve_display_for_target(
        self, target: str, allow_fallback: bool = False
    ) -> Display:
        """Build a `Display` scoped to `target` and record which monitor (if any)
        is now active in `self._current_monitor`.

        Single shared implementation for both `resolve_display()` (mount time and
        the `screen_info` refresh path) and `select_monitor()` (runtime target
        switch) - one place computes monitor-scoped geometry, so there is no
        separate "switch monitor" code path that could drift out of sync with how
        mount-time resolution works.

        `allow_fallback` governs exactly one failure mode: monitor enumeration
        being genuinely unavailable (`Backend.list_monitors()` raising
        `BackendError` - no RandR, no RandR Monitor objects, etc.). Verified for
        real on a headless/virtual single-display X11 session during
        development (RandR present, `GetMonitors` returns zero - a real,
        legitimate configuration, not a hypothetical): that machine has exactly
        one display, so falling back to `screen_geometry()`'s virtual-desktop
        bounding box - the code path that has ALWAYS been correct for a single
        display - reports the SAME rectangle enumeration would have, had it
        worked. That is not "pretending there is one monitor" (no `MonitorInfo`
        is invented); it is correctly falling back to the one mode that was
        already right for this machine, loudly logged so it stays diagnosable.
        This is why `allow_fallback` is only ever `True` when `target` is the
        unconfigured default (`resolve_display()` with no explicit
        `target_monitor` config) - an explicit ask (`target_monitor` config, or
        a runtime `select_monitor()` call) always fails loud instead, and a
        target id that IS enumerated but does not match a request always fails
        loud regardless of `allow_fallback` (see `select_monitor()` call below):
        a config typo must never silently degrade to a different region of the
        real, multi-monitor desktop it was supposed to protect against.
        """
        if target == VIRTUAL_DESKTOP:
            geo = self._backend.screen_geometry()
            self._current_monitor = None
            origin_x, origin_y = geo.origin_x, geo.origin_y
            width, height = geo.width, geo.height
        else:
            try:
                monitors = self._backend.list_monitors()
            except BackendError as exc:
                if not allow_fallback:
                    raise
                logger.warning(
                    "computer-use: monitor enumeration unavailable for target "
                    "%r (%s); falling back to whole-desktop bounding-box mode "
                    "for this session. Expected on a headless/virtual "
                    "single-display X11 session with no RandR monitor objects "
                    "- on a REAL multi-monitor desktop this is worth "
                    "investigating rather than trusting the fallback.",
                    target,
                    exc,
                )
                return self._resolve_display_for_target(VIRTUAL_DESKTOP)
            self._monitors = monitors
            # `select_monitor` (the module-level function) always fails loud on
            # an unmatched explicit id - deliberately NOT gated by
            # allow_fallback. Enumeration succeeding but not containing the
            # requested id is a config typo, not an environmental limitation;
            # silently substituting a different monitor would be exactly the
            # "silently targets the wrong region" failure mode this feature
            # exists to eliminate.
            chosen = select_monitor(monitors, None if target == PRIMARY else target)
            if target == PRIMARY and not chosen.primary:
                # Not a synthesized fallback - `chosen` is a real, enumerated
                # monitor. Logged because picking one among several equally
                # real candidates, absent a primary flag, is an environmental
                # fact worth surfacing, not something to bury silently.
                logger.warning(
                    "computer-use: no monitor reported as primary; "
                    "deterministically targeting %r (%dx%d at %d,%d)",
                    chosen.id,
                    chosen.width,
                    chosen.height,
                    chosen.x,
                    chosen.y,
                )
            self._current_monitor = chosen
            origin_x, origin_y = chosen.x, chosen.y
            width, height = chosen.width, chosen.height

        mw, mh = compute_display(width, height, self._max_edge, self._max_pixels)
        disp = Display(width, height, mw, mh, origin_x, origin_y)
        logger.info(
            "computer-use display: target=%r screen %dx%d at (%d,%d) -> model %dx%d",
            target,
            width,
            height,
            origin_x,
            origin_y,
            mw,
            mh,
        )
        return disp

    def list_monitors(self) -> list[MonitorInfo]:
        """Enumerate monitors via the backend, refreshing the cached list.

        Independent of the current target: works the same whether `display` is
        currently scoped to a monitor or to the whole virtual desktop.
        """
        self._monitors = self._backend.list_monitors()
        return self._monitors

    def _monitors_for_attribution(self) -> list[MonitorInfo]:
        """Best-effort monitor list for `monitors.attribute_monitor`, used by
        the `list_windows`/`focus_window` actions below.

        Prefers a fresh `list_monitors()` call (a window can move between
        monitors between calls) but falls back to whatever was last resolved
        (`self._monitors`, set by `mount()`/`select_monitor()`/an earlier
        `list_monitors()` call) if enumeration fails right now - a transient
        enumeration failure degrades attribution to "unknown" for each window
        (`attribute_monitor` already returns `None` on an empty list), it
        does not make `list_windows`/`focus_window` themselves fail.
        """
        try:
            return self._backend.list_monitors()
        except BackendError:
            return self._monitors

    def _focus_monitor_warning(self, handle: str) -> str:
        """After `focus_window`, tell the caller - explicitly, in the result
        text - if the window it just raised is on a DIFFERENT monitor than
        the one `computer` currently captures.

        This closes the exact gap that made a working `focus_window` look
        broken for three sessions in a row: `focus_window` can succeed
        completely (the foreground window genuinely changes) while the next
        screenshot shows no difference, because capture is scoped to one
        monitor at a time (see `monitors.py`) and the window landed on a
        different one. Without this, "focus succeeded but nothing visibly
        changed" and "focus silently did nothing" are indistinguishable from
        the caller's side - which is precisely what happened.

        Deliberately a WARNING, never an automatic `select_monitor` switch:
        changing the capture target is a separate, already-explicit action
        (`desktop.select_monitor`) - silently doing it here would mutate
        session state the caller never asked to change, on the strength of
        one `focus_window` call. That mirrors this same method's cursor-clamp
        warning a few lines up (`_run`'s `cursor_position` branch): inform,
        never silently substitute.

        Returns `""` (no note) when: this session is in virtual-desktop mode
        (`self._current_monitor is None` - capture already shows the whole
        desktop, so there is nothing to warn about); the window landed on the
        SAME monitor `computer` is already scoped to; or fresh window
        enumeration itself fails (cannot verify either way - say nothing
        false rather than fabricate a warning).
        """
        if self._current_monitor is None:
            return ""
        try:
            result = self._backend.list_windows()
        except BackendError:
            return ""
        entry = next((w for w in result.windows if w.handle == handle), None)
        if entry is None or entry.rect is None:
            return (
                " [warning: could not verify which monitor this window is "
                "on now - this backend does not report window geometry for "
                "it; take a screenshot to confirm the focus actually landed "
                "where expected]"
            )
        target = self._current_monitor.id
        landed = attribute_monitor(entry.rect, self._monitors_for_attribution())
        if landed == target:
            return ""
        if landed is None:
            return (
                " [warning: window is not within any enumerated monitor "
                f"(possibly off-screen or minimized) - `computer` is scoped "
                f"to {target!r} and will not show it; take a screenshot to "
                "confirm]"
            )
        return (
            f" [warning: window is now on monitor {landed!r}, but `computer` "
            f"screenshots are scoped to {target!r} - it will not appear "
            f"there; use desktop.select_monitor({landed!r}) to see it]"
        )

    @property
    def current_monitor(self) -> MonitorInfo | None:
        """The monitor `display` is scoped to, or `None` in virtual-desktop mode."""
        return self._current_monitor

    def select_monitor(self, target: str) -> Display:
        """Switch the active target (a monitor id, `"primary"`, or
        `monitors.VIRTUAL_DESKTOP`) and re-resolve `display` for it.

        Safe to call mid-session. `hook-computer-use` reads `native_tool_spec`
        fresh on *every* provider request (see that property's docstring) - it is
        never cached at the hook layer - so the very next request after this call
        returns automatically declares the new `display_width_px`/
        `display_height_px` with no extra plumbing. The only state this needs to
        update is `self._display`/`self._current_monitor`, exactly what
        `_resolve_display_for_target` already does.

        Always fails loud on enumeration failure (`allow_fallback=False`):
        unlike the unconfigured default, this is an explicit ask - the caller
        (config or a live `desktop.select_monitor` call) is entitled to know it
        failed, not have it silently ignored in favor of the previous target.
        """
        self._display = self._resolve_display_for_target(target, allow_fallback=False)
        self._target_monitor = target
        self._target_monitor_explicit = True
        return self._display

    @property
    def display(self) -> Display:
        if self._display is None:
            # Should never happen in normal operation - mount() resolves eagerly -
            # but if it does, fail loudly rather than silently blocking the hot path
            # on a subprocess the way the old `native_tool_spec` property did.
            raise BackendError(
                "display geometry not resolved; resolve_display() must be called at mount time"
            )
        return self._display

    @property
    def image_space(self) -> ImageSpace | None:
        """The coordinate space a tool-call payload's numbers are relative to:
        the size of the screenshot the model was actually shown.

        `None`, rather than raising like `display`, when geometry was never
        resolved - and that difference is deliberate. This is read at the very
        top of `execute()`, BEFORE its error handling; raising here would turn
        every unmounted-display tool call into an exception escaping `execute()`
        instead of the clean `ToolResult` error it returns today. A dialect that
        needs the size and is handed `None` raises `ValueError` naming what is
        missing, which `execute()` already converts into an ordinary tool error.
        Loud, in the right place, without moving the failure outside the handler.
        """
        return None if self._display is None else self._display.image_space

    # -- Tool protocol ----------------------------------------------------------
    @property
    def name(self) -> str:
        return "computer"

    @property
    def description(self) -> str:
        return (
            "Control the user's real desktop: capture the screen, move and click the "
            "mouse, drag, scroll, type text, press key combinations, and list or focus windows. "
            "Coordinates are in the pixel space of the screenshots returned by this tool. "
            "Always take a screenshot before acting so you can see where things are. "
            "This machine may be in use by a human at the same time you are driving it: "
            "your keystrokes and theirs can interleave, so a command you believe you typed "
            "verbatim may land with extra or missing characters. If a result looks off "
            "(an unexpected error, a typo, output that doesn't match), verify what actually "
            "landed before assuming your own input was wrong."
        )

    @property
    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ACTIONS,
                    "description": "Operation to perform.",
                },
                "coordinate": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "description": "[x, y] target. For zoom: a 4-element region [x1, y1, x2, y2].",
                },
                "region": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "description": "Alias for a zoom region: [x1, y1, x2, y2].",
                },
                "start_coordinate": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "description": "[x, y] drag origin for left_click_drag.",
                },
                "text": {
                    "type": "string",
                    "description": "Text to type, or key combo such as 'ctrl+s'.",
                },
                "scroll_direction": {
                    "type": "string",
                    "enum": ["up", "down", "left", "right"],
                },
                "scroll_amount": {
                    "type": "integer",
                    "description": "Wheel notches to scroll.",
                },
                "duration": {
                    "type": "number",
                    "description": "Seconds, for wait and hold_key.",
                },
                "handle": {
                    "type": "string",
                    "description": "Window handle from list_windows.",
                },
            },
            "required": ["action"],
        }

    # -- native promotion (read by hook-computer-use) ---------------------------
    @property
    def native_tool_type(self) -> str:
        """The native tool type this tool has resolved for THIS turn - the key
        `providers.dialect_for_tool_type` dispatches on.

        Exists because `hook-computer-use` needs exactly this fact and had no
        honest way to get it. It was reading `native_tool_spec["type"]`, i.e.
        recovering a vendor-neutral fact by parsing a VENDOR-SHAPED artifact.
        That works only for vendors that happen to put their type under a key
        named `type`; a vendor that discriminates by some other key has no
        `type` at all, and the hook silently fell back to a default belonging to
        a different vendor and probed the mounted provider for the wrong wire
        convention entirely.

        The fix is not to teach the hook more wire formats - it declares
        `dependencies = []` and cannot import `providers.py`, by design. It is
        to stop making it infer: the tool already knows the answer, so it says
        it. Plain `str`, no import, no coupling - the same duck-typed read the
        hook already does for `native_tool_spec`.

        For both dialects that put their type on the wire this is exactly
        `native_tool_spec["type"]`, so the hook's answer for them is unchanged -
        pinned in `tests/test_provider_dialects.py`.
        """
        return self._tool_version

    @property
    def native_tool_spec(self) -> dict[str, Any]:
        """The native tool declaration for whichever dialect `_tool_version`
        belongs to (`providers.py`) - sized to the cached display when that
        dialect requires a size, bare when it rejects one.

        This used to build ONE shape (Anthropic's: `name` +
        `display_width_px`/`display_height_px`) no matter which provider was in
        play, and gate `enable_zoom` on `self._tool_version >=
        "computer_20251124"` - a string comparison that silently did double
        duty as a provider check, because OpenAI's bare `"computer"` happens to
        sort below it. OpenAI tolerated the surplus fields only because
        `provider-openai` discards everything but `type`; the declaration
        itself was wrong for that wire and nothing here said so. Each dialect
        now owns its own shape (see `providers._declare_anthropic` /
        `_declare_openai`).

        D2 fix: this used to call `self._bridge.display()`, a property that shelled
        out to PowerShell with a 30s timeout on *every single* provider request. That
        subprocess call is why the hook's `hasattr` guard (D3) mattered so much: any
        transient bridge failure here raised on the hot path. Display is now resolved
        once at mount and cached in-memory; this property does no I/O and cannot
        raise for that reason again.

        Per-monitor targeting tension: `desktop.select_monitor` can change what
        `self.display` reports *mid-session*, and this property's declared
        `display_width_px`/`display_height_px` must never drift out of sync with
        what `computer.screenshot` is actually capturing. That is why this stays a
        plain property reading `self.display` rather than something cached at
        construction: the orchestrator's `ToolSpec` construction (see
        `amplifier-module-loop-streaming`'s `_build_tool_spec`, which reads this
        property fresh to build every request's tool list - it is never cached
        there either; only D2's *I/O* was cached here, not the *value*) re-reads
        `native_tool_spec` on every `provider:request`, so the very next request
        after a monitor switch automatically declares the new dimensions. The
        in-memory state this property reads (`self._display`) is exactly what
        `select_monitor` updates - one piece of state, two readers (this property
        and `_run`), always in sync.
        """
        disp = self.display
        return dialect_for_tool_type(self._tool_version).declare(
            self._tool_version,
            width=disp.model_width,
            height=disp.model_height,
            enable_zoom=self._enable_zoom,
        )

    @property
    def native_beta_header(self) -> str | None:
        """`None` when the vendor owning this tool type has no beta-header
        mechanism at all - a distinct answer from any header string, and one
        this used to be unable to give (see `tool_versions.beta_header_for`).
        A caller must send no header for `None`, never an empty one."""
        return beta_header_for(self._tool_version)

    def note_model(self, model: str | None) -> None:
        """Re-resolve `tool_version` for the model about to receive THIS request.

        Called by hook-computer-use's wrapped `provider.complete()`
        (`_note_model_on_computer_tool`, `hook-computer-use/__init__.py`) with
        `request.model` on every request, before forwarding the request to the
        real provider - see `tool_versions.py` module docstring. This corrects
        `_tool_version` for the *next* time `native_tool_spec` is read (that
        property is read earlier in the same turn, by the orchestrator's own
        `ToolSpec` construction, before `provider.complete()` is ever called -
        so a correction here lands one turn ahead of the read it protects, not
        retroactively inside the same turn). Never raises: a mid-session
        exception here would take down the whole request, the exact class of
        bug D3 already fixed once for `native_tool_spec` itself.
        """
        resolved, corrected = resolve_tool_version(
            model, self._configured_tool_version, previous=self._tool_version
        )
        if corrected:
            logger.warning(
                "computer-use: model %r requires tool_version %r; correcting "
                "from %r to avoid the API rejecting every request with this "
                "pairing (see tool_versions.py)",
                model,
                resolved,
                self._tool_version,
            )
        self._tool_version = resolved

    # -- coexistence guard wiring for every mutating action ----------------------
    @contextmanager
    def _guard_write(self, *, coord: tuple[int, int] | None = None):
        """Wrap one mutating action in the coexistence guard's before/after
        discipline (`docs/designs/coexistence.md` \u00a75.2/\u00a78.6), extended in
        this pass from `type_text` (the only action guarded before) to every
        action in `MUTATING`.

        Checked ONCE, around the whole action - not once per constituent
        click/motion inside a composite - matching \u00a78.4's "complete the
        composite (\u2264~200ms), then honour the pause" rule: `double_click`/
        `triple_click` (multiple clicks), `left_click_drag` (down+move+up),
        and `scroll` (N wheel notches) are each faster than the OS's own
        double-click timing window, so interrupting between their
        constituent events would silently convert a double_click into a
        single click - exactly the failure \u00a78.4 warns against. A human
        detected mid-composite is instead caught at the very next action's
        guard check, bounded by the same ~200ms this design already accepts
        as the pause-latency cost of an atomic composite (\u00a712). `type`
        keeps its own separate, finer-grained per-keystroke wiring
        (`backend.type_text(..., guard=guard)`) precisely because a
        multi-hundred-character string is NOT a tightly-timed composite -
        the two cases are handled differently on purpose, not by oversight.

        A no-op context (nothing enforced, identical to every action's
        behavior before this pass) when no guard exists for this backend/
        platform (`self._coexistence_guard is None` - e.g. Windows, or any
        platform with coexistence explicitly disabled).
        """
        guard = self._coexistence_guard
        if guard is None:
            yield
            return
        if self._is_remote and self._announcement is not None:
            self._sync_remote_announcement_state(guard)
        guard.check_start_permission()
        guard.bind_target()
        guard.before_event(coord=coord)
        try:
            yield
        finally:
            guard.after_event()
            guard.release_target()

    def _sync_remote_announcement_state(self, guard: CoexistenceGuard) -> None:
        """Pull the target-side overlay's Pause/Cancel state
        (docs/designs/coexistence.md \u00a78.1/\u00a79.1) into THIS session's guard
        before every guarded write. The overlay lives entirely on the
        target (only that process can draw on that desktop, or observe a
        real click there) - a click is otherwise invisible to this
        controller until asked. Piggybacked on the exact cadence
        `before_event()`'s own presence sample already uses for a remote
        backend (\u00a75.2/\u00a75.7) rather than a second background thread or a
        parallel polling channel - the same once-per-guarded-write wire
        round trip discipline `presence_idle` already established, applied
        to a second fact instead of a new mechanism.

        Reuses `_on_overlay_pause`/`_on_overlay_cancel` verbatim - the same
        functions a LOCAL overlay's own click callback already calls
        in-process - so a remote pause/cancel is handled identically to a
        local one from this point forward (guard.pause.set(...) /
        durable-halt-record + release_all). Edge-triggered via
        `_remote_pause_seen`/`_remote_cancel_seen`: each fires at most once
        per session, since the target's own flags are latched
        (level-triggered, never cleared - see `RemoteAgent
        ._op_announcement_status`) and calling `_on_overlay_cancel` twice
        would write a redundant durable halt record for no benefit.

        Best-effort: a read failure here must never block the write path
        this guard already protects - \u00a76.0's halt invariant and this
        session's own live presence sample apply regardless of whether this
        secondary signal could be read this time.
        """
        status_fn = getattr(self._backend, "announcement_status", None)
        if status_fn is None:
            return
        try:
            status = status_fn()
        except BackendError as exc:
            logger.warning(
                "coexistence: remote announcement_status read failed "
                "(backend=%r): %s - a human's Pause/Cancel click on the "
                "remote overlay would be invisible to this controller until "
                "the next successful read",
                self._backend.name,
                exc,
            )
            return
        if status.get("cancelled") and not self._remote_cancel_seen:
            self._remote_cancel_seen = True
            _on_overlay_cancel(guard, self._backend.name)
            return
        if status.get("paused") and not self._remote_pause_seen:
            self._remote_pause_seen = True
            _on_overlay_pause(guard, self._backend.name)

    # -- session-start disclosure: fired at first real use, not mount() ---------
    def _ensure_announced(self) -> None:
        """Fire the session-start disclosure (docs/designs/coexistence.md \u00a77) on
        THIS session's first real action, and never before.

        Why not `mount()` (the defect this closes): `amplifier_core`'s loader
        calls every tool module's `mount()` TWICE per real session - once as a
        throwaway protocol-compliance probe
        (`amplifier_core.validation.tool.ToolValidator._check_protocol_compliance`,
        against a fresh `MockCoordinator` whose `mount()` result is discarded
        and torn down in a `finally` block a few lines later - see that
        module's source), and once for real
        (`loader.mount_with_config_ep`/`mount_with_config_direct_ep`). Both
        calls run the module's real `mount()` function with the real config.
        Building the disclosure inside `mount()` meant the FIRST dialog a
        human ever answered could be for the discarded probe - and any
        consent given applied to a `ComputerTool`/`CoexistenceGuard` pair
        about to be thrown away, not the one actually about to drive
        anything. Gating here instead means a validation probe - which never
        calls `execute()` on the tool it mounted, only `mount()` itself -
        cannot trigger this at all: there is no code path from
        `_check_protocol_compliance` to this method.

        What counts as "first use": ANY action on `computer` or `desktop`,
        not just a mutating one. Both classes' `execute()` call this before
        doing anything else (`ComputerTool.execute` directly;
        `DesktopTool.execute` via `self._computer._ensure_announced()`), so a
        pure read - `screenshot`, `zoom`, `list_windows`, `get_clipboard` - is
        gated identically to a click or a keystroke. A screenshot IS a
        capture of a human's screen; gating only writes would let an agent
        silently see everything on the target's display before ever
        disclosing that it was watching, which defeats the entire purpose of
        a session-start disclosure. There is exactly one gate, not one gate
        for writes and a silent hole for reads.

        Ordering: because this runs synchronously (via `asyncio.to_thread`,
        same as `_run` itself) as the very first statement of `execute()`,
        the announcement fully COMPLETES - dialog answered, overlay actually
        shown, or the channel's own failure policy resolved - before any
        backend call for that action begins. Not concurrent with the first
        action, not merely started before it: this call returns (or raises)
        before `execute()`'s own dispatch logic ever runs.

        Concurrency: idempotent and thread-safe. Two actions issued by the
        model in the same turn can race to be "first" on two different
        worker threads. `self._announce_lock` is a per-instance lock
        (distinct from the module-level `_announcement_lock`, which guards
        the cross-instance/cross-process channel cache in
        `_build_announcement`) - double-checked so the actual
        dialog/overlay/RPC call, which can genuinely block (a countdown
        timer, an SSH round trip), only ever runs once per instance; every
        other caller either returns immediately (already announced) or
        blocks briefly on the lock and then reuses whatever the winner
        decided.

        Refusal is STICKY for the life of this instance (this mount, this
        session): once `self._announce_refused` is set, every later call -
        from this thread or any other, for `computer` OR `desktop` -
        re-raises the SAME `AnnouncementRefused` immediately, without
        touching the backend again. Combined with there being exactly one
        gate both tools' `execute()` methods call, this makes "refused" mean
        "stop driving" structurally, not by convention: no action from
        either tool can reach the backend without passing through here
        first, and once refused this method never again returns normally.
        """
        if self._announce_refused is not None:
            raise self._announce_refused
        if self._announced:
            return
        with self._announce_lock:
            # Double-checked: another thread may have already announced (or
            # been refused) while this thread waited for the lock above.
            if self._announce_refused is not None:
                raise self._announce_refused
            if self._announced:
                return
            try:
                self._announcement = _build_announcement(
                    self._backend,
                    self._coexistence_guard,
                    self._cfg,
                    self.resolve_display(),
                )
            except AnnouncementRefused as exc:
                self._announce_refused = exc
                # Same best-effort, idempotent, refcounted cleanup mount()
                # used to perform on a mount-time refusal. Safe for a REMOTE
                # backend too - `close()` only decrements THIS handle's own
                # refcount (see shared_transport.py); it can never tear down
                # a connection a different, already-consented session is
                # using against the same target.
                try:
                    self._backend.close()
                except Exception:  # noqa: BLE001 - best-effort cleanup on refusal
                    logger.debug(
                        "tool-computer-use: backend.close() failed after a "
                        "first-use announcement refusal",
                        exc_info=True,
                    )
                raise
            self._announced = True

    # -- execution --------------------------------------------------------------
    def _run(self, action: str, params: dict[str, Any]) -> tuple[str, str | None]:
        """Run one Anthropic computer-tool action against `self._backend`.

        Returns (text_summary, base64_png_or_None). Mirrors the dispatch logic that
        used to live inside `WindowsBridge.execute` - now backend-agnostic: it only
        ever calls the `Backend` protocol, never a concrete backend's internals.
        """
        disp = self.display
        backend = self._backend

        def coord(key: str = "coordinate") -> tuple[int, int]:
            raw = params.get(key)
            if not isinstance(raw, (list, tuple)) or len(raw) < 2:
                raise ValueError(f"action {action!r} requires {key} as [x, y]")
            return disp.to_screen(float(raw[0]), float(raw[1]))

        text = params.get("text") or params.get("key")

        if action == "screenshot":
            # Scoped to the current monitor, not cropped from a full-desktop grab:
            # when a monitor is targeted, pass its bounds as an explicit region so
            # both backends capture that region directly (`Graphics.CopyFromScreen`
            # on Windows, X `GetImage` on Linux) - never the whole virtual desktop
            # downscaled and then implicitly "close enough". `None` here (only in
            # virtual-desktop mode) preserves the original whole-desktop capture.
            region = None
            if self._current_monitor is not None:
                m = self._current_monitor
                region = (m.x, m.y, m.x + m.width, m.y + m.height)
            b64 = capture_scaled_b64(
                backend, disp, region, self._max_edge, self._max_pixels
            )
            # \u00a73 audit hardening: a content hash for every capture, never the
            # pixels themselves in the log - the same digest-not-plaintext
            # discipline applied below to type/set_clipboard.
            logger.info(
                "computer-use audit: op=screenshot sha256=%s",
                _bytes_digest(base64.standard_b64decode(b64)),
            )
            return "screenshot captured", b64

        if action == "zoom":
            # Models reach for `region` about as often as `coordinate`, and sometimes
            # split it across start_coordinate/coordinate. Accept all three rather
            # than burning a turn on a schema correction.
            raw = params.get("coordinate") or params.get("region")
            if (not isinstance(raw, (list, tuple)) or len(raw) < 4) and params.get(
                "start_coordinate"
            ):
                s_, e_ = params["start_coordinate"], params.get("coordinate") or []
                if len(s_) >= 2 and len(e_) >= 2:
                    raw = [s_[0], s_[1], e_[0], e_[1]]
            if not isinstance(raw, (list, tuple)) or len(raw) < 4:
                raise ValueError(
                    "zoom requires a 4-element region: coordinate=[x1, y1, x2, y2]"
                )
            x1, y1 = disp.to_screen(raw[0], raw[1])
            x2, y2 = disp.to_screen(raw[2], raw[3])
            region = (x1, y1, max(x1 + 8, x2), max(y1 + 8, y2))
            b64 = capture_scaled_b64(
                backend, disp, region, self._max_edge, self._max_pixels
            )
            return f"zoomed to region {list(raw)}", b64

        if action == "cursor_position":
            sx, sy = backend.cursor_position()
            mx, my = disp.to_model(sx, sy)
            note = ""
            if self._current_monitor is not None:
                m = self._current_monitor
                if not (m.x <= sx < m.x + m.width and m.y <= sy < m.y + m.height):
                    # Honest, not synthetic: to_model() above already clamped
                    # (sx, sy) to the targeted monitor's edge because the real
                    # cursor is elsewhere. Say so rather than silently reporting
                    # a clamped position as if it were exact.
                    note = (
                        f" [warning: real cursor is outside targeted monitor "
                        f"{m.id!r} ({m.width}x{m.height} at {m.x},{m.y}); "
                        "position above is clamped to the nearest edge, not exact]"
                    )
            return f"cursor at [{mx}, {my}] (model space){note}", None

        if action == "screen_info":
            # The one deliberate refresh path (alongside select_monitor):
            # re-resolves and re-caches geometry for the CURRENT target, so a
            # resolution change is picked up without touching the hot path.
            fresh = self.resolve_display(refresh=True)
            payload: dict[str, Any] = {
                "screen_width": fresh.screen_width,
                "screen_height": fresh.screen_height,
                "model_width": fresh.model_width,
                "model_height": fresh.model_height,
                "origin_x": fresh.origin_x,
                "origin_y": fresh.origin_y,
                "target_monitor": self._target_monitor,
            }
            if self._current_monitor is not None:
                payload["monitor_id"] = self._current_monitor.id
                payload["monitor_primary"] = self._current_monitor.primary
            elif self._target_monitor != VIRTUAL_DESKTOP:
                # Degraded: a per-monitor target was requested but enumeration
                # was unavailable, so geometry is the whole virtual-desktop
                # bounding box. A logger.warning alone is not enough - the model
                # is the one reasoning about this coordinate space, so it has to
                # be told. On a multi-monitor desktop the bounding box can span
                # large regions where no display exists at all, and clicks there
                # land nowhere.
                payload["degraded"] = "monitor-enumeration-unavailable"
                payload["coordinate_space"] = "virtual-desktop-bounding-box"
                payload["warning"] = (
                    "Per-monitor targeting is unavailable on this host, so these "
                    "coordinates span the whole virtual desktop. On a multi-monitor "
                    "setup this space may contain gaps with no display behind them; "
                    "clicks there do nothing."
                )
            return json.dumps(payload), None

        if action == "list_windows":
            result = backend.list_windows()
            monitors = self._monitors_for_attribution()
            visible = [w for w in result.windows if not w.minimized][:25]
            lines = []
            for w in visible:
                mon = attribute_monitor(w.rect, monitors)
                lines.append(f"  [{w.handle}] {w.title} (monitor={mon!r})")
            listing = "\n".join(lines)
            return f"visible windows (foreground={result.foreground}):\n{listing}", None

        if action == "focus_window":
            handle = params.get("handle")
            if not handle:
                raise ValueError("action 'focus_window' requires 'handle'")
            with self._guard_write():
                backend.focus_window(str(handle))
            note = self._focus_monitor_warning(str(handle))
            return f"focused window {handle}{note}", None

        if action in _CLICK_ACTIONS:
            button, count = _CLICK_ACTIONS[action]
            x, y = coord() if params.get("coordinate") is not None else (None, None)
            with self._guard_write(
                coord=(x, y) if x is not None and y is not None else None
            ):
                backend.click(x, y, button=button, count=count)
            where = (
                f" at {params.get('coordinate')}" if params.get("coordinate") else ""
            )
            logger.info("computer-use audit: op=%s%s", action, where)
            return f"{action}{where}", None

        if action == "mouse_move":
            x, y = coord()
            with self._guard_write(coord=(x, y)):
                backend.move(x, y)
            return f"mouse_move at {params.get('coordinate')}", None

        if action == "left_mouse_down":
            x, y = coord() if params.get("coordinate") is not None else (None, None)
            with self._guard_write(
                coord=(x, y) if x is not None and y is not None else None
            ):
                backend.mouse_down(x, y, "left")
            return "left_mouse_down", None

        if action == "left_mouse_up":
            x, y = coord() if params.get("coordinate") is not None else (None, None)
            with self._guard_write(
                coord=(x, y) if x is not None and y is not None else None
            ):
                backend.mouse_up(x, y, "left")
            return "left_mouse_up", None

        if action == "left_click_drag":
            start = (
                coord("start_coordinate") if params.get("start_coordinate") else None
            )
            end = coord()
            # Guarded once around the whole drag (down+move+up), per
            # `_guard_write`'s docstring - a drag is exactly the kind of
            # tightly-timed composite \u00a78.4 says must complete, not tear.
            # `coord=end` (not `start`): the exclusion-zone check (\u00a77.5)
            # cares about where the drag's synthetic input actually lands.
            with self._guard_write(coord=end):
                backend.drag(start, end)
            logger.info(
                "computer-use audit: op=left_click_drag start=%s end=%s", start, end
            )
            return f"dragged to {params.get('coordinate')}", None

        if action == "scroll":
            x, y = coord() if params.get("coordinate") is not None else (None, None)
            direction = params.get("scroll_direction") or params.get("direction")
            if not direction:
                raise ValueError("action 'scroll' requires 'scroll_direction'")
            amount = int(params.get("scroll_amount") or params.get("amount") or 3)
            with self._guard_write(
                coord=(x, y) if x is not None and y is not None else None
            ):
                backend.scroll(x, y, str(direction), amount)
            return f"scrolled {direction} x{amount}", None

        if action in {"key", "hold_key"}:
            if not text:
                raise ValueError(f"action {action!r} requires 'text'")
            with self._guard_write():
                if action == "key":
                    backend.key(str(text))
                else:
                    backend.hold_key(str(text), float(params.get("duration") or 1.0))
            # Combos (e.g. "ctrl+s") are short, symbolic, and not free-form
            # secret text the way typed prose or a clipboard payload can be -
            # logged directly, unlike \u00a73's digest-not-plaintext rule for
            # `type`/`set_clipboard` below.
            logger.info("computer-use audit: op=%s combo=%s", action, text)
            return f"pressed {text}", None

        if action == "type":
            if not text:
                raise ValueError("action 'type' requires 'text'")
            body = str(text)
            guard = self._coexistence_guard
            guard_active = guard is not None and self._backend_type_text_supports_guard
            # Measured safety gap (type_pacing.py): a 202-character string
            # typed at full speed via a per-character guarded loop can
            # complete in ~70ms - an inter-character gap far narrower than
            # any platform's GUARD_MS, making the presence detector
            # structurally blind for the whole operation. Pacing is applied
            # HERE, in the one shared call site every backend routes
            # through, so Linux and macOS both benefit from a single fix
            # rather than a per-backend patch - and only when a guard is
            # actually active, per `resolve_type_pacing_ms`'s contract.
            pacing_ms = resolve_type_pacing_ms(
                self._type_pacing_ms, guard_active=guard_active
            )
            if guard_active and self._type_pacing_ms == 0:
                assert guard is not None
                logger.warning(
                    "computer-use: type_pacing_ms=0 explicitly configured "
                    "while a coexistence guard is active (backend=%r, "
                    "guard_ms=%.1f) - this disables the pacing that keeps "
                    "the inter-character gap wider than the guard band, "
                    "making the presence detector structurally blind for "
                    "the duration of this type_text call "
                    "(docs/designs/coexistence.md \u00a75.2). A deliberate, "
                    "logged choice, not a default.",
                    backend.name,
                    guard.presence.guard_ms,
                )
            if guard_active:
                assert guard is not None
                if self._is_remote and self._announcement is not None:
                    self._sync_remote_announcement_state(guard)
                # \u00a75.2/\u00a78.6: bind the delivery target once at operation
                # start; `backend.type_text` re-checks it (via the guard)
                # before EVERY keystroke, and records a fresh injection
                # timestamp after each one - this is what lets a human
                # keystroke landing mid-`type_text` be detected (O5), not
                # just between whole operations.
                guard.check_start_permission()
                guard.bind_target()
                try:
                    if pacing_ms > 0:
                        pacing_seconds = pacing_ms / 1000.0
                        for ch in body:
                            backend.type_text(ch, guard=guard)
                            time.sleep(pacing_seconds)
                    else:
                        backend.type_text(body, guard=guard)
                finally:
                    guard.release_target()
            else:
                backend.type_text(body)
            # §3 audit hardening: a digest, never the plaintext - typed
            # content frequently includes credentials (the same rationale
            # `docs/designs/remote-transport.md` §10 already gives for
            # `type_text`'s `args_digest`; this is where that discipline is
            # actually implemented, and where `set_clipboard` below matches it).
            logger.info(
                "computer-use audit: op=type chars=%d sha256=%s",
                len(body),
                _text_digest(body),
            )
            return f"typed {len(body)} characters", None

        if action == "wait":
            duration = float(params.get("duration", 1.0))
            time.sleep(duration)
            return f"waited {duration}s", None

        raise ValueError(f"unsupported action {action!r}")

    async def execute(self, input: dict[str, Any]) -> ToolResult:
        """Run one tool call, in whichever provider dialect it arrived in.

        The two live wire forms disagree about shape and cardinality -
        Anthropic sends one `{"action": ..., "coordinate": [...]}` per call;
        OpenAI batches N `{"type": ..., "x": ..., ...}` entries under `actions`
        and expects ONE result for the whole batch (`providers.py` has the full
        comparison). `providers.read_call` is the only place that knows the
        difference: it identifies the dialect from the payload's actual shape -
        never by asking which provider is mounted - and yields this tool's own
        `(action, params)` vocabulary. Everything after that line is shared, and
        was already shared before this seam existed: one `ACTIONS` check, one
        `read_only`/`MUTATING` gate, one `_run()`, one set of error handlers.
        There is no second, parallel dispatcher per vendor and there must never
        be one.

        Cardinality is not special-cased either: a single Anthropic action is a
        one-element batch, and the loop below is identical for both. The one
        genuine per-dialect difference is what a result must carry -
        `result_must_carry_screenshot`.

        The very first thing this does - before parsing the call, before
        anything else - is `_ensure_announced()` (see that method): the
        session-start disclosure gate, moved here from `mount()` so a
        throwaway protocol-compliance probe can never trigger it.
        """
        try:
            await asyncio.to_thread(self._ensure_announced)
        except AnnouncementRefused as exc:
            return ToolResult(
                success=False,
                error={"message": str(exc), "type": "AnnouncementRefused"},
            )
        dialect, calls = read_call(input, self.image_space)

        last_summary = ""
        last_image_b64: str | None = None
        try:
            # The iterable may be lazy and may raise while being pulled (see
            # `providers._normalize_openai_batch`), so iteration happens INSIDE
            # this handler: a malformed entry halfway through a batch fails
            # after the good actions before it have already run, exactly as the
            # per-item loop did before. Every ValueError raised by `_run` is
            # caught below and returned, so the only thing reaching this
            # handler is a dialect read failure.
            for action, params in calls:
                if action not in ACTIONS:
                    return ToolResult(
                        success=False,
                        error={
                            "message": f"unknown action {action!r}; expected one of {', '.join(ACTIONS)}"
                        },
                    )
                if self._read_only and action in MUTATING:
                    return ToolResult(
                        success=False,
                        error={
                            "message": f"action {action!r} blocked: computer-use is mounted read_only"
                        },
                    )
                try:
                    # C4: `Backend` stays synchronous (local paths and all existing
                    # tests untouched), but a remote action can block on a network
                    # round trip for hundreds of milliseconds. Running it in a
                    # thread keeps the event loop live so cancellation can actually
                    # be serviced during that wait, instead of stalling behind a
                    # screenshot transfer. Cheap locally too - `asyncio.to_thread`
                    # on a microsecond-scale X11/Quartz call costs a thread-pool
                    # round trip, not a network one.
                    summary, image_b64 = await asyncio.to_thread(
                        self._run, action, params
                    )
                except HaltedError as exc:
                    return self._record_halt_result(action, exc)
                except (BackendError, ValueError) as exc:
                    return ToolResult(
                        success=False,
                        error={"message": str(exc), "type": type(exc).__name__},
                    )
                except Exception as exc:
                    logger.exception("computer action %s failed", action)
                    return ToolResult(
                        success=False,
                        error={"message": str(exc), "type": type(exc).__name__},
                    )
                last_summary = summary
                if image_b64 is not None:
                    last_image_b64 = image_b64
        except ValueError as exc:
            return ToolResult(
                success=False, error={"message": str(exc), "type": "ValueError"}
            )

        if last_image_b64 is None:
            if not dialect.result_must_carry_screenshot:
                return ToolResult(success=True, output=last_summary)
            # OpenAI's `computer_call_output` is invalid without an image, so
            # take one more if the batch's own actions produced none - the
            # model always sees the result of what it just did.
            try:
                last_summary, last_image_b64 = await asyncio.to_thread(
                    self._run, "screenshot", {}
                )
            except (BackendError, ValueError) as exc:
                return ToolResult(
                    success=False,
                    error={"message": str(exc), "type": type(exc).__name__},
                )
        # `_run("screenshot", ...)` always returns image bytes (never None) -
        # see its own branch - so this is a real invariant, not a defensive guess.
        assert last_image_b64 is not None
        return self._screenshot_tool_result(last_summary, last_image_b64)

    def _record_halt_result(self, action: str, exc: HaltedError) -> ToolResult:
        """Shared halt bookkeeping for both `execute()`'s single-action path
        and `_execute_openai_action_batch()`'s per-item loop - see
        `execute()`'s original inline comment (Defect 1 + defect 2,
        docs/designs/coexistence.md \u00a76.0/\u00a713 D3) for the full rationale;
        moved here unchanged so both callers hit the exact same recording
        logic rather than two copies drifting apart."""
        self.halt_notices.append(
            {
                "at": time.time(),
                "action": action,
                "message": str(exc),
                "margin_ms": exc.snapshot.margin_ms,
                "guard_ms": exc.snapshot.guard_ms,
                "last_human_input_ago_ms": exc.snapshot.last_human_input_ago_ms,
                # \u00a75.7 (measured safety gap): declared alongside guard_ms,
                # not silently folded into it or omitted - see
                # presence.PresenceSnapshot's own docstring. ~0 for a
                # local backend; real and large for a remote one.
                "transport_latency_ms": exc.snapshot.transport_latency_ms,
                "effective_staleness_ms": exc.snapshot.effective_staleness_ms,
            }
        )
        backend_name = getattr(self._backend, "name", "unknown")
        record_halt(backend_name, exc.snapshot, reason=str(exc))
        return ToolResult(
            success=False, error={"message": str(exc), "type": type(exc).__name__}
        )

    def _screenshot_tool_result(self, summary: str, image_b64: str) -> ToolResult:
        """Package a successful `_run()` outcome that produced an image into
        the marker `ToolResult` `hook-computer-use` looks for - unchanged
        logic, extracted out of `execute()` so `_execute_openai_action_batch()`
        can reuse it instead of re-implementing screenshot persistence.

        Screenshots live on disk; only a path travels in the transcript. The hook
        inlines the bytes at request time, so the transcript never carries base64.
        (Marker protocol unchanged - see hook-computer-use.)

        Security hardening: previously one flat directory shared by every
        session, relying entirely on the inherited umask for permissions -
        on a shared/multi-user controller box that can leave screenshots of
        a driven desktop world- or group-readable for the full TTL window.
        Now: a per-session subdirectory (`self._session_id`, set once in
        `__init__`), and BOTH the directory and the file get an explicit
        `os.chmod` after creation - umask only affects the mode requested
        at creation time, it is not itself a guarantee, so this is what
        actually enforces owner-only access regardless of umask.
        """
        SHOT_DIR.mkdir(parents=True, exist_ok=True)
        os.chmod(SHOT_DIR, _PRIVATE_DIR_MODE)
        # Prune BEFORE creating this call's own session directory - not
        # after. `_prune_shots()` also removes now-empty session
        # directories (housekeeping for past sessions); running it after
        # creating (but before writing into) the CURRENT session directory
        # would race with that cleanup and delete the directory this very
        # call is about to write into, since it is briefly empty.
        _prune_shots()
        session_dir = SHOT_DIR / self._session_id
        session_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(session_dir, _PRIVATE_DIR_MODE)

        path = session_dir / f"{uuid.uuid4().hex}.png"
        path.write_bytes(base64.standard_b64decode(image_b64))
        os.chmod(path, _PRIVATE_FILE_MODE)
        disp = self.display
        return ToolResult(
            success=True,
            output=json.dumps(
                {
                    MARKER: 1,
                    "text": f"{summary} ({disp.model_width}x{disp.model_height})",
                    "images": [str(path)],
                }
            ),
        )


DESKTOP_ACTIONS = [
    "list_windows",
    "focus_window",
    "screen_info",
    "get_clipboard",
    "set_clipboard",
    "list_monitors",
    "select_monitor",
]

#: Clipboard *reads* travel to the model provider as tool output (see README Safety
#: section), the same exfiltration-risk surface `read_only` exists to close - but a
#: clipboard can carry things a screenshot never shows (a just-copied password, an
#: unseen paste buffer). `read_only` is documented as "screenshots only, all input
#: blocked"; a clipboard read is neither a screenshot nor input, but it is exactly
#: the kind of invisible exfiltration `read_only` mode is meant to prevent. Gated
#: accordingly - this is a deliberate behavior change from the original ungated
#: `get_clipboard`, not an oversight.
_READ_ONLY_BLOCKED = {"focus_window", "set_clipboard", "get_clipboard"}

#: Desktop actions that change target state - gated by `gate_writes` the same
#: way `MUTATING` gates `computer` actions (see `ComputerTool.__init__`).
#: `get_clipboard` is a read (already covered by `_READ_ONLY_BLOCKED` above for
#: its exfiltration risk, not because it changes anything).
MUTATING_DESKTOP = {"focus_window", "set_clipboard"}


class DesktopTool:
    """Window and clipboard helpers that the native `computer` tool cannot express.

    Once `computer` is promoted to Anthropic's server-side tool type, the model only
    knows that tool's fixed action list - so window management and clipboard access
    have to live somewhere else. This is that somewhere.
    """

    def __init__(self, computer: ComputerTool) -> None:
        self._computer = computer

    @property
    def name(self) -> str:
        return "desktop"

    @property
    def description(self) -> str:
        return (
            "Desktop helpers that complement the `computer` tool: list open windows, "
            "bring a window to the front before typing into it, read the display geometry, "
            "read or write the clipboard, and list/switch which physical monitor "
            "`computer` screenshots and clicks are scoped to. Use `list_windows` then "
            "`focus_window` to make sure keystrokes land in the right application. "
            "Clipboard access is the reliable way to pull exact text out of an app "
            "(select, copy, then get_clipboard). On a multi-monitor desktop, use "
            "`list_monitors` to see what's available and `select_monitor` to target one - "
            "`computer` is scoped to a single monitor by default so screenshots stay legible. "
            "This machine may be in use by a human at the same time as you: a window's "
            "focus or clipboard contents can change from something other than your own "
            "actions between calls, so re-check with `list_windows`/`get_clipboard` rather "
            "than assuming the state you last set still holds."
        )

    @property
    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": DESKTOP_ACTIONS},
                "handle": {
                    "type": "string",
                    "description": "Window handle from list_windows (focus_window).",
                },
                "text": {
                    "type": "string",
                    "description": "Text to place on the clipboard (set_clipboard).",
                },
                "monitor": {
                    "type": "string",
                    "description": (
                        "Monitor id from list_monitors, or 'primary' / "
                        "'virtual-desktop' (select_monitor)."
                    ),
                },
            },
            "required": ["action"],
        }

    async def execute(self, input: dict[str, Any]) -> ToolResult:
        # Same gate `computer` runs first (see `ComputerTool._ensure_announced`
        # and `ComputerTool.execute`'s own docstring) - `desktop` shares the
        # SAME `ComputerTool` instance, so this reuses (and, if this is the
        # first action of either tool this session, actually fires) the exact
        # same disclosure decision. A pure read like `get_clipboard` never
        # reaches `ComputerTool._run()` (it calls `backend.get_clipboard()`
        # directly, below) - so this explicit call, not `_run()` alone, is
        # what makes the gate cover every `desktop` action too, not just the
        # ones that happen to share `_run()` with `computer`.
        try:
            await asyncio.to_thread(self._computer._ensure_announced)
        except AnnouncementRefused as exc:
            return ToolResult(
                success=False,
                error={"message": str(exc), "type": "AnnouncementRefused"},
            )
        action = str(input.get("action") or "").strip()
        if action not in DESKTOP_ACTIONS:
            return ToolResult(
                success=False,
                error={
                    "message": f"unknown action {action!r}; expected one of {', '.join(DESKTOP_ACTIONS)}"
                },
            )
        if self._computer._read_only and action in _READ_ONLY_BLOCKED:
            return ToolResult(
                success=False,
                error={"message": f"action {action!r} blocked: mounted read_only"},
            )
        backend = self._computer._backend
        try:
            # C4, same reasoning as ComputerTool.execute: keep the sync Backend
            # protocol, move the (possibly-remote) blocking call off the event
            # loop at this boundary instead.
            if action == "get_clipboard":
                # §3 hardening: `clipboard_read_policy` (ComputerTool.__init__)
                # is an explicit, named, always-audited gate distinct from
                # `read_only` - see that attribute's docstring for the full
                # rationale and default rules.
                policy = self._computer._clipboard_read_policy
                if policy == "block":
                    return ToolResult(
                        success=False,
                        error={
                            "message": "action 'get_clipboard' blocked by "
                            "clipboard_read_policy=block"
                        },
                    )
                output = await asyncio.to_thread(backend.get_clipboard)
                digest = _text_digest(output)
                logger.info(
                    "computer-use audit: op=get_clipboard policy=%s chars=%d sha256=%s",
                    policy,
                    len(output),
                    digest,
                )
                if policy == "redact":
                    return ToolResult(
                        success=True,
                        output=(
                            f"<clipboard content redacted by policy "
                            f"(clipboard_read_policy=redact): {len(output)} chars, "
                            f"sha256={digest}>"
                        ),
                    )
                return ToolResult(success=True, output=output)
            if action == "set_clipboard":
                body = str(input.get("text") or "")
                # Same guard discipline every other mutating action in
                # `ComputerTool._run` now gets (§2) - `set_clipboard` mutates
                # target state but is dispatched here, not through `_run`,
                # so it needs its own explicit wiring rather than inheriting
                # `_guard_write` for free.
                with self._computer._guard_write():
                    await asyncio.to_thread(backend.set_clipboard, body)
                # §3 audit hardening: digest, never plaintext - the same
                # discipline `type` uses, since clipboard content is exactly
                # the kind of thing that "frequently includes credentials."
                logger.info(
                    "computer-use audit: op=set_clipboard chars=%d sha256=%s",
                    len(body),
                    _text_digest(body),
                )
                return ToolResult(success=True, output="clipboard set")
            if action == "list_monitors":
                monitors = await asyncio.to_thread(self._computer.list_monitors)
                current = self._computer.current_monitor
                lines = [
                    f"  [{m.id}] {m.width}x{m.height} at ({m.x},{m.y})"
                    f"{' (primary)' if m.primary else ''}"
                    f"{' [ACTIVE]' if current is not None and current.id == m.id else ''}"
                    for m in monitors
                ]
                mode = "virtual-desktop" if current is None else f"monitor {current.id}"
                return ToolResult(
                    success=True,
                    output=f"target mode: {mode}\nmonitors:\n" + "\n".join(lines),
                )
            if action == "select_monitor":
                target = str(input.get("monitor") or "").strip()
                if not target:
                    raise ValueError("action 'select_monitor' requires 'monitor'")
                disp = await asyncio.to_thread(self._computer.select_monitor, target)
                return ToolResult(
                    success=True,
                    output=(
                        f"target monitor set to {target!r}: screen "
                        f"{disp.screen_width}x{disp.screen_height} at "
                        f"({disp.origin_x},{disp.origin_y}) -> model "
                        f"{disp.model_width}x{disp.model_height}"
                    ),
                )
            if (
                self._computer._gate_writes
                and action in MUTATING_DESKTOP
                and not self._computer._unattended_writes_ok
                and not self._computer._interactive_write_approved
            ):
                # Fail-safe of last resort: reached only when NOTHING already
                # granted this write - no gate hook synced
                # `unattended_writes_ok=True` (the explicit unattended
                # config opt-in), no gate hook synced
                # `interactive_write_approved=True` (a human granting THIS
                # call via `ask_user`), and `gate_writes` was not explicitly
                # turned off. Every remedy is named here, not just pointed
                # at - a doc pointer alone has already shipped as an
                # unreachable-in-the-moment reference three times in this
                # repo.
                return ToolResult(
                    success=False,
                    error={
                        "message": (
                            f"action {action!r} requires confirmation "
                            "(gate_writes) and nothing granted it: no gate "
                            "hook is registered (or none has run yet), "
                            "'unattended_writes_ok' is not set, and "
                            "'gate_writes' was not explicitly disabled. The "
                            "write was NOT sent. To proceed, do ONE of: (1) "
                            "mount hook-computer-use so its 'tool:pre' gate "
                            "hook can grant approval (interactively via "
                            "'ask_user', or unattended - see (2)); (2) set "
                            "hook-computer-use config "
                            "'unattended_writes_ok: true' to allow writes on "
                            "this target with no human confirmation - a "
                            "deliberate, logged opt-in, never a default; or "
                            "(3) set tool-computer-use config "
                            "'gate_writes: false' to disable the gate "
                            "entirely for this target - a deliberate, "
                            "logged opt-out. See "
                            "docs/designs/remote-transport.md \u00a710.4 for "
                            "the policy rationale."
                        )
                    },
                )
            summary, _ = await asyncio.to_thread(self._computer._run, action, input)
            return ToolResult(success=True, output=summary)
        except (BackendError, ValueError) as exc:
            return ToolResult(
                success=False, error={"message": str(exc), "type": type(exc).__name__}
            )
        except Exception as exc:
            logger.exception("desktop action %s failed", action)
            return ToolResult(
                success=False, error={"message": str(exc), "type": type(exc).__name__}
            )


def _build_coexistence_guard(
    backend: Backend, cfg: dict[str, Any]
) -> CoexistenceGuard | None:
    """Build the `CoexistenceGuard` for `backend`, or `None` if this backend
    has no proven presence-detector wiring yet (`docs/designs/coexistence.md`).

    Deliberately conservative: a guard is only ever constructed for a backend
    that exposes `presence_idle_ms()` (today: `LinuxX11Backend`, `MacOSBackend`
    since `presence.GUARD_MS["macos"]` was measured by O4, `WindowsBackend`
    (via `bridge.ps1`'s `presence_idle` action), and `RemoteBackend` (forwards
    the read to the SAME method on the target's own backend, \u00a75 of
    `docs/designs/remote-transport.md`) - see each method's docstring). A
    backend with no such method gets no coexistence layer at all, rather than
    one built on a guessed/unmeasured `GUARD` band - the same "do not claim a
    guarantee you do not have" principle \u00a75.5 applies to Windows `type_text`.

    `cfg["coexistence"]` (all keys optional):
      - `enabled` (default `True` when the backend supports it): set `False`
        to opt out of building the layer entirely for this session. This is
        NOT the \u00a76.0 halt-invariant opt-out - once a guard exists, nothing
        in its config can disable the halt (see `coexistence_guard.py`
        module docstring). This key only controls whether one exists.
      - `drive_anyway` (default `False`, \u00a77.6/D5): permits *beginning* to
        drive when a human is already detected present at guard-construction
        time. Logged when it fires. Never affects the halt invariant.
    """
    coexistence_cfg = dict(cfg.get("coexistence") or {})
    idle_source = getattr(backend, "presence_idle_ms", None)
    if idle_source is None:
        return None
    if not bool(coexistence_cfg.get("enabled", True)):
        logger.info(
            "coexistence: disabled by config for backend %r - no presence "
            "detector, halt invariant, or target binding this session",
            backend.name,
        )
        return None
    # Coverage-gap fix: `backend.name` is the right key for LOCAL backends
    # ("linux-x11", "macos", "windows-wsl2" - each IS its own GUARD_MS
    # platform), but for `RemoteBackend` it is a COMPOSITE identifier
    # ("remote-ssh:windows-wsl2", unique per remote target - see that
    # class's own docstring for why it stays that way for logs/halt-state
    # keys) that is deliberately never a `GUARD_MS` key. `presence_platform`
    # (set only on `RemoteBackend`, from its own handshake) resolves to the
    # REMOTE machine's actual measured platform band instead - network
    # latency never gets folded into the guard band; the underlying
    # platform's own measured GUARD_MS is used unchanged, exactly as if
    # driving that platform locally.
    platform_for_guard = getattr(backend, "presence_platform", None) or backend.name
    if platform_for_guard not in GUARD_MS:
        logger.warning(
            "coexistence: backend %r exposes presence_idle_ms() but resolves "
            "to platform %r, which has no GUARD_MS entry; not building a guard",
            backend.name,
            platform_for_guard,
        )
        return None
    presence = PresenceMonitor(idle_source=idle_source, platform=platform_for_guard)
    ledger = HeldInputLedger()
    target_source = getattr(backend, "current_target", None)
    guard = CoexistenceGuard(
        presence=presence,
        release_all=lambda reason: ledger.release_all(reason=reason),
        drive_anyway=bool(coexistence_cfg.get("drive_anyway", False)),
        target_source=target_source,
        # Defect 2 fix: consult the durable halt record on every
        # before_event(), not just once here at mount time - closes the
        # window where a DIFFERENT session (same backend) detects a human
        # and persists it AFTER this guard already mounted (see
        # `halt_state.make_durable_halt_poll`'s docstring for the real
        # evaluation evidence).
        durable_halt_poll=make_durable_halt_poll(backend.name),
    )
    logger.info(
        "coexistence: guard built for backend %r (guard_ms=%.1f, measured=%s)",
        backend.name,
        presence.guard_ms,
        presence.guard_measured,
    )
    # \u00a75.7 (measured safety gap, docs/designs/coexistence.md): a remote
    # backend's presence_idle_ms() is an SSH round trip (plus, on Windows, a
    # per-op powershell.exe spawn) - not the in-process microsecond call
    # `guard_ms` was measured against. Measured on windows-host (n=80,
    # key("shift")): 296-875ms. Declared HERE, at construction, the same
    # place every other coexistence capability already declares what it can
    # and cannot promise (\u00a75.5's Windows intra-type_text declaration) -
    # never left for a caller to discover only by noticing a halt came late.
    # Every live sample ALSO carries its own measured
    # transport_latency_ms/effective_staleness_ms (presence.PresenceSnapshot)
    # so this is a standing notice, not the only place it is visible.
    if bool(getattr(backend, "is_remote", False)):
        logger.warning(
            "coexistence: backend %r is remote - every presence sample "
            "crosses a transport whose measured latency (296-875ms, "
            "windows-host n=80) is up to ~40x this platform's %.1fms "
            "guard_ms. Do not read guard_ms alone as the size of this "
            "session's blind window; each sample's own "
            "effective_staleness_ms is the honest figure.",
            backend.name,
            presence.guard_ms,
        )
    # Defect 2 fix: a brand-new guard has no memory of a human detected in a
    # PRIOR session against this same backend - `_halted` is a plain
    # in-memory field on an object that stops existing when its mount does
    # (see `halt_state.py` module docstring for the real evaluation evidence
    # this closes: a halted sub-agent session ended, its parent session
    # mounted its OWN fresh guard, and writes resumed automatically ~80s
    # later with no human ever choosing to resume anything). If a durable
    # halt record exists for this backend, seed this guard already-halted -
    # the ONLY way past it is an explicit human action (`resolve_resume_command()`
    # below - resolved against THIS process, never a bare name assumed to be
    # on PATH), never the mere passage of time.
    persisted = load_halt(backend.name)
    if persisted is not None:
        guard.seed_halted(persisted.to_snapshot())
        logger.warning(
            "coexistence: backend %r has a durable halt record from a prior "
            "session (reason=%r) - this session starts already HALTED; run "
            "%s to clear it explicitly (docs/designs/coexistence.md \u00a713 D3)",
            backend.name,
            persisted.reason,
            resolve_resume_command(),
        )
    return guard


class AnnouncementRefused(RuntimeError):
    """The session-start announcement gate refused to let this session begin
    driving (`docs/designs/coexistence.md` \u00a77.3/\u00a77.6).

    Distinct from `NoBackendAvailable`: the backend itself is fine (the
    display, the injector, the guard all work) - what refused is the
    disclosure gate. Caught by `mount()` exactly like `NoBackendAvailable`:
    logged plainly, nothing mounted, no traceback.
    """


def _channel_failure_snapshot(guard: CoexistenceGuard) -> PresenceSnapshot:
    """A live presence read taken at the moment a disclosure channel failed
    to display - safe to call here because every caller of `_build_announcement`
    (`ComputerTool._ensure_announced`) holds that instance's own
    `_announce_lock` for the whole call, so there is at most one thread per
    `ComputerTool` inside `_build_announcement`/`_dispatch_announcement` at a
    time, and that thread is the only one touching this `guard` this early -
    every other action on the same instance blocks on `_ensure_announced`
    before it can reach anything that mutates `guard.presence` (unlike the
    overlay's own click callbacks - see `_on_overlay_cancel` below, which
    deliberately does NOT call this)."""
    return guard.presence.sample()


def _handle_channel_failure(
    guard: CoexistenceGuard, backend_name: str, channel: str, exc: Exception
) -> None:
    """\u00a77.6's rule, applied uniformly to every disclosure channel (Linux
    overlay, Windows overlay, macOS dialog): a human detected present, with
    no working way to tell them an agent is about to drive this machine, is
    refused outright. Nobody detected present -> proceed, but LOUDLY (an
    `logger.error`, not a swallowed exception) - this is the \"loud, not
    silent\" requirement: a channel that failed to display must never look,
    from the caller's side, identical to a channel that was never attempted.
    """
    try:
        snap = _channel_failure_snapshot(guard)
        human_present = snap.state is PresenceState.HUMAN_ACTIVE
    except IdleUnreadableError:
        # \u00a79.6: an unreadable idle counter is a hard error, never guessed as
        # "quiet" - the same fail-safe direction applies here: treat it as if
        # a human were detected.
        human_present = True
    if human_present:
        raise AnnouncementRefused(
            f"{channel} failed to display on backend {backend_name!r} ({exc}) "
            "and a human is currently detected at this machine - refusing to "
            "begin driving with no working disclosure channel "
            "(docs/designs/coexistence.md \u00a77.6). Not overridable by default; "
            "see that section for the explicit, logged per-target opt-out."
        ) from exc
    logger.error(
        "coexistence: %s failed to display on backend %r (%s) - no human is "
        "currently detected, so this session proceeds WITHOUT a disclosure "
        "channel. This is loud, not silent: if a human sits down mid-session "
        "there is nothing to warn them beyond the halt invariant itself.",
        channel,
        backend_name,
        exc,
    )


def _on_overlay_pause(guard: CoexistenceGuard, backend_name: str) -> None:
    """Wired as the overlay's Pause button callback - runs on the overlay's
    own background poll thread (`overlay_linux.LinuxOverlay._poll_events` /
    `overlay_windows.WindowsOverlay._poll_events`), never on the guard's
    usual single-threaded call path. This is safe BY DESIGN, not by luck:
    `pause.PauseController` exists specifically so a human, via the overlay,
    may set pause (`HUMAN_SOURCES` names `\"overlay_click\"` explicitly in its
    own docstring) - this is that wiring, not a new one invented here.
    """
    guard.pause.set("overlay_click", reason="human clicked Pause on the overlay")
    logger.warning("coexistence: human paused via overlay (backend=%r)", backend_name)


def _on_overlay_cancel(guard: CoexistenceGuard, backend_name: str) -> None:
    """Wired as the overlay's Cancel button callback (\u00a78.5: cancel is
    terminal, unlike pause). Runs on the SAME background poll thread as
    `_on_overlay_pause` above.

    Deliberately does NOT call `guard.seed_halted()` or `guard.presence.sample()`
    directly from this thread - both mutate multi-field state
    (`CoexistenceGuard._halted`/`_halt_snapshot`, `PresenceMonitor`'s internal
    fields) that `before_event()` reads/writes from the guard's own
    single-threaded call path, and doing so from a second thread would be a
    genuinely NEW race this codebase does not otherwise have (unlike
    `PauseController.set()`, which was built for exactly this cross-thread
    call). Instead this only writes the DURABLE halt record
    (`halt_state.record_halt`) - the exact mechanism `_poll_durable_halt()`
    already polls for, from the guard's own thread, on the very next
    `before_event()` call. That is what actually latches `_halted=True`;
    this function only ever gets the fact onto disk.
    """
    guard.release_all("cancelled_via_overlay")
    snap = PresenceSnapshot(
        state=PresenceState.HUMAN_ACTIVE,
        confidence=Confidence.HIGH,
        basis="overlay_cancel",
        last_human_input_ago_ms=0.0,
        margin_ms=None,
        guard_ms=guard.presence.guard_ms,
        guard_measured=guard.presence.guard_measured,
        sample_interval_ms=None,
        latched_until_ms=None,
    )
    record_halt(backend_name, snap, reason="cancelled via overlay Cancel button")
    logger.warning(
        "coexistence: human clicked Cancel via overlay (backend=%r) - a "
        "durable halt record was written; every write on this backend is "
        "refused starting with the next guard check, this session and any "
        "future one, until a human explicitly clears it (see "
        "halt_state.resolve_resume_command())",
        backend_name,
    )


def _macos_announce_message(timeout: int, *, controller_host: str) -> str:
    """The \u00a77.3 disclosed-timeout dialog text, shared by the local and
    remote paths - `controller_host` is the machine driving the Mac (its own
    hostname when local, the actual controller's hostname when remote), so
    the same sentence is honest in both deployment shapes.
    """
    return (
        "An automated agent (Amplifier computer-use) wants to drive this Mac "
        f"from {controller_host}.\n\n"
        f"This prompt closes in {timeout} seconds.\n"
        "If nobody answers and this Mac is idle, driving will start.\n"
        "If nobody answers and someone is using this Mac, driving will NOT "
        "start.\n\n"
        "Click Continue to allow driving to begin, or Pause to refuse."
    )


def _apply_macos_announce_result(
    guard: CoexistenceGuard, backend_name: str, result: AnnounceResult
) -> None:
    """The \u00a77.3 policy (rules 1-3), applied to an `AnnounceResult` -
    shared by `_handle_macos_announce` (the dialog ran in THIS process,
    because this process IS the Mac) and `_handle_remote_macos_announce`
    (the dialog ran on a DIFFERENT Mac, relayed back over the wire). The
    decision rules do not care which; only where the dialog physically
    displayed differs, and that distinction is already resolved by the time
    an `AnnounceResult` reaches this function.
    """
    if result.acknowledged:
        if result.button == "Continue":
            logger.info(
                "coexistence: macOS announce dialog acknowledged (Continue) "
                "- driving begins (backend=%r)",
                backend_name,
            )
            return
        raise AnnouncementRefused(
            f"macOS announce dialog was answered {result.button!r} - the "
            "human explicitly declined to allow driving to begin this "
            "session (docs/designs/coexistence.md \u00a77.3)."
        )
    # gave_up: a countdown nobody answered is never consent (rule 2). What it
    # permits is decided by a fresh presence sample, not the clock (rule 3).
    try:
        snap = guard.presence.sample()
    except IdleUnreadableError as exc:
        raise AnnouncementRefused(
            "macOS announce dialog timed out with no answer, and presence "
            f"could not be read to decide what that permits ({exc}) - "
            "refusing to begin driving (docs/designs/coexistence.md \u00a77.3)."
        ) from exc
    if snap.state is PresenceState.QUIET:
        logger.info(
            "coexistence: macOS announce dialog timed out with nobody there "
            "to answer (presence=quiet) - proceeding, per "
            "docs/designs/coexistence.md \u00a77.3 rule 3 (backend=%r)",
            backend_name,
        )
        return
    raise AnnouncementRefused(
        "macOS announce dialog timed out (gave_up) and presence sampled "
        f"{snap.state.value!r} at that moment - a non-answer while someone "
        "may be at the machine is treated as a refusal, never as consent "
        "(docs/designs/coexistence.md \u00a77.3 rules 2-3)."
    )


def _handle_macos_announce(
    guard: CoexistenceGuard, backend_name: str, cfg: dict[str, Any]
) -> None:
    """One announce-and-acknowledge dialog at session start, before the first
    write (`docs/designs/coexistence.md` \u00a77.3) - implements that section's
    three rules exactly (see `_apply_macos_announce_result`).

    The dialog's actual buttons are "Pause"/"Continue" (the tested,
    implemented contract in `announce_macos.py` - see `tests/test_announce_macos.py`),
    not the illustrative "Don't allow"/"Allow" mockup text in the design
    doc's prose; this follows the code that was actually built and tested.
    """
    coexistence_cfg = dict(cfg.get("coexistence") or {})
    timeout = int(
        coexistence_cfg.get("announce_timeout_seconds", MACOS_ANNOUNCE_TIMEOUT_SECONDS)
    )
    message = _macos_announce_message(timeout, controller_host=socket.gethostname())
    try:
        result = macos_announce(message, timeout_seconds=timeout)
    except AnnounceError as exc:
        _handle_channel_failure(guard, backend_name, "macOS announce dialog", exc)
        return
    _apply_macos_announce_result(guard, backend_name, result)


#: Guards `_announcement_decisions` below. A plain `threading.Lock`, not
#: per-key: contention is momentary (dict read/write only - never held
#: across the actual dialog/overlay/RPC call, see `_build_announcement`) and
#: mount() itself is rare enough in any one process that a single lock is
#: not a bottleneck worth complicating.
_announcement_lock = threading.Lock()

#: One decision per PHYSICAL disclosure channel, cached for the life of this
#: controller process - see `_channel_identity` for what "physical channel"
#: means and `_build_announcement` for why this exists. The defect this
#: fixes: a parent session's own `mount()` and a delegated child session's
#: `mount()` (`tool-delegate` inherits the parent's tool config, including
#: any `target:` - see `amplifier_module_tool_delegate._merge_tools`) each
#: used to independently decide whether to proceed, against the SAME
#: machine, without knowing the other had already asked.
#: `shared_transport.py` already solved this exact "more than one consumer
#: in this process talks to the same target" problem for the SSH connection
#: itself (see that module's own docstring); this dict is the same fix one
#: layer up, for the disclosure gate.
_announcement_decisions: dict[str, _AnnouncementDecision] = {}


@dataclass(frozen=True)
class _AnnouncementDecision:
    """The outcome of the FIRST mount() to ask this channel's question -
    reused verbatim by every later mount() in this process rather than
    asking (or refusing) again. Exactly one of `handle`/`refused` is
    meaningful; `refused is not None` means the channel was declined, and
    every subsequent mount() for this channel refuses too, without
    re-showing anything - re-asking after a human already said no is worse
    than not asking at all (it trains people to click through)."""

    handle: Any
    refused: AnnouncementRefused | None


def _channel_identity(backend: Backend) -> str:
    """A stable identity for the PHYSICAL machine a disclosure channel would
    target - the same identity for every `ComputerTool` mount in this
    process that would show a dialog/overlay on the SAME desktop, however
    many separate `mount()` calls (parent session, delegated child session,
    a second delegated child, ...) each independently construct their own
    `Backend`/`ComputerTool` instances for it.

    Remote: `user_host` (added to `RemoteBackend` alongside this fix) - the
    actual `user@host` string, not `backend.name` (which is
    `"remote-ssh:<platform>"` - identical for any two DIFFERENT hosts that
    happen to run the same platform, e.g. two macOS targets). Falls back to
    `backend.name` only if some future `Backend` sets `is_remote = True`
    without a `user_host` - never true for `RemoteBackend` itself past
    `connect()`.

    Local: `backend.name` alone (e.g. "linux-x11", "windows-wsl2", "macos")
    - a controller process only ever drives one local desktop, so the
    backend type is already a unique-enough key.
    """
    if bool(getattr(backend, "is_remote", False)):
        host = getattr(backend, "user_host", None)
        return f"remote:{host}" if host else f"remote:{backend.name}"
    return f"local:{backend.name}"


def _build_announcement(
    backend: Backend,
    guard: CoexistenceGuard | None,
    cfg: dict[str, Any],
    disp: Display,
) -> Any | None:
    """Build (and show) the session-start disclosure this backend supports,
    or refuse via `AnnouncementRefused` (\u00a77.3/\u00a77.6). Called exactly once per
    `ComputerTool` instance, from `ComputerTool._ensure_announced` on that
    session's first real action - NOT from `mount()` (see that function's
    own comment for why: a throwaway protocol-compliance probe also calls
    `mount()`, and must never be able to trigger this).

    Returns whatever handle (if any) needs to stay alive for this tool's
    lifetime (an overlay object) so it is not garbage-collected - `None` for
    a one-shot channel (macOS's dialog) or when no channel exists at all.

    `cfg[\"coexistence\"][\"announce\"]` (default `True`): the on/off switch for
    this feature, symmetric with `_build_coexistence_guard`'s own `enabled`
    key - \"on by default\" per the design brief, with the same kind of
    explicit, logged opt-out the rest of this module already uses for other
    policy knobs.
    """
    coexistence_cfg = dict(cfg.get("coexistence") or {})
    if not bool(coexistence_cfg.get("announce", True)):
        logger.info(
            "coexistence: announcement disabled by config for backend %r",
            backend.name,
        )
        return None
    if guard is None:
        # No presence detector at all for this backend (\u00a75.5) - there is no
        # way to apply \u00a77.6's human-detected-vs-not policy, so nothing can be
        # safely built here. Loud, not silent: this backend has neither halt
        # protection nor a disclosure channel.
        logger.warning(
            "coexistence: backend %r has no presence detector, so no "
            "announcement channel can be safely gated either "
            "(docs/designs/coexistence.md \u00a77.6) - this session has neither "
            "halt protection nor a disclosure channel",
            backend.name,
        )
        return None

    channel_key = _channel_identity(backend)
    with _announcement_lock:
        cached = _announcement_decisions.get(channel_key)
    if cached is not None:
        if cached.refused is not None:
            logger.warning(
                "coexistence: NOT re-asking for %r - an earlier mount() in "
                "this process already asked and the human declined (%s). "
                "This mount is refused without showing anything again: "
                "re-asking after a refusal is worse than not asking at all.",
                channel_key,
                cached.refused,
            )
            raise AnnouncementRefused(str(cached.refused))
        logger.info(
            "coexistence: reusing the disclosure decision an earlier "
            "mount() in this process already made for %r - a delegated "
            "child session (or a second tool config) targeting the same "
            "machine does not get its own dialog/overlay",
            channel_key,
        )
        return cached.handle

    try:
        handle = _dispatch_announcement(backend, guard, cfg, disp)
    except AnnouncementRefused as exc:
        with _announcement_lock:
            _announcement_decisions.setdefault(
                channel_key, _AnnouncementDecision(handle=None, refused=exc)
            )
        raise
    with _announcement_lock:
        _announcement_decisions.setdefault(
            channel_key, _AnnouncementDecision(handle=handle, refused=None)
        )
    return handle


def _dispatch_announcement(
    backend: Backend,
    guard: CoexistenceGuard,
    cfg: dict[str, Any],
    disp: Display,
) -> Any | None:
    """The actual per-backend-type disclosure logic `_build_announcement`
    memoizes above - unchanged from before that cache existed. Split out so
    the memoization wrapper never has to duplicate (or risk drifting from)
    any of these branches; `guard` is narrowed to non-None here because
    `_build_announcement` already returned early for that case."""

    if isinstance(backend, LinuxX11Backend):
        try:
            overlay = LinuxOverlay(
                backend.display,
                screen_width=disp.screen_width,
                screen_x=disp.origin_x,
                screen_y=disp.origin_y,
                exclusion=guard.exclusion,
                on_pause=lambda: _on_overlay_pause(guard, backend.name),
                on_cancel=lambda: _on_overlay_cancel(guard, backend.name),
            )
            overlay.show()
        except Exception as exc:  # noqa: BLE001 - any failure -> the shared \u00a77.6 policy
            _handle_channel_failure(guard, backend.name, "Linux overlay", exc)
            return None
        logger.info("coexistence: Linux overlay shown for backend %r", backend.name)
        return overlay

    if isinstance(backend, WindowsBackend):
        overlay = WindowsOverlay(
            screen_width=disp.screen_width,
            screen_x=disp.origin_x,
            screen_y=disp.origin_y,
            exclusion=guard.exclusion,
            on_pause=lambda: _on_overlay_pause(guard, backend.name),
            on_cancel=lambda: _on_overlay_cancel(guard, backend.name),
            powershell_path=cfg.get("powershell_path"),
        )
        try:
            overlay.show()
        except Exception as exc:  # noqa: BLE001 - any failure -> the shared \u00a77.6 policy
            _handle_channel_failure(guard, backend.name, "Windows overlay", exc)
            return None
        # No cross-process handle ties this detached PID's life to this
        # agent process's (see overlay_windows.py's module docstring, Phase
        # C5/transport Phase 4) - `atexit` is the honest, minimal stand-in:
        # best-effort cleanup on every normal exit path this process has.
        atexit.register(overlay.hide)
        logger.info("coexistence: Windows overlay shown for backend %r", backend.name)
        return overlay

    if isinstance(backend, MacOSBackend):
        _handle_macos_announce(guard, backend.name, cfg)
        return None

    if bool(getattr(backend, "is_remote", False)):
        return _build_remote_announcement(backend, guard, cfg, disp)

    # Deliberate scope boundary, not a silent gap: a genuinely new backend
    # type this module does not yet know how to announce for.
    logger.warning(
        "coexistence: no announcement channel implemented for backend %r - "
        "a deliberate scope boundary, not a silent gap: the halt invariant "
        "(\u00a76.0) still enforces stop-on-detected-human for this backend, but "
        "there is no proactive disclosure to a human who sits down "
        "mid-session",
        backend.name,
    )
    return None


@dataclass(frozen=True)
class _RemoteAnnouncementHandle:
    """Truthy sentinel stored in `ComputerTool._announcement` when a REMOTE
    target's persistent overlay was raised successfully - there is nothing
    LOCAL to keep alive for it (no thread, no subprocess: the overlay lives
    entirely in the target-side `RemoteAgent`, torn down by ITS OWN shutdown
    path - see `remote_agent.RemoteAgent._teardown_overlay`, wired into the
    same `finally`/signal-handler paths that already guarantee held-input
    release for a remote session). Its only job is to tell
    `ComputerTool._sync_remote_announcement_state` that a channel exists and
    is worth polling before every guarded write - `bool(handle)` is always
    `True` for a real instance, so `self._announcement is not None` alone
    already means \"poll it\".
    """

    backend_name: str


def _build_remote_announcement(
    backend: Backend, guard: CoexistenceGuard, cfg: dict[str, Any], disp: Display
) -> Any | None:
    """The remote counterpart of the three local branches above
    (docs/designs/coexistence.md \u00a77, \u00a710.3) - closes the gap the prior
    pass left as a deliberate scope boundary (see BACKLOG.md): every
    existing announcement module was architected around running in the
    SAME process that owns the injection call site, which for a remote
    session is the TARGET-side `RemoteAgent`, never this controller. This
    function only ever asks the target to raise its own channel
    (`RemoteBackend.announce_raise`, forwarding to
    `remote_agent.RemoteAgent._op_announce_raise`) and applies the exact
    same \u00a77.3/\u00a77.6 policy the local branches already enforce to whatever
    comes back.

    `presence_platform` (set on `RemoteBackend` from its own handshake, the
    same field `_build_coexistence_guard` already uses to resolve
    `GUARD_MS`) - not `backend.name`, which for a remote backend is the
    composite `"remote-ssh:<platform>"` identifier - selects which flavor
    of channel to ask for.
    """
    platform = getattr(backend, "presence_platform", None)
    if platform == "macos":
        _handle_remote_macos_announce(backend, guard, cfg)
        return None
    if platform in ("linux-x11", "windows-wsl2"):
        return _handle_remote_overlay_announce(backend, guard, disp)
    logger.warning(
        "coexistence: no announcement channel implemented for remote "
        "platform %r (backend=%r) - a deliberate scope boundary, not a "
        "silent gap: the halt invariant (\u00a76.0) still enforces "
        "stop-on-detected-human for this backend, but there is no proactive "
        "disclosure to a human who sits down mid-session",
        platform,
        backend.name,
    )
    return None


def _handle_remote_macos_announce(
    backend: Backend, guard: CoexistenceGuard, cfg: dict[str, Any]
) -> None:
    """Remote counterpart of `_handle_macos_announce`: the dialog runs ON
    the target (`remote_agent.RemoteAgent._op_announce_raise`), blocking
    THAT process, not this one, for up to `timeout` seconds - session start
    only, exactly like the local case, never on the injection path. The
    message discloses THIS controller's own hostname (`socket.gethostname()`
    here IS the controller, unlike the local case where it is the Mac
    naming itself) so the human at the far end knows where the session is
    coming from.
    """
    coexistence_cfg = dict(cfg.get("coexistence") or {})
    timeout = int(
        coexistence_cfg.get("announce_timeout_seconds", MACOS_ANNOUNCE_TIMEOUT_SECONDS)
    )
    message = _macos_announce_message(timeout, controller_host=socket.gethostname())
    announce_raise = getattr(backend, "announce_raise", None)
    if announce_raise is None:
        _handle_channel_failure(
            guard,
            backend.name,
            "macOS announce dialog (remote)",
            RuntimeError("remote backend has no announce_raise()"),
        )
        return
    try:
        raw = announce_raise(message=message, timeout_seconds=timeout)
    except BackendError as exc:
        _handle_channel_failure(
            guard, backend.name, "macOS announce dialog (remote)", exc
        )
        return
    result = AnnounceResult(
        button=raw.get("button"), gave_up=bool(raw.get("gave_up")), raw_stdout=""
    )
    _apply_macos_announce_result(guard, backend.name, result)


def _handle_remote_overlay_announce(
    backend: Backend, guard: CoexistenceGuard, disp: Display
) -> Any | None:
    """Remote counterpart of the local Linux/Windows overlay branches: ask
    the target to raise its OWN persistent overlay
    (`remote_agent.RemoteAgent._op_announce_raise`), at the exact screen
    geometry this session already resolved for that target (`disp`, from
    `RemoteBackend.screen_geometry()`/`list_monitors()` - already proven to
    work over the wire). On success, registers the overlay's own
    Pause/Cancel button rects into `guard.exclusion` (\u00a77.5) so THIS
    session's own synthetic clicks refuse to land on them, exactly as the
    local branches already do via `exclusion=guard.exclusion` - the only
    difference is the rects are reported back over the wire rather than
    read off a local object, since the buttons themselves were drawn on
    the target, not here.
    """
    announce_raise = getattr(backend, "announce_raise", None)
    if announce_raise is None:
        _handle_channel_failure(
            guard,
            backend.name,
            "remote overlay",
            RuntimeError("remote backend has no announce_raise()"),
        )
        return None
    try:
        raw = announce_raise(
            screen_width=disp.screen_width,
            screen_x=disp.origin_x,
            screen_y=disp.origin_y,
        )
    except BackendError as exc:
        _handle_channel_failure(guard, backend.name, "remote overlay", exc)
        return None
    if not raw.get("shown"):
        _handle_channel_failure(
            guard,
            backend.name,
            "remote overlay",
            RuntimeError(f"target reported the overlay was not shown: {raw!r}"),
        )
        return None
    for name, rect in (raw.get("buttons") or {}).items():
        if isinstance(rect, (list, tuple)) and len(rect) == 4:
            guard.exclusion.register(
                f"overlay_{name}_button", Rect(*(int(v) for v in rect))
            )
    logger.info("coexistence: remote overlay shown for backend %r", backend.name)
    return _RemoteAnnouncementHandle(backend.name)


async def mount(
    coordinator: Any, config: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Probe for a usable backend, and only then mount `computer` and `desktop`.

    D1 fix: this used to construct `WindowsBridge` and mount both tools
    unconditionally - on any platform. Now every configured backend is probed
    first (`registry.select_backend`); if none can serve this machine, nothing is
    mounted, the reason is logged, and this function returns normally (it does not
    raise - a missing backend is not a bundle-load failure).
    """
    cfg = config or {}
    try:
        backend = select_backend(cfg)
    except NoBackendAvailable as exc:
        logger.warning("tool-computer-use: not mounting - %s", exc)
        return {
            "name": "tool-computer-use",
            "version": __version__,
            "provides": [],
            "description": f"computer-use not mounted: {exc}",
        }
    except (ValueError, TypeError) as exc:
        # A malformed config (e.g. `target: user@host` instead of
        # `target: ssh://user@host`) raises out of `select_backend`, NOT as
        # `NoBackendAvailable`. Before this branch existed it escaped the handler
        # above entirely and the tool simply never appeared - no traceback, no
        # log line, nothing in the session to explain the absence. Observed for
        # real: a session was asked to drive a remote desktop, found no tool, and
        # silently improvised its own ssh+screencapture workaround instead.
        #
        # Deliberately ERROR, not WARNING: an unavailable backend is a fact about
        # the machine, but a malformed target is a mistake someone made and can
        # fix, and they cannot fix what they cannot see. Still non-fatal - a bad
        # config for one tool must not take down the whole bundle load.
        logger.error("tool-computer-use: NOT MOUNTING - invalid configuration: %s", exc)
        return {
            "name": "tool-computer-use",
            "version": __version__,
            "provides": [],
            "description": f"computer-use not mounted (invalid config): {exc}",
        }

    computer = ComputerTool(backend, cfg)
    # D2: resolve display once, here, before the tool ever answers a provider
    # request - not lazily on the first `native_tool_spec` read.
    computer.resolve_display()
    # Human/agent coexistence (docs/designs/coexistence.md) - only built for
    # backends with a proven presence-detector wiring (see
    # `_build_coexistence_guard`). `None` on every other backend, unchanged
    # from before this feature existed.
    computer._coexistence_guard = _build_coexistence_guard(backend, cfg)
    # Session-start disclosure (docs/designs/coexistence.md §7) - the
    # Linux/Windows overlay or the macOS announce-and-acknowledge dialog,
    # whichever this backend supports - is deliberately NOT built here.
    #
    # It used to be: `mount()` called `_build_announcement()` directly, right
    # after the coexistence guard, and could refuse to mount via
    # `AnnouncementRefused`. That broke the moment a REAL session started:
    # `amplifier_core`'s loader calls every tool module's `mount()` TWICE -
    # once as a throwaway protocol-compliance probe
    # (`amplifier_core.validation.tool.ToolValidator._check_protocol_compliance`,
    # against a `MockCoordinator` whose result is discarded and torn down a
    # few lines later) and once for real. Both calls ran this module's real
    # `mount()` with the real config, so the probe showed a real dialog to a
    # real human for a `ComputerTool`/`CoexistenceGuard` pair that was about
    # to be thrown away - and any consent given applied to that discarded
    # pair, not the one actually about to drive anything.
    #
    # The disclosure now fires on THIS session's first real action instead -
    # see `ComputerTool._ensure_announced`, called at the top of both
    # `ComputerTool.execute()` and `DesktopTool.execute()`. A validation
    # probe never calls `execute()`, only `mount()`, so it cannot trigger
    # this at all. `mount()` therefore always proceeds to mount both tools
    # below; refusal (and the same backend.close() safety net that used to
    # live here) happens later, at first use.
    await coordinator.mount("tools", computer, name=computer.name)
    desktop = DesktopTool(computer)
    await coordinator.mount("tools", desktop, name=desktop.name)
    logger.info(
        "tool-computer-use mounted: 'computer' (%s, backend=%s) + 'desktop'",
        computer._tool_version,
        backend.name,
    )
    return {
        "name": "tool-computer-use",
        "version": __version__,
        "provides": ["computer", "desktop"],
        "description": f"Anthropic native computer-use via backend={backend.name}",
    }
