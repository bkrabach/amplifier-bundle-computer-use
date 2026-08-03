"""Explicit human resume signal for a durable coexistence halt - packaged.

`docs/coexistence.md` \u00a713 D3: "resume is manual when a console user
is present." `halt_state.py` persists a durable halt record the moment a real
human is detected touching a driven machine, so that a NEW driving session (a
fresh `mount()` - a new sub-agent, a restarted process, anything) does not
silently resume writing just because enough wall-clock time happened to pass.
The record is cleared by exactly one path: a human running this entry point,
on purpose.

Nothing on the automated tool-call path ever calls `clear_halt()` - not
`mount()`, not `_build_coexistence_guard()`, not `ComputerTool.execute()`.
If it did, "resume requires an explicit signal, not the mere passage of
time" would not be true.

Why this lives inside the package, not only as a repo-root script
-------------------------------------------------------------------
`scripts/resume_after_halt.py` (the original, checkout-only entry point) puts
`REPO_ROOT/modules/tool-computer-use` onto `sys.path` by hand - it only works
from a full git checkout. Someone who installed this tool the way the
behavior YAML actually tells them to (`pip`/`uv` installing straight from
`source: git+https://...#subdirectory=modules/tool-computer-use`) gets the
`amplifier_module_tool_computer_use` package with NO `scripts/` directory,
NO repo root, and therefore no way to run that script at all - a halt with no
time-based expiry (by design) and, for that install path, no on-ramp back.

This module is the fix: it lives INSIDE the package that already ships to
every install path, and `pyproject.toml` registers it as a console-script
entry point (`amplifier-computer-use-resume`) - so `pip install`/`uv pip
install` from the module's own subdirectory puts a real command on `PATH`,
no checkout required. `scripts/resume_after_halt.py` still works for a repo
checkout (it now just imports and calls `main()` from here, so there is one
copy of this logic, not two drifting apart).

Usage (same behavior via either entry point):
    amplifier-computer-use-resume                 # list halted backends
    amplifier-computer-use-resume linux-x11        # clear one backend
    amplifier-computer-use-resume --all            # clear every backend
"""

from __future__ import annotations

import argparse

from .halt_state import clear_halt, list_halted_platforms, load_halt


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
        print("Backends currently HALTED (require this command to resume):")
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
