"""Amplifier tool module: `computer` - Anthropic native computer-use, on Windows via WSL2.

The tool mounts under the name `computer` so the orchestrator can execute the
`tool_use` blocks Claude emits for its built-in computer tool. `hook-computer-use`
promotes this tool's declaration to the *native* Anthropic tool type on the wire and
turns screenshot markers into real image content blocks.

Without the hook the tool still works as an ordinary function tool (Claude drives it
from the JSON schema below), it just cannot show Claude the screen.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from pathlib import Path
from typing import Any

from amplifier_core.models import ToolResult

from .windows import BridgeError, WindowsBridge

logger = logging.getLogger(__name__)

__version__ = "0.1.0"

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


def _prune_shots() -> None:
    cutoff = time.time() - SHOT_TTL_SECONDS
    try:
        for old in SHOT_DIR.glob("*.png"):
            if old.stat().st_mtime < cutoff:
                old.unlink(missing_ok=True)
    except OSError:  # pragma: no cover - best-effort housekeeping
        pass


class ComputerTool:
    """Executes Anthropic computer-tool actions against the real Windows desktop."""

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        cfg = config or {}
        self._read_only = bool(cfg.get("read_only", False))
        self._bridge = WindowsBridge(
            powershell_path=cfg.get("powershell_path"),
            max_edge=int(cfg.get("max_edge", 1280)),
            max_pixels=int(cfg.get("max_pixels", 1_150_000)),
            timeout=float(cfg.get("timeout", 90.0)),
        )
        self._tool_version = str(cfg.get("tool_version", "computer_20251124"))
        self._enable_zoom = bool(cfg.get("enable_zoom", True))

    # -- Tool protocol ----------------------------------------------------------
    @property
    def name(self) -> str:
        return "computer"

    @property
    def description(self) -> str:
        return (
            "Control the user's real Windows desktop: capture the screen, move and click the "
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
        """Anthropic server-side tool definition, sized to the current display."""
        disp = self._bridge.display()
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
            summary, image_b64 = self._bridge.execute(action, input)
        except (BridgeError, ValueError) as exc:
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
        SHOT_DIR.mkdir(parents=True, exist_ok=True)
        _prune_shots()
        import base64

        path = SHOT_DIR / f"{uuid.uuid4().hex}.png"
        path.write_bytes(base64.standard_b64decode(image_b64))
        disp = self._bridge.display()
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
            "Windows desktop helpers that complement the `computer` tool: list open windows, "
            "bring a window to the front before typing into it, read the display geometry, and "
            "read or write the Windows clipboard. Use `list_windows` then `focus_window` to make "
            "sure keystrokes land in the right application. Clipboard access is the reliable way "
            "to pull exact text out of an app (select, ctrl+c, then get_clipboard)."
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
        if self._computer._read_only and action in {"focus_window", "set_clipboard"}:
            return ToolResult(
                success=False,
                error={"message": f"action {action!r} blocked: mounted read_only"},
            )
        try:
            if action in {"get_clipboard", "set_clipboard"}:
                res = self._computer._bridge.raw(action, text=input.get("text"))
                if not res.get("ok"):
                    raise BridgeError(res.get("error", f"{action} failed"))
                return ToolResult(
                    success=True,
                    output=res.get("text", "")
                    if action == "get_clipboard"
                    else "clipboard set",
                )
            summary, _ = self._computer._bridge.execute(action, input)
            return ToolResult(success=True, output=summary)
        except (BridgeError, ValueError) as exc:
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
    """Mount the `computer` tool and its `desktop` companion."""
    computer = ComputerTool(config or {})
    await coordinator.mount("tools", computer, name=computer.name)
    desktop = DesktopTool(computer)
    await coordinator.mount("tools", desktop, name=desktop.name)
    logger.info(
        "tool-computer-use mounted: 'computer' (%s) + 'desktop'", computer._tool_version
    )
    return {
        "name": "tool-computer-use",
        "version": __version__,
        "provides": ["computer", "desktop"],
        "description": "Anthropic native computer-use against the Windows desktop via WSL2",
    }
