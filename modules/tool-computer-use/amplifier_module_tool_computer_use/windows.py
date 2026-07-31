"""WSL2 -> Windows desktop bridge.

Every action is executed by `bridge.ps1` via `powershell.exe` interop. Requests are
handed over as a temp JSON file (never as a -Command string) so no user text can be
interpolated into a PowerShell expression.

Coordinate spaces
-----------------
MODEL space  - what Claude sees and emits. Matches the downscaled screenshot exactly.
SCREEN space - real physical pixels of the Windows virtual desktop.

`Display.to_screen()` is the only place the two are converted.
"""

from __future__ import annotations

import base64
import io
import json
import logging
import shutil
import subprocess
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

BRIDGE_PS1 = Path(__file__).parent / "bridge.ps1"

#: Anthropic recommends staying at/below WXGA. Larger costs tokens and *loses* accuracy.
DEFAULT_MAX_EDGE = 1280
DEFAULT_MAX_PIXELS = 1_150_000


class BridgeError(RuntimeError):
    """The PowerShell bridge could not be reached or failed outside a known action."""


def _which_powershell(configured: str | None) -> str:
    if configured:
        return configured
    found = shutil.which("powershell.exe")
    if found:
        return found
    fallback = "/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe"
    if Path(fallback).exists():
        return fallback
    raise BridgeError(
        "powershell.exe not found. WSL<->Windows interop must be enabled (it is on by "
        "default in WSL2). Set the tool config key 'powershell_path' to override."
    )


