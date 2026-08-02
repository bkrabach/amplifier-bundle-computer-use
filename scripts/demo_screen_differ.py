#!/usr/bin/env python3
"""Demonstration: `tools.screen_differ` detecting a real screen change.

Captures the real root window of a (headless) X11 display via ImageMagick's
`import` before and after opening a real `zenity` dialog, then runs
`tools.screen_differ.diff_bytes` against the two real PNGs - no synthetic
images, no mocked capture.

    Xvfb :99 -screen 0 1280x800x24 -nolisten tcp &
    DISPLAY=:99 .venv/bin/python scripts/demo_screen_differ.py
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.screen_differ import diff_bytes, format_summary


def _capture(display: str) -> bytes:
    proc = subprocess.run(
        ["import", "-window", "root", "-display", display, "png:-"],
        capture_output=True,
        check=True,
    )
    return proc.stdout


def main() -> int:
    display = os.environ.get("DISPLAY", ":99")
    print(
        f"=== screen_differ demonstration against real Xvfb display {display!r} ===\n"
    )

    print("[1] capturing the real root window BEFORE opening anything...")
    before = _capture(display)
    print(f"    captured {len(before)} real PNG bytes")

    print(
        "\n[2] a second capture of the SAME empty desktop (flat-desktop control case)..."
    )
    before_again = _capture(display)
    flat_result = diff_bytes(before, before_again)
    print(format_summary(flat_result))

    print("\n[3] opening a real zenity dialog...")
    proc = subprocess.Popen(
        [
            "zenity",
            "--info",
            "--title=screen_differ demo",
            "--text=screen_differ demo",
            "--width=300",
        ],
        env={**os.environ, "DISPLAY": display},
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        time.sleep(1.5)
        if proc.poll() is not None:
            print(
                f"    zenity exited early (rc={proc.poll()}) - cannot demonstrate a real change"
            )
            return 1

        print("\n[4] capturing AFTER the dialog is on screen...")
        after = _capture(display)
        print(f"    captured {len(after)} real PNG bytes")

        print("\n[5] diffing the two REAL captures...")
        result = diff_bytes(before, after)
        print(format_summary(result))

        ok = (
            flat_result.verdict == "unchanged"
            and flat_result.distinct_colors_before <= 1
            and result.verdict == "changed"
            and result.changed_pixels > 0
        )
        print(f"\nVERDICT: {'PASS' if ok else 'FAIL'}")
        return 0 if ok else 1
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()


if __name__ == "__main__":
    sys.exit(main())
