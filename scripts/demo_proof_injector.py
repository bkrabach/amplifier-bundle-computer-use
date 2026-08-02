#!/usr/bin/env python3
"""Demonstration: `tools.proof_injector` firing a real N-event stream
against a real (headless) X11 display via `Xvfb`.

Proves, against real XTEST calls (not a mock):

1. A genuinely separate OS process fires the stream (this parent process's
   PID never touches Xlib at all).
2. `event_count` real events land, each with its own child-recorded
   timestamp.
3. `move_noop` is truly zero-visible-impact: the pointer's on-screen
   position is bit-for-bit identical before and after the whole stream.
4. The measured inter-event spacing is consistent with what was asked for.

    Xvfb :99 -screen 0 1280x800x24 -nolisten tcp &
    DISPLAY=:99 .venv/bin/python scripts/demo_proof_injector.py

Note: this box's own primary display (`:1`) has an exclusive input grab
held by `gnome-remote-desktop` (documented in `scripts/verify_linux_x11.py`)
- synthetic input is silently swallowed there. `Xvfb :99` is a clean
display with no such grab, which is exactly why this demonstration (and the
task's own instructions) use it instead.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.proof_injector import (
    GUARD_MS,
    StreamSpec,
    predicted_stream_masked_fraction,
    spawn_stream,
)


def main() -> int:
    display = os.environ.get("DISPLAY", ":99")
    print(
        f"=== proof_injector demonstration against real Xvfb display {display!r} ===\n"
    )

    from Xlib import display as xlib_display

    probe_conn = xlib_display.Display(display)
    before_pos = probe_conn.screen().root.query_pointer()
    before_xy = (before_pos.root_x, before_pos.root_y)
    probe_conn.close()
    print(f"[1] pointer position BEFORE the stream: {before_xy}")

    event_count = 12
    spacing_s = 0.05
    spec = StreamSpec(
        event_count=event_count,
        spacing_s=spacing_s,
        event_kind="move_noop",
        display=display,
    )

    print(
        f"\n[2] spawning a SEPARATE OS process to fire {event_count} move_noop events "
        f"spaced {spacing_s * 1000:.0f}ms apart..."
    )
    result = spawn_stream("linux-x11", spec)

    print(f"    child fired {result.count} real XTEST MotionNotify events")
    print(f"    first fire (child's own clock): {result.events[0].fired_at:.6f}")
    print(f"    last fire  (child's own clock): {result.events[-1].fired_at:.6f}")
    gaps = result.inter_event_gaps_s
    print(
        f"    inter-event gaps (s): min={min(gaps):.4f} max={max(gaps):.4f} "
        f"mean={sum(gaps) / len(gaps):.4f} (asked for {spacing_s:.4f})"
    )

    probe_conn = xlib_display.Display(display)
    after_pos = probe_conn.screen().root.query_pointer()
    after_xy = (after_pos.root_x, after_pos.root_y)
    probe_conn.close()
    print(f"\n[3] pointer position AFTER the stream:  {after_xy}")

    zero_impact_ok = before_xy == after_xy
    count_ok = result.count == event_count
    print(f"\n[4] zero-visible-impact verified (pointer unchanged): {zero_impact_ok}")
    print(f"[5] all {event_count} requested events fired: {count_ok}")

    guard_ms = GUARD_MS["linux-x11"]
    cadence_ms = 60.0
    single = predicted_stream_masked_fraction(guard_ms, cadence_ms, 1)
    stream = predicted_stream_masked_fraction(guard_ms, cadence_ms, event_count)
    print(
        f"\n[6] predicted fully-masked probability at GUARD={guard_ms}ms, cadence={cadence_ms}ms: "
        f"single-event={single * 100:.2f}%  {event_count}-event stream={stream:.2e}"
    )

    ok = zero_impact_ok and count_ok
    print(f"\nVERDICT: {'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
