#!/usr/bin/env python3
"""Explicit human resume signal for a durable coexistence halt - checkout entry point.

`docs/designs/coexistence.md` \u00a713 D3: "resume is manual when a console user
is present." `halt_state.py` (`modules/tool-computer-use/...`) persists a
durable halt record the moment a real human is detected touching a driven
machine, so that a NEW driving session (a fresh `mount()` - a new sub-agent,
a restarted process, anything) does not silently resume writing just because
enough wall-clock time happened to pass. The record is cleared by exactly
one path: a human running this entry point, on purpose.

**If you installed this tool the normal way** - `pip`/`uv` installing straight
from the behavior YAML's `source: git+https://...#subdirectory=modules/tool-computer-use`
- you have no repo checkout, and this SCRIPT FILE does not exist on your
machine at all. Use the console script that ships with the package instead;
it is the same logic, reachable with no checkout:

    amplifier-computer-use-resume                 # list halted backends
    amplifier-computer-use-resume linux-x11        # clear one backend
    amplifier-computer-use-resume --all            # clear every backend

This file remains for anyone working from a full git checkout (the
`CONTRIBUTING.md` dev setup): it puts `modules/tool-computer-use` on
`sys.path` and delegates to the SAME `main()` the console script calls, so
there is exactly one copy of this logic, not two that can drift apart.

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

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "modules" / "tool-computer-use"))

from amplifier_module_tool_computer_use.resume_cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
