"""Monitor selection: pick one physical monitor to scope computer-use to.

Per-monitor targeting exists because a virtual-desktop bounding box spanning
several real monitors downscales far more aggressively than any single monitor
does, and can contain large stretches of dead space where no monitor exists at
all. Measured on the real machine that motivated this: a 9626x4323 bounding box
around four 3840x2160 monitors, ~20% of which (8.4 MPix) is dead space between
non-aligned monitor origins. Scoping to one 3840x2160 monitor instead roughly
halves the linear downscale factor (7.52x -> 3.00x) and eliminates the dead
space entirely.

This module is pure selection logic - no I/O, no backend-specific knowledge -
so it is shared unchanged by every backend's monitor list.
"""

from __future__ import annotations

from .backend import BackendError, MonitorInfo

#: Sentinel `target_monitor` config value that opts OUT of per-monitor scoping
#: entirely and restores the old virtual-desktop-bounding-box behavior. Not the
#: default - see `ComputerTool.resolve_display` in `__init__.py`.
VIRTUAL_DESKTOP = "virtual-desktop"

#: Sentinel meaning "whichever monitor the OS reports as primary" - the default
#: when `target_monitor` is unset.
PRIMARY = "primary"


def select_monitor(monitors: list[MonitorInfo], target: str | None) -> MonitorInfo:
    """Pick one monitor from `monitors` according to `target`.

    `target` is one of:
      - `None` or `"primary"`: the monitor the backend reports as primary.
      - an explicit monitor `id`, as returned by `Backend.list_monitors()`.

    Raises `BackendError` (never falls back to a guess) if:
      - `monitors` is empty - there is nothing to select from, and
      - `target` is an explicit id that matches no enumerated monitor - the
        error lists every id that *was* found, so a config typo is immediately
        diagnosable instead of silently targeting the wrong screen.

    The one deliberate exception to "never guess": if `target` requests
    `"primary"` (explicitly or by default) and *no* monitor is flagged primary,
    the first enumerated monitor is used, not raised on. This is not a
    synthesized fallback - every candidate came from a real `list_monitors()`
    call - it is a deterministic, honest tie-break among equally real monitors
    when the backend simply did not report which one is primary (observed on
    some Linux window managers that never set RandR's primary-output flag).
    Callers should log when this tie-break fires so it stays diagnosable.
    """
    if not monitors:
        raise BackendError("no monitors enumerated; cannot select a target monitor")

    if target in (None, PRIMARY):
        for mon in monitors:
            if mon.primary:
                return mon
        return monitors[0]

    for mon in monitors:
        if mon.id == target:
            return mon

    available = ", ".join(repr(mon.id) for mon in monitors)
    raise BackendError(
        f"target_monitor {target!r} not found among enumerated monitors "
        f"({available}); check the 'target_monitor' config value or the "
        "monitor id passed to desktop.select_monitor"
    )
