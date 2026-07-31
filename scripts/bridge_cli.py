"""Manual driver for bridge.ps1 - diagnostics without starting an Amplifier session.

    python scripts/bridge_cli.py screen_info
    python scripts/bridge_cli.py list_windows
    python scripts/bridge_cli.py screenshot
    python scripts/bridge_cli.py left_click '{"coordinate": [1920, 1080]}'

Coordinates here are PHYSICAL screen pixels, not model-space.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from pathlib import Path

BRIDGE = (
    Path(__file__).resolve().parents[1]
    / "modules"
    / "tool-computer-use"
    / "amplifier_module_tool_computer_use"
    / "bridge.ps1"
)
_PS = (
    shutil.which("powershell.exe")
    or "/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe"
)


def _winpath(p: str) -> str:
    return subprocess.run(
        ["wslpath", "-w", str(p)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=10,
        check=False,
    ).stdout.strip()


def _wslpath(p: str) -> str:
    return subprocess.run(
        ["wslpath", "-u", str(p)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=10,
        check=False,
    ).stdout.strip()


def act(action: str, timeout: float = 60.0, **params):
    payload = {"action": action, **{k: v for k, v in params.items() if v is not None}}
    with tempfile.NamedTemporaryFile(
        "w", suffix=".json", delete=False, encoding="utf-8"
    ) as f:
        json.dump(payload, f)
        req = f.name
    try:
        proc = subprocess.run(
            [
                _PS,
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                _winpath(str(BRIDGE)),
                "-RequestFile",
                _winpath(req),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
        out = (proc.stdout or "").strip().splitlines()
        if not out:
            return {
                "ok": False,
                "error": f"no output (rc={proc.returncode}): {proc.stderr.strip()[:400]}",
            }
        res = json.loads(out[-1])
        if res.get("path"):
            res["wsl_path"] = _wslpath(res["path"])
        return res
    finally:
        Path(req).unlink(missing_ok=True)


if __name__ == "__main__":
    import sys

    print(
        json.dumps(
            act(sys.argv[1], **json.loads(sys.argv[2]) if len(sys.argv) > 2 else {}),
            indent=2,
        )[:2000]
    )
