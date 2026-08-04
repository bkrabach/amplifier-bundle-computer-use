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


def attribute_monitor(
    rect: tuple[int, int, int, int] | None, monitors: list[MonitorInfo]
) -> str | None:
    """Which monitor (by `MonitorInfo.id`) a window's `rect` is actually on.

    This is the join that was missing and caused a real incident: capture is
    scoped to one monitor at a time (see this module's own docstring), but
    `list_windows()` reported no monitor or geometry data at all - so a
    `focus_window` call that raised a window on a DIFFERENT monitor than the
    one being captured looked, from the caller's side, identical to a
    `focus_window` call that silently did nothing. Three sessions in a row
    misdiagnosed a working `focus_window` as broken because of exactly this
    gap.

    Picks the monitor with the largest rect/monitor intersection area - a
    deterministic computation over real geometry, not a guess: window
    managers commonly reparent/decorate windows such that a window can
    straddle a monitor boundary, and "most of the window is here" is the same
    notion `MonitorFromWindow(MONITOR_DEFAULTTONEAREST)` and equivalents use.

    Returns `None` - explicitly, never a fabricated attribution - when:
      - `rect` is `None` (this backend could not determine the window's
        geometry for this call), or
      - `monitors` is empty (enumeration unavailable), or
      - `rect` does not overlap any enumerated monitor at all. This is a
        real, expected case, not just a theoretical one: Windows moves
        minimized windows to an off-screen parking position (commonly
        around `(-32000, -32000)`), which is a genuine `GetWindowRect`
        result, not a bug in this function - reporting `None` for a
        minimized window's monitor is the honest answer.
    """
    if rect is None or not monitors:
        return None
    left, top, right, bottom = rect
    best_id: str | None = None
    best_area = 0
    for mon in monitors:
        mon_right, mon_bottom = mon.x + mon.width, mon.y + mon.height
        ix1, iy1 = max(left, mon.x), max(top, mon.y)
        ix2, iy2 = min(right, mon_right), min(bottom, mon_bottom)
        area = max(0, ix2 - ix1) * max(0, iy2 - iy1)
        if area > best_area:
            best_area = area
            best_id = mon.id
    return best_id