def _translate(path: str, flag: str) -> str:
    proc = subprocess.run(
        ["wslpath", flag, str(path)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=15,
        check=False,
    )
    if proc.returncode != 0:
        raise BridgeError(f"wslpath {flag} failed for {path!r}: {proc.stderr.strip()}")
    return proc.stdout.strip()


@dataclass(frozen=True)
class Display:
    """Mapping between the real desktop and the image the model actually sees."""

    screen_width: int
    screen_height: int
    model_width: int
    model_height: int
    origin_x: int = 0
    origin_y: int = 0

    @property
    def scale(self) -> float:
        return self.screen_width / self.model_width

    def to_screen(self, x: float, y: float) -> tuple[int, int]:
        """MODEL coords -> SCREEN coords, clamped to the desktop."""
        sx = round(x * self.scale) + self.origin_x
        sy = round(y * (self.screen_height / self.model_height)) + self.origin_y
        sx = max(self.origin_x, min(sx, self.origin_x + self.screen_width - 1))
        sy = max(self.origin_y, min(sy, self.origin_y + self.screen_height - 1))
        return sx, sy


def compute_display(
    screen_w: int, screen_h: int, max_edge: int, max_pixels: int
) -> tuple[int, int]:
    """Largest model-space size that preserves aspect ratio and respects both budgets."""
    scale = min(
        1.0,
        max_edge / max(screen_w, screen_h),
        (max_pixels / (screen_w * screen_h)) ** 0.5,
    )
    # Even dimensions avoid half-pixel rounding drift when mapping coordinates back.
    return max(2, int(screen_w * scale) // 2 * 2), max(
        2, int(screen_h * scale) // 2 * 2
    )


class WindowsBridge:
    """Executes computer-use actions against the real Windows desktop."""

    def __init__(
        self,
        powershell_path: str | None = None,
        max_edge: int = DEFAULT_MAX_EDGE,
        max_pixels: int = DEFAULT_MAX_PIXELS,
        timeout: float = 90.0,
    ) -> None:
        self._ps_config = powershell_path
        self._ps: str | None = None
        self._max_edge = max_edge
        self._max_pixels = max_pixels
        self._timeout = timeout
        self._display: Display | None = None
        self._lock = threading.Lock()

    # -- plumbing ---------------------------------------------------------------
    @property
    def powershell(self) -> str:
        if self._ps is None:
            self._ps = _which_powershell(self._ps_config)
        return self._ps

    def raw(
        self, action: str, timeout: float | None = None, **params: Any
    ) -> dict[str, Any]:
        payload = {
            "action": action,
            **{k: v for k, v in params.items() if v is not None},
        }
        with tempfile.NamedTemporaryFile(
            "w", suffix=".json", delete=False, encoding="utf-8"
        ) as fh:
            json.dump(payload, fh)
            req_path = fh.name
        try:
            proc = subprocess.run(
                [
                    self.powershell,
                    "-NoProfile",
                    "-NonInteractive",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    _translate(str(BRIDGE_PS1), "-w"),
                    "-RequestFile",
                    _translate(req_path, "-w"),
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout or self._timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise BridgeError(
                f"action {action!r} timed out after {timeout or self._timeout}s"
            ) from exc
        finally:
            Path(req_path).unlink(missing_ok=True)

        lines = [ln for ln in (proc.stdout or "").splitlines() if ln.strip()]
        if not lines:
            raise BridgeError(
                f"bridge produced no output for {action!r} (rc={proc.returncode}): {(proc.stderr or '').strip()[:400]}"
            )
        try:
            return json.loads(lines[-1])
        except json.JSONDecodeError as exc:
            raise BridgeError(
                f"bridge returned non-JSON for {action!r}: {lines[-1][:300]}"
            ) from exc

    # -- display ----------------------------------------------------------------
    def display(self, refresh: bool = False) -> Display:
        with self._lock:
            if self._display is not None and not refresh:
                return self._display
            info = self.raw("screen_info", timeout=30)
            if not info.get("ok"):
                raise BridgeError(f"screen_info failed: {info.get('error')}")
            sw, sh = int(info["width"]), int(info["height"])
            mw, mh = compute_display(sw, sh, self._max_edge, self._max_pixels)
            self._display = Display(
                sw, sh, mw, mh, int(info.get("x", 0)), int(info.get("y", 0))
            )
            logger.info(
                "computer-use display: screen %dx%d -> model %dx%d", sw, sh, mw, mh
            )
            return self._display

    # -- screenshots ------------------------------------------------------------
    def screenshot_b64(self, region: list[int] | None = None) -> str:
        """Capture (optionally a MODEL-space region) and return base64 PNG in model scale."""
        from PIL import Image  # imported lazily so import errors surface as tool errors

        disp = self.display()
        if region:
            x1, y1 = disp.to_screen(region[0], region[1])
            x2, y2 = disp.to_screen(region[2], region[3])
            res = self.raw(
                "zoom", coordinate=[x1, y1, max(x1 + 8, x2), max(y1 + 8, y2)]
            )
        else:
            res = self.raw("screenshot")
        if not res.get("ok"):
            raise BridgeError(f"capture failed: {res.get('error')}")

        wsl_path = _translate(res["path"], "-u")
        with Image.open(wsl_path) as img:
            img = img.convert("RGB")
            if region:
                # Zoom returns a crop at native resolution: shrink only if oversized.
                tw, th = compute_display(
                    img.width, img.height, self._max_edge, self._max_pixels
                )
                if (tw, th) != (img.width, img.height):
                    img = img.resize((tw, th), Image.Resampling.LANCZOS)
            else:
                img = img.resize(
                    (disp.model_width, disp.model_height), Image.Resampling.LANCZOS
                )
            buf = io.BytesIO()
            img.save(buf, format="PNG")
        Path(wsl_path).unlink(missing_ok=True)
        return base64.standard_b64encode(buf.getvalue()).decode()

    # -- actions ----------------------------------------------------------------
    def execute(self, action: str, params: dict[str, Any]) -> tuple[str, str | None]:
        """Run one Anthropic computer-tool action.

        Returns (text_summary, base64_png_or_None).
        """
        disp = self.display()

        def coord(key: str = "coordinate") -> list[int]:
            raw = params.get(key)
            if not isinstance(raw, (list, tuple)) or len(raw) < 2:
                raise ValueError(f"action {action!r} requires {key} as [x, y]")
            return list(disp.to_screen(float(raw[0]), float(raw[1])))

        text = params.get("text") or params.get("key")

        if action == "screenshot":
            return "screenshot captured", self.screenshot_b64()

        if action == "zoom":
            # Models reach for `region` about as often as `coordinate`, and sometimes
            # split it across start_coordinate/coordinate. Accept all three rather than
            # burning a turn on a schema correction.
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
            return f"zoomed to region {list(raw)}", self.screenshot_b64(
                region=[int(v) for v in raw]
            )

        if action == "cursor_position":
            res = self.raw("cursor_position")
            if not res.get("ok"):
                raise BridgeError(res.get("error", "cursor_position failed"))
            sx, sy = res["coordinate"]
            return (
                f"cursor at [{int(sx / disp.scale)}, {int(sy / disp.scale)}] (model space)",
                None,
            )

        if action in {"screen_info", "list_windows", "focus_window"}:
            res = self.raw(action, handle=params.get("handle"))
            if not res.get("ok"):
                raise BridgeError(res.get("error", f"{action} failed"))
            if action == "list_windows":
                wins = [w for w in res.get("windows", []) if not w.get("minimized")][
                    :25
                ]
                listing = "\n".join(f"  [{w['handle']}] {w['title']}" for w in wins)
                return (
                    f"visible windows (foreground={res.get('foreground')}):\n{listing}",
                    None,
                )
            return json.dumps(res), None

        simple = {
            "left_click",
            "right_click",
            "middle_click",
            "double_click",
            "triple_click",
            "mouse_move",
            "left_mouse_down",
            "left_mouse_up",
        }
        if action in simple:
            kwargs: dict[str, Any] = {}
            if params.get("coordinate") is not None:
                kwargs["coordinate"] = coord()
            res = self.raw(action, **kwargs)
            if not res.get("ok"):
                raise BridgeError(res.get("error", f"{action} failed"))
            where = (
                f" at {params.get('coordinate')}" if params.get("coordinate") else ""
            )
            return f"{action}{where}", None

        if action == "left_click_drag":
            start = (
                coord("start_coordinate") if params.get("start_coordinate") else None
            )
            res = self.raw(
                "left_click_drag", start_coordinate=start, coordinate=coord()
            )
            if not res.get("ok"):
                raise BridgeError(res.get("error", "drag failed"))
            return f"dragged to {params.get('coordinate')}", None

        if action == "scroll":
            res = self.raw(
                "scroll",
                coordinate=coord() if params.get("coordinate") else None,
                scroll_direction=params.get("scroll_direction")
                or params.get("direction"),
                scroll_amount=params.get("scroll_amount") or params.get("amount") or 3,
            )
            if not res.get("ok"):
                raise BridgeError(res.get("error", "scroll failed"))
            return f"scrolled {res.get('direction')} x{res.get('clicks')}", None

        if action in {"key", "hold_key"}:
            if not text:
                raise ValueError(f"action {action!r} requires 'text'")
            res = self.raw(action, text=str(text), duration=params.get("duration"))
            if not res.get("ok"):
                raise BridgeError(res.get("error", f"{action} failed"))
            return f"pressed {text}", None

        if action == "type":
            if not text:
                raise ValueError("action 'type' requires 'text'")
            body = str(text)
            res = self.raw(
                "type", text=body, timeout=max(self._timeout, 10 + len(body) * 0.05)
            )
            if not res.get("ok"):
                raise BridgeError(res.get("error", "type failed"))
            return f"typed {len(body)} characters", None

        if action == "wait":
            res = self.raw("wait", duration=params.get("duration", 1.0))
            if not res.get("ok"):
                raise BridgeError(res.get("error", "wait failed"))
            return f"waited {res.get('duration')}s", None

        raise ValueError(f"unsupported action {action!r}")
