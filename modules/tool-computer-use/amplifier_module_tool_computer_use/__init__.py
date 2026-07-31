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

import base64
import json
import logging
import time
import uuid
from pathlib import Path
from typing import Any

from amplifier_core.models import ToolResult

from .backend import Backend, BackendError
from .geometry import Display, compute_display
from .imaging import capture_scaled_b64
from .registry import NoBackendAvailable, select_backend

logger = logging.getLogger(__name__)

__version__ = "0.2.0"

#: Marker key the companion hook looks for in tool output.
MARKER = "__amplifier_computer_use__"

SHOT_DIR = Path.home() / ".amplifier" / "computer-use" / "shots"
SHOT_TTL_SECONDS = 2 * 60 * 60

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
    cutoff = time.time() - SHOT_TTL_SECONDS
    try:
        for old in SHOT_DIR.glob("*.png"):
            if old.stat().st_mtime < cutoff:
                old.unlink(missing_ok=True)
    except OSError:  # pragma: no cover - best-effort housekeeping
        pass


class ComputerTool:
    """Executes Anthropic computer-tool actions against whatever desktop the
    selected `Backend` can reach."""

    def __init__(self, backend: Backend, config: dict[str, Any] | None = None) -> None:
        cfg = config or {}
        self._read_only = bool(cfg.get("read_only", False))
        self._backend = backend
        self._max_edge = int(cfg.get("max_edge", 1280))
        self._max_pixels = int(cfg.get("max_pixels", 1_150_000))
        self._tool_version = str(cfg.get("tool_version", "computer_20251124"))
        self._enable_zoom = bool(cfg.get("enable_zoom", True))
        # D2: resolved once (by mount(), right after backend selection) and cached -
        # never touched on the request hot path. See `resolve_display`.
        self._display: Display | None = None

    # -- display resolution (D2) -------------------------------------------------
    def resolve_display(self, refresh: bool = False) -> Display:
        """Resolve and cache display geometry.

        Called once by `mount()`, right after the backend is selected. The only
        other caller is the `screen_info` action, which passes `refresh=True` as its
        explicit, deliberate refresh path (e.g. after a resolution change) - the
        *only* place this ever talks to the backend again after mount.
        """
        if self._display is not None and not refresh:
            return self._display
        geo = self._backend.screen_geometry()
        mw, mh = compute_display(
            geo.width, geo.height, self._max_edge, self._max_pixels
        )
        self._display = Display(
            geo.width, geo.height, mw, mh, geo.origin_x, geo.origin_y
        )
        logger.info(
            "computer-use display: screen %dx%d -> model %dx%d",
            geo.width,
            geo.height,
            mw,
            mh,
        )
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
        return {
            "computer_20251124": "computer-use-2025-11-24",
            "computer_20250124": "computer-use-2025-01-24",
            "computer_20241022": "computer-use-2024-10-22",
        }.get(self._tool_version, "computer-use-2025-11-24")

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
            b64 = capture_scaled_b64(
                backend, disp, None, self._max_edge, self._max_pixels
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
            return f"cursor at [{mx}, {my}] (model space)", None

        if action == "screen_info":
            # The one deliberate refresh path: re-resolves and re-caches geometry,
            # so a resolution change is picked up without touching the hot path.
            fresh = self.resolve_display(refresh=True)
            return (
                json.dumps(
                    {
                        "screen_width": fresh.screen_width,
                        "screen_height": fresh.screen_height,
                        "model_width": fresh.model_width,
                        "model_height": fresh.model_height,
                    }
                ),
                None,
            )

        if action == "list_windows":
            result = backend.list_windows()
            visible = [w for w in result.windows if not w.minimized][:25]
            listing = "\n".join(f"  [{w.handle}] {w.title}" for w in visible)
            return f"visible windows (foreground={result.foreground}):\n{listing}", None

        if action == "focus_window":
            handle = params.get("handle")
            if not handle:
                raise ValueError("action 'focus_window' requires 'handle'")
            backend.focus_window(str(handle))
            return f"focused window {handle}", None

        if action in _CLICK_ACTIONS:
            button, count = _CLICK_ACTIONS[action]
            x, y = coord() if params.get("coordinate") is not None else (None, None)
            backend.click(x, y, button=button, count=count)
            where = (
                f" at {params.get('coordinate')}" if params.get("coordinate") else ""
            )
            return f"{action}{where}", None

        if action == "mouse_move":
            x, y = coord()
            backend.move(x, y)
            return f"mouse_move at {params.get('coordinate')}", None

        if action == "left_mouse_down":
            x, y = coord() if params.get("coordinate") is not None else (None, None)
            backend.mouse_down(x, y, "left")
            return "left_mouse_down", None

        if action == "left_mouse_up":
            x, y = coord() if params.get("coordinate") is not None else (None, None)
            backend.mouse_up(x, y, "left")
            return "left_mouse_up", None

        if action == "left_click_drag":
            start = (
                coord("start_coordinate") if params.get("start_coordinate") else None
            )
            end = coord()
            backend.drag(start, end)
            return f"dragged to {params.get('coordinate')}", None

        if action == "scroll":
            x, y = coord() if params.get("coordinate") is not None else (None, None)
            direction = params.get("scroll_direction") or params.get("direction")
            if not direction:
                raise ValueError("action 'scroll' requires 'scroll_direction'")
            amount = int(params.get("scroll_amount") or params.get("amount") or 3)
            backend.scroll(x, y, str(direction), amount)
            return f"scrolled {direction} x{amount}", None

        if action in {"key", "hold_key"}:
            if not text:
                raise ValueError(f"action {action!r} requires 'text'")
            if action == "key":
                backend.key(str(text))
            else:
                backend.hold_key(str(text), float(params.get("duration") or 1.0))
            return f"pressed {text}", None

        if action == "type":
            if not text:
                raise ValueError("action 'type' requires 'text'")
            body = str(text)
            backend.type_text(body)
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
            summary, image_b64 = self._run(action, input)
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
        SHOT_DIR.mkdir(parents=True, exist_ok=True)
        _prune_shots()

        path = SHOT_DIR / f"{uuid.uuid4().hex}.png"
        path.write_bytes(base64.standard_b64decode(image_b64))
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
            "bring a window to the front before typing into it, read the display geometry, and "
            "read or write the clipboard. Use `list_windows` then `focus_window` to make "
            "sure keystrokes land in the right application. Clipboard access is the reliable way "
            "to pull exact text out of an app (select, copy, then get_clipboard)."
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
            if action == "get_clipboard":
                return ToolResult(success=True, output=backend.get_clipboard())
            if action == "set_clipboard":
                backend.set_clipboard(str(input.get("text") or ""))
                return ToolResult(success=True, output="clipboard set")
            summary, _ = self._computer._run(action, input)
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

    computer = ComputerTool(backend, cfg)
    # D2: resolve display once, here, before the tool ever answers a provider
    # request - not lazily on the first `native_tool_spec` read.
    computer.resolve_display()

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
