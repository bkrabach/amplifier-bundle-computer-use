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
import base64
import hashlib
import inspect
import json
import logging
import os
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from amplifier_core.models import ToolResult

from .backend import Backend, BackendError, MonitorInfo
from .coexistence_guard import CoexistenceGuard
from .geometry import Display, compute_display
from .imaging import capture_scaled_b64
from .ledger import HeldInputLedger
from .monitors import PRIMARY, VIRTUAL_DESKTOP, select_monitor
from .presence import GUARD_MS, PresenceMonitor
from .registry import NoBackendAvailable, select_backend
from .tool_versions import (
    beta_header_for,
    require_static_pairing,
    resolve_tool_version,
)
from .type_pacing import resolve_type_pacing_ms

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
        # `get_clipboard` (docs/designs/DesktopTool) previously had no gate
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
            "Always take a screenshot before acting so you can see where things are."
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
    def native_tool_spec(self) -> dict[str, Any]:
        """Anthropic server-side tool definition, sized to the cached display.

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
        construction: `hook-computer-use` re-reads `native_tool_spec` fresh on
        *every* `provider:request` (see `_promote_tools` in that module - it is
        never cached at the hook layer; only D2's *I/O* was cached here, not the
        *value*), so the very next request after a monitor switch automatically
        declares the new dimensions. The in-memory state this property reads
        (`self._display`) is exactly what `select_monitor` updates - one piece of
        state, two readers (this property and `_run`), always in sync.
        """
        disp = self.display
        spec: dict[str, Any] = {
            "type": self._tool_version,
            "name": "computer",
            "display_width_px": disp.model_width,
            "display_height_px": disp.model_height,
        }
        if self._enable_zoom and self._tool_version >= "computer_20251124":
            spec["enable_zoom"] = True
        return spec

    @property
    def native_beta_header(self) -> str:
        return beta_header_for(self._tool_version)

    def note_model(self, model: str | None) -> None:
        """Re-resolve `tool_version` for the model about to receive THIS request.

        Called by hook-computer-use on every `provider:request`, before it reads
        `native_tool_spec`/`native_beta_header` - see `tool_versions.py` module
        docstring. Never raises: a mid-session exception here would take down
        the whole request, the exact class of bug D3 already fixed once for
        `native_tool_spec` itself.
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
        guard.check_start_permission()
        guard.bind_target()
        guard.before_event(coord=coord)
        try:
            yield
        finally:
            guard.after_event()
            guard.release_target()

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
            visible = [w for w in result.windows if not w.minimized][:25]
            listing = "\n".join(f"  [{w.handle}] {w.title}" for w in visible)
            return f"visible windows (foreground={result.foreground}):\n{listing}", None

        if action == "focus_window":
            handle = params.get("handle")
            if not handle:
                raise ValueError("action 'focus_window' requires 'handle'")
            with self._guard_write():
                backend.focus_window(str(handle))
            return f"focused window {handle}", None

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
        action = str(input.get("action") or "").strip()
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
            # C4: `Backend` stays synchronous (local paths and all existing tests
            # untouched), but a remote action can block on a network round trip
            # for hundreds of milliseconds. Running it in a thread keeps the
            # event loop live so cancellation can actually be serviced during
            # that wait, instead of stalling behind a screenshot transfer.
            # Cheap locally too - `asyncio.to_thread` on a microsecond-scale
            # X11/Quartz call costs a thread-pool round trip, not a network one.
            summary, image_b64 = await asyncio.to_thread(self._run, action, input)
        except (BackendError, ValueError) as exc:
            return ToolResult(
                success=False, error={"message": str(exc), "type": type(exc).__name__}
            )
        except Exception as exc:
            logger.exception("computer action %s failed", action)
            return ToolResult(
                success=False, error={"message": str(exc), "type": type(exc).__name__}
            )

        if image_b64 is None:
            return ToolResult(success=True, output=summary)

        # Screenshots live on disk; only a path travels in the transcript. The hook
        # inlines the bytes at request time, so the transcript never carries base64.
        # (Marker protocol unchanged - see hook-computer-use.)
        #
        # Security hardening: previously one flat directory shared by every
        # session, relying entirely on the inherited umask for permissions -
        # on a shared/multi-user controller box that can leave screenshots of
        # a driven desktop world- or group-readable for the full TTL window.
        # Now: a per-session subdirectory (`self._session_id`, set once in
        # `__init__`), and BOTH the directory and the file get an explicit
        # `os.chmod` after creation - umask only affects the mode requested
        # at creation time, it is not itself a guarantee, so this is what
        # actually enforces owner-only access regardless of umask.
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
            "`computer` is scoped to a single monitor by default so screenshots stay legible."
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
            if self._computer._gate_writes and action in MUTATING_DESKTOP:
                return ToolResult(
                    success=False,
                    error={
                        "message": (
                            f"action {action!r} requires confirmation "
                            "(gate_writes) but no gate hook is registered to "
                            "grant it - see docs/designs/remote-transport.md "
                            "\u00a710.4"
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
    that exposes `presence_idle_ms()` (today: `LinuxX11Backend` and, since
    `presence.GUARD_MS["macos"]` was measured by O4, `MacOSBackend` too - see
    each method's docstring). A backend with no such method gets no
    coexistence layer at all, rather than one built on a guessed/unmeasured
    `GUARD` band - the same "do not claim a guarantee you do not have"
    principle \u00a75.5 applies to Windows `type_text`.

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
    if backend.name not in GUARD_MS:
        logger.warning(
            "coexistence: backend %r exposes presence_idle_ms() but has no "
            "GUARD_MS entry; not building a guard",
            backend.name,
        )
        return None
    presence = PresenceMonitor(idle_source=idle_source, platform=backend.name)
    ledger = HeldInputLedger()
    target_source = getattr(backend, "current_target", None)
    guard = CoexistenceGuard(
        presence=presence,
        release_all=lambda reason: ledger.release_all(reason=reason),
        drive_anyway=bool(coexistence_cfg.get("drive_anyway", False)),
        target_source=target_source,
    )
    logger.info(
        "coexistence: guard built for backend %r (guard_ms=%.1f, measured=%s)",
        backend.name,
        presence.guard_ms,
        presence.guard_measured,
    )
    return guard


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
