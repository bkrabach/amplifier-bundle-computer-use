"""WSL2 -> Windows desktop backend.

Every action crosses the WSL2/Win32 boundary through `bridge.ps1` via `powershell.exe`
interop - there is no in-process alternative for this boundary, so every method on
this backend necessarily subprocesses. Requests are handed over as a temp JSON file
(never as a -Command string) so no user text can be interpolated into a PowerShell
expression.

This module implements the `Backend` protocol (see `backend.py`). It is a mechanical
refactor of the original `WindowsBridge`: the wire format, the PowerShell invocation,
and every action's behavior are unchanged. It could not be exercised on this box (no
Windows target was available) - see the top-level report for what remains unverified.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from .backend import BackendError, ProbeResult, ScreenGeometry, WindowInfo, WindowList

logger = logging.getLogger(__name__)

BRIDGE_PS1 = Path(__file__).parent / "bridge.ps1"

#: Anthropic recommends staying at/below WXGA; larger costs tokens and *loses* accuracy.
#: (Enforced by the caller via `ComputerTool`/`compute_display`, not by this backend.)

_CLICK_ACTIONS = {
    ("left", 1): "left_click",
    ("right", 1): "right_click",
    ("middle", 1): "middle_click",
    ("left", 2): "double_click",
    ("left", 3): "triple_click",
}


def _which_powershell(configured: str | None) -> str | None:
    if configured:
        return configured
    found = shutil.which("powershell.exe")
    if found:
        return found
    fallback = "/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe"
    if Path(fallback).exists():
        return fallback
    return None


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
        raise BackendError(f"wslpath {flag} failed for {path!r}: {proc.stderr.strip()}")
    return proc.stdout.strip()


class WindowsBackend:
    """Executes computer-use actions against a real Windows desktop via PowerShell."""

    name = "windows-wsl2"

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        cfg = config or {}
        self._ps_config = cfg.get("powershell_path")
        self._ps: str | None = None
        self._timeout = float(cfg.get("timeout", 90.0))

    # -- capability probe (D1) ---------------------------------------------------
    def probe(self) -> ProbeResult:
        """Cheap PATH lookups only - no subprocess, no bridge round trip.

        This is the fix for D1: previously `mount()` unconditionally registered the
        `computer`/`desktop` tools, so a Linux box with no `powershell.exe` mounted a
        tool that could not possibly work. Now this check runs first.
        """
        if shutil.which("wslpath") is None:
            return ProbeResult(False, "wslpath not on PATH (not running under WSL2?)")
        ps = _which_powershell(self._ps_config)
        if ps is None:
            return ProbeResult(
                False,
                "powershell.exe not found; WSL<->Windows interop must be enabled "
                "(on by default in WSL2), or set tool config 'powershell_path'",
            )
        self._ps = ps
        return ProbeResult(True)

    @property
    def powershell(self) -> str:
        if self._ps is None:
            ps = _which_powershell(self._ps_config)
            if ps is None:
                raise BackendError(
                    "powershell.exe not found. WSL<->Windows interop must be enabled "
                    "(it is on by default in WSL2). Set the tool config key "
                    "'powershell_path' to override."
                )
            self._ps = ps
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
            raise BackendError(
                f"action {action!r} timed out after {timeout or self._timeout}s"
            ) from exc
        finally:
            Path(req_path).unlink(missing_ok=True)

        lines = [ln for ln in (proc.stdout or "").splitlines() if ln.strip()]
        if not lines:
            raise BackendError(
                f"bridge produced no output for {action!r} (rc={proc.returncode}): "
                f"{(proc.stderr or '').strip()[:400]}"
            )
        try:
            return json.loads(lines[-1])
        except json.JSONDecodeError as exc:
            raise BackendError(
                f"bridge returned non-JSON for {action!r}: {lines[-1][:300]}"
            ) from exc

    # -- Backend protocol ---------------------------------------------------------
    def screen_geometry(self) -> ScreenGeometry:
        info = self.raw("screen_info", timeout=30)
        if not info.get("ok"):
            raise BackendError(f"screen_info failed: {info.get('error')}")
        return ScreenGeometry(
            int(info["width"]),
            int(info["height"]),
            int(info.get("x", 0)),
            int(info.get("y", 0)),
        )

    def capture(self, region: tuple[int, int, int, int] | None = None) -> bytes:
        if region:
            x1, y1, x2, y2 = region
            res = self.raw(
                "zoom", coordinate=[x1, y1, max(x1 + 8, x2), max(y1 + 8, y2)]
            )
        else:
            res = self.raw("screenshot")
        if not res.get("ok"):
            raise BackendError(f"capture failed: {res.get('error')}")
        wsl_path = _translate(res["path"], "-u")
        try:
            return Path(wsl_path).read_bytes()
        finally:
            Path(wsl_path).unlink(missing_ok=True)

    def cursor_position(self) -> tuple[int, int]:
        res = self.raw("cursor_position")
        if not res.get("ok"):
            raise BackendError(res.get("error", "cursor_position failed"))
        sx, sy = res["coordinate"]
        return int(sx), int(sy)

    def move(self, x: int, y: int) -> None:
        res = self.raw("mouse_move", coordinate=[x, y])
        if not res.get("ok"):
            raise BackendError(res.get("error", "mouse_move failed"))

    def click(
        self, x: int | None, y: int | None, button: str = "left", count: int = 1
    ) -> None:
        action = _CLICK_ACTIONS.get((button, count))
        if action is None:
            raise BackendError(f"unsupported click: button={button!r} count={count!r}")
        kwargs: dict[str, Any] = {}
        if x is not None and y is not None:
            kwargs["coordinate"] = [x, y]
        res = self.raw(action, **kwargs)
        if not res.get("ok"):
            raise BackendError(res.get("error", f"{action} failed"))

    def mouse_down(self, x: int | None, y: int | None, button: str = "left") -> None:
        if button != "left":
            raise BackendError(f"mouse_down only supports 'left' (got {button!r})")
        kwargs: dict[str, Any] = (
            {"coordinate": [x, y]} if x is not None and y is not None else {}
        )
        res = self.raw("left_mouse_down", **kwargs)
        if not res.get("ok"):
            raise BackendError(res.get("error", "left_mouse_down failed"))

    def mouse_up(self, x: int | None, y: int | None, button: str = "left") -> None:
        if button != "left":
            raise BackendError(f"mouse_up only supports 'left' (got {button!r})")
        kwargs: dict[str, Any] = (
            {"coordinate": [x, y]} if x is not None and y is not None else {}
        )
        res = self.raw("left_mouse_up", **kwargs)
        if not res.get("ok"):
            raise BackendError(res.get("error", "left_mouse_up failed"))

    def drag(self, start: tuple[int, int] | None, end: tuple[int, int]) -> None:
        kwargs: dict[str, Any] = {"coordinate": list(end)}
        if start is not None:
            kwargs["start_coordinate"] = list(start)
        res = self.raw("left_click_drag", **kwargs)
        if not res.get("ok"):
            raise BackendError(res.get("error", "drag failed"))

    def scroll(self, x: int | None, y: int | None, direction: str, amount: int) -> None:
        kwargs: dict[str, Any] = {
            "scroll_direction": direction,
            "scroll_amount": amount,
        }
        if x is not None and y is not None:
            kwargs["coordinate"] = [x, y]
        res = self.raw("scroll", **kwargs)
        if not res.get("ok"):
            raise BackendError(res.get("error", "scroll failed"))

    def key(self, combo: str) -> None:
        res = self.raw("key", text=combo)
        if not res.get("ok"):
            raise BackendError(res.get("error", "key failed"))

    def hold_key(self, combo: str, duration: float) -> None:
        res = self.raw("hold_key", text=combo, duration=duration)
        if not res.get("ok"):
            raise BackendError(res.get("error", "hold_key failed"))

    def type_text(self, text: str) -> None:
        res = self.raw(
            "type", text=text, timeout=max(self._timeout, 10 + len(text) * 0.05)
        )
        if not res.get("ok"):
            raise BackendError(res.get("error", "type failed"))

    def list_windows(self) -> WindowList:
        res = self.raw("list_windows")
        if not res.get("ok"):
            raise BackendError(res.get("error", "list_windows failed"))
        windows = [
            WindowInfo(
                str(w["handle"]), str(w["title"]), bool(w.get("minimized", False))
            )
            for w in res.get("windows", [])
        ]
        return WindowList(windows, res.get("foreground"))

    def focus_window(self, handle: str) -> None:
        res = self.raw("focus_window", handle=handle)
        if not res.get("ok"):
            raise BackendError(res.get("error", "focus_window failed"))

    def get_clipboard(self) -> str:
        res = self.raw("get_clipboard")
        if not res.get("ok"):
            raise BackendError(res.get("error", "get_clipboard failed"))
        return res.get("text", "")

    def set_clipboard(self, text: str) -> None:
        res = self.raw("set_clipboard", text=text)
        if not res.get("ok"):
            raise BackendError(res.get("error", "set_clipboard failed"))

    def close(self) -> None:
        pass  # no persistent resources held (each action is its own subprocess)
