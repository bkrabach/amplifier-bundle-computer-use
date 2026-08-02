#!/usr/bin/env python3
"""Explicit human resume signal for a durable coexistence halt.

`docs/designs/coexistence.md` \u00a713 D3: "resume is manual when a console user
is present." `halt_state.py` (`modules/tool-computer-use/...`) persists a
durable halt record the moment a real human is detected touching a driven
machine, so that a NEW driving session (a fresh `mount()` - a new sub-agent,
a restarted process, anything) does not silently resume writing just because
enough wall-clock time happened to pass. The record is cleared by exactly
one path: this script, run by a human, on purpose.

Nothing on the automated tool-call path ever calls `clear_halt()` - not
`mount()`, not `_build_coexistence_guard()`, not `ComputerTool.execute()`.
If it did, "resume requires an explicit signal, not the mere passage of
time" would not be true.

Usage:
    python scripts/resume_after_halt.py                 # list halted backends
    python scripts/resume_after_halt.py linux-x11        # clear one backend
    python scripts/resume_after_halt.py --all            # clear every backend
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "modules" / "tool-computer-use"))

from amplifier_module_tool_computer_use.halt_state import (  # noqa: E402
    clear_halt,
    list_halted_platforms,
    load_halt,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "platform",
        nargs="?",
        help="Backend/platform name to clear (e.g. 'linux-x11'). Omit to list.",
    )
    parser.add_argument(
        "--all", action="store_true", help="Clear every backend's halt record."
    )
    args = parser.parse_args()

    halted = list_halted_platforms()

    if args.all:
        if not halted:
            print("No durable halt records to clear.")
            return 0
        for platform in halted:
            record = load_halt(platform)
            cleared = clear_halt(platform)
            reason = record.reason if record else "?"
            print(f"cleared {platform!r} (was: {reason}) -> {cleared}")
        return 0

    if args.platform is None:
        if not halted:
            print("No durable halt records - every backend may drive freely.")
            return 0
        print("Backends currently HALTED (require this script to resume):")
        for platform in halted:
            record = load_halt(platform)
            reason = record.reason if record else "?"
            print(f"  {platform}: {reason}")
        return 0

    record = load_halt(args.platform)
    if record is None:
        print(f"{args.platform!r} has no durable halt record - nothing to clear.")
        return 0
    print(f"{args.platform!r} is halted: {record.reason}")
    cleared = clear_halt(args.platform)
    print(f"cleared: {cleared}. Future sessions for {args.platform!r} may drive again.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
