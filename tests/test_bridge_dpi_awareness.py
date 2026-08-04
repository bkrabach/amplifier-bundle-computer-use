"""Regression guard for the multi-monitor DPI-virtualization defect in
`bridge.ps1`.

CI-side only: no Windows target, no PowerShell, no subprocess spawned - this
is a source-text assertion, following the same established pattern as
`test_overlay_windows.py` for Windows-only code this suite cannot execute.
Real-hardware proof lives in the accompanying investigation report (a real
four-monitor Win11 desktop reached over SSH, `brkrabac@alienware-r13`), not
here - matching `CONTRIBUTING.md`'s "ship gate" precedent for platform code
with no CI-side Windows box available.

Bug this guards against
------------------------
`bridge.ps1` used to call the legacy `SetProcessDPIAware()`, which declares
only SYSTEM DPI awareness. Measured live on a real four-monitor 4K desktop
(every physical panel 3840x2160 at 150% scaling): under that legacy
awareness level, `[System.Windows.Forms.Screen]::AllScreens` reports every
monitor's bounds DPI-VIRTUALIZED to 2560x1440 - not the true physical pixel
grid. Every downstream coordinate (`Save-Screenshot`'s `CopyFromScreen`,
`SetCursorPos`, `GetWindowRect`, ...) is built from those same virtualized
bounds, so a monitor-scoped `screenshot` captures a squished/cropped
fraction of the real monitor into a bitmap sized for the WRONG dimensions,
and every click/move computed from that same `Display` lands off-target on
that monitor - precisely the live-agent report this fix responds to
("DISPLAY2's 3840x2160 renders into only the left ~635px of the 1280x720
frame").

The fix declares Per-Monitor-v2 DPI awareness
(`SetProcessDpiAwarenessContext(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2)`,
Windows 10 1703+) before any DPI-sensitive Win32 call, and fails loud (never
falls back to the DPI-broken legacy call) if the Windows build refuses it -
this bundle's own no-silent-degradation rule (a capture that cannot
faithfully represent a monitor must fail loud, not return a misleading
frame).
"""

from __future__ import annotations

from pathlib import Path

BRIDGE_PS1 = (
    Path(__file__).resolve().parents[1]
    / "modules"
    / "tool-computer-use"
    / "amplifier_module_tool_computer_use"
    / "bridge.ps1"
)


def _source() -> str:
    return BRIDGE_PS1.read_text(encoding="utf-8")


def test_bridge_declares_per_monitor_v2_dpi_awareness():
    """The fix: a P/Invoke declaration for the per-monitor-aware API must
    exist - without it, the process cannot escape system-DPI virtualization
    on a mixed/scaled multi-monitor desktop."""
    src = _source()
    assert "SetProcessDpiAwarenessContext" in src, (
        "bridge.ps1 no longer declares SetProcessDpiAwarenessContext - "
        "without per-monitor-v2 DPI awareness, Screen.AllScreens and every "
        "CopyFromScreen/SetCursorPos coordinate are DPI-virtualized on any "
        "multi-monitor desktop with per-monitor scaling, corrupting "
        "monitor-scoped capture and every click computed from it"
    )


def test_bridge_never_calls_the_legacy_system_dpi_aware_api():
    """`SetProcessDPIAware()` (legacy, system-DPI-only) is the actual root
    cause - it must not be called anywhere, including as a fallback. A
    fallback here would be exactly the silent degradation this bundle
    refuses: it would work on a single-monitor uniform-DPI box and silently
    corrupt capture/input on the real multi-monitor, mixed-DPI desktop this
    bug was filed against."""
    src = _source()
    # Case-sensitive: `SetProcessDpiAwarenessContext` (the fix) must not be
    # confused with `SetProcessDPIAware` (the bug) by a substring match -
    # they differ only in capitalization, so match the legacy call's exact
    # casing and its invocation form.
    assert "SetProcessDPIAware(" not in src, (
        "bridge.ps1 still declares/calls the legacy SetProcessDPIAware() - "
        "this is the exact API that DPI-virtualizes monitor bounds and "
        "capture coordinates on a mixed-DPI multi-monitor desktop"
    )


def test_bridge_fails_loud_if_per_monitor_dpi_awareness_is_refused():
    """No silent degradation: if this Windows build refuses per-monitor-v2
    awareness (predates the 1703 Creators Update), the bridge must throw -
    not continue and silently serve a capture it cannot guarantee is
    correctly scoped."""
    src = _source()
    assert "SetProcessDpiAwarenessContext" in src
    # The call's return value must be checked and a failure must raise -
    # not merely be invoked and ignored (`[void]`-discarded, the pattern the
    # legacy call used, which is exactly how this bug shipped unnoticed).
    call_site = src[src.index("if (-not [CU]::SetProcessDpiAwarenessContext") :]
    throw_block = call_site[: call_site.index("}") + 1]
    assert "throw" in throw_block, (
        "SetProcessDpiAwarenessContext's return value must be checked with "
        "a `throw` on failure (fail loud) - not silently discarded like "
        "the legacy [void][CU]::SetProcessDPIAware() call it replaces"
    )


def test_bridge_uses_the_per_monitor_aware_v2_context_constant():
    """Must request PER_MONITOR_AWARE_V2 (-4), the strictest available
    context, not one of the weaker DPI_AWARENESS_CONTEXT values (system-DPI
    or the non-V2 per-monitor-aware, which still mis-scales non-client area
    and dialogs) - V2 is what makes Screen.AllScreens/CopyFromScreen/cursor
    coordinates consistently physical across every monitor regardless of
    its individual scale factor."""
    src = _source()
    assert "SetProcessDpiAwarenessContext([IntPtr]-4)" in src, (
        "expected SetProcessDpiAwarenessContext to be called with "
        "DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 ([IntPtr]-4)"
    )


def test_bridge_sets_dpi_awareness_before_any_session_or_capture_logic():
    """The awareness context must be set before `Get-SessionState`/the
    dispatch switch runs - every action (screenshot, zoom, cursor_position,
    mouse_move, ...) depends on it, and setting it after the first
    DPI-sensitive call would be too late for that call."""
    src = _source()
    dpi_idx = src.index("SetProcessDpiAwarenessContext([IntPtr]-4)")
    session_state_idx = src.index("$state = Get-SessionState")
    assert dpi_idx < session_state_idx, (
        "DPI awareness must be set before Get-SessionState/dispatch runs, not after"
    )
