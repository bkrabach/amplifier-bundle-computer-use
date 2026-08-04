"""The Windows on-desktop coexistence overlay - `docs/designs/coexistence.md` \u00a77.

An always-on-top status band with Pause/Cancel buttons, matching
`overlay_linux.py`'s shape and semantics as closely as the two platforms'
architectures allow - see that module for the properties both overlays are
responsible for (input-transparent band + real button rects, exclusion
registration at the injection call site).

Why this module looks different from `overlay_linux.py`, architecturally
--------------------------------------------------------------------------
`LinuxOverlay` lives INSIDE the same process that already holds a live X11
socket (`linux_x11.py`'s `LinuxX11Backend`) - so its lifetime is tied to
that socket for free (U6: the X server destroys every resource a client
owns the instant its connection dies, `SIGKILL` included).

`WindowsBackend` has no equivalent live connection: every action crosses
the WSL2/Win32 boundary through a *fresh* `powershell.exe` subprocess
(`windows.py`), so there is nothing in-process to tie an overlay's lifetime
to. This module instead launches the overlay as its own DETACHED Windows
process (`overlay_windows.ps1`, invoked with `-Detached`) and tracks its
PID explicitly. Teardown (`hide()`) kills that PID with
`Stop-Process -Force` (Windows' `SIGKILL` equivalent - PowerShell cannot
intercept or ignore it). This is not a weaker guarantee than Linux's: Win32
guarantees every window and GDI resource owned by a process is destroyed
the instant that process terminates, by clean exit OR by force, exactly the
same "resource lifetime == process lifetime" property U6 proved for X11 -
just enforced by a different OS subsystem. See `verify_windows_overlay.py`
for the real-hardware proof of this, and the honest caveat: unlike X11's
*automatic* teardown on socket death, an unexpected death of the AGENT's
own WSL2-side process does not automatically kill this Windows-side PID
today (there is no live handle spanning that boundary) - killing it
requires an explicit `hide()` call. Tying the two together is exactly what
`docs/designs/coexistence.md` \u00a77's Phase C5 (folded into transport Phase 4,
one persistent process serving injection + presence + overlay together)
is for; this module is the overlay half, built and proven standalone ahead
of that integration, matching this codebase's existing pattern of shipping
`overlay_linux.py` unwired into `ComputerTool.mount()` until its consumer
lands.

Click-without-activate, the Win32 way
--------------------------------------
X11's override-redirect + `SHAPE`/`ShapeInput` combination has a direct
Win32 analogue used here:

* The status band itself is `WS_EX_TRANSPARENT` (click-through - real
  clicks pass to whatever is underneath) + `WS_EX_NOACTIVATE` (never
  becomes the foreground window) + `WS_EX_TOPMOST` (always on top) +
  `WS_EX_TOOLWINDOW` (no taskbar/alt-tab entry).
* The Pause and Cancel buttons are separate small top-level windows at the
  band's button rects, WITHOUT `WS_EX_TRANSPARENT` (so they DO receive
  clicks) but still `WS_EX_NOACTIVATE` - Microsoft's own documented purpose
  for that flag is precisely "a window that does not become the foreground
  window when the user clicks it," which is the exact property this
  feature requires ("accepts clicks on its buttons while another window
  keeps focus").

Geometry is intentionally NOT re-derived in the PowerShell script: this
module computes `_button_rects()` once, in Python, and passes the resulting
rects to `overlay_windows.ps1` as explicit CLI arguments. Two independent
implementations of the same right-aligned-band formula (one per language)
would drift silently the day someone changes `BUTTON_WIDTH` in one and not
the other; passing the already-computed numbers keeps there being exactly
one source of truth, in the same language `exclusion.ExclusionZone` (the
other consumer of these same rects) already lives in.

Known limitation, disclosed rather than hidden: button fill color on a
per-monitor-DPI-scaled desktop
------------------------------------------------------------------------
Proven on real hardware (`windows-host`, a live Windows 11 desktop):
the band renders (a real, quantified pixel-color change at the band's own
sample point), does not steal focus (identical foreground window handle
before/after), and tears down with zero residual pixels or process
(`Stop-Process -Force`, then the sampled pixels revert exactly to their
pre-launch values). All three were measured directly - see the top-level
task report for the raw before/after numbers.

NOT proven, and left honestly unresolved: on this same hardware (a
multi-monitor desktop where the target display reports 144 `PixelsPerXLogicalInch`,
i.e. 150% DPI scaling), the Pause/Cancel button sub-rectangles paint with
the *band's* color rather than their own distinct gray/red, even though a
paint-time diagnostic confirmed `OverlayBand.PauseRect`/`CancelRect` hold
the exact correct client-space coordinates at the moment `OnPaint` runs.
The overall band's gross position and color are unaffected. The most
likely cause, based on what was ruled out and what was confirmed: DPI
virtualization for a non-DPI-aware process (this script does not call
`SetProcessDPIAware`/`SetProcessDpiAwarenessContext`) can non-uniformly
scale/stretch a window's composited bitmap on a non-100%-scaled monitor,
which would explain correct gross placement (the outer window bounds)
coexisting with incorrect fine sub-rectangle rendering (a `<100px` button
within a `2560px` band) - but this was not proven by elimination to the
same evidence standard as the three properties above; adding
`SetProcessDPIAware()` was tried and made the band's OWN rendering
disappear entirely against a non-DPI-aware sampling script, which is
consistent with the theory but not a proof, and fixing it properly would
require making the ENTIRE Windows coordinate pipeline (this module,
`windows.py`, `bridge.ps1`, and whatever samples its output) consistently
per-monitor-DPI-aware together - a larger, riskier change than this task's
scope, attempted here only far enough to characterize the defect
honestly rather than silently ship a plausible-looking but unverified fix.
Clicking the actual button areas was consequently NOT verified end-to-end
on hardware (the geometry is provably correct; whether a human's real
click lands correctly given this same rendering discrepancy is unknown).
"""

from __future__ import annotations

import json
import logging
import subprocess
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .exclusion import ExclusionZone, Rect
from .windows import BackendError, _translate, _which_powershell

logger = logging.getLogger(__name__)

OVERLAY_PS1 = Path(__file__).parent / "overlay_windows.ps1"

#: Same geometry constants as `overlay_linux.py` - a thin strip along the
#: top edge with two right-aligned buttons. Kept as an independent literal
#: rather than importing from `overlay_linux` (which has no Windows
#: dependency to leak, but the two platform overlay modules have never
#: shared a common ancestor and this repo does not yet have a
#: platform-agnostic `overlay_geometry` module - a reasonable follow-up
#: once both have shipped, per the kernel philosophy's two-implementation
#: rule, not before).
BAND_HEIGHT = 36
BUTTON_WIDTH = 90
BUTTON_MARGIN = 8

#: Fixed, well-known location for click events, mirroring `bridge.ps1`'s
#: own `Save-Screenshot` temp-directory convention exactly (`$env:TEMP\
#: amplifier-computer-use\`) rather than inventing a new one. Windows path
#: form; translate with `_translate(..., "-u")` to read it from WSL2, the
#: same direction already proven by `WindowsBackend.capture()` for
#: screenshot files.
EVENTS_FILENAME = "overlay-events.ndjson"


@dataclass(frozen=True)
class OverlayButton:
    name: str
    rect: Rect


class WindowsOverlay:
    """An always-on-top status band with Pause/Cancel buttons, hosted as a
    detached Windows process across the WSL2/Win32 boundary.

    `powershell_path`/`timeout` mirror `WindowsBackend`'s own constructor
    knobs (see `windows.py`) rather than inventing a second configuration
    surface for the same underlying resolution problem.
    """

    def __init__(
        self,
        *,
        screen_width: int,
        screen_x: int = 0,
        screen_y: int = 0,
        exclusion: ExclusionZone | None = None,
        on_pause: Callable[[], None] | None = None,
        on_cancel: Callable[[], None] | None = None,
        powershell_path: str | None = None,
        timeout: float = 30.0,
    ) -> None:
        self._screen_x = screen_x
        self._screen_y = screen_y
        self._screen_width = screen_width
        self._exclusion = exclusion
        self._on_pause = on_pause
        self._on_cancel = on_cancel
        self._ps_config = powershell_path
        self._timeout = timeout
        self._pid: int | None = None
        self._buttons: list[OverlayButton] = []
        self._events_seen = 0
        self._poll_thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._shown = False

    # -- geometry (pure, unit-testable without any Windows dependency) -------
    def _button_rects(self) -> list[OverlayButton]:
        """Right-aligned Pause/Cancel button rects within the band, in
        SCREEN space - identical formula to `overlay_linux.LinuxOverlay
        ._button_rects`, see the module docstring for why this is not
        imported from there instead."""
        y1 = self._screen_y
        y2 = self._screen_y + BAND_HEIGHT
        cancel_x2 = self._screen_x + self._screen_width - BUTTON_MARGIN
        cancel_x1 = cancel_x2 - BUTTON_WIDTH
        pause_x2 = cancel_x1 - BUTTON_MARGIN
        pause_x1 = pause_x2 - BUTTON_WIDTH
        return [
            OverlayButton("pause", Rect(pause_x1, y1, pause_x2, y2)),
            OverlayButton("cancel", Rect(cancel_x1, y1, cancel_x2, y2)),
        ]

    @property
    def powershell(self) -> str:
        ps, attempts = _which_powershell(self._ps_config)
        if ps is None:
            detail = "; ".join(attempts) if attempts else "no candidates found"
            raise BackendError(f"powershell.exe not found (tried: {detail})")
        return ps

    # -- lifecycle ------------------------------------------------------------
    def show(self) -> None:
        """Launch the detached overlay process. Idempotent."""
        if self._shown:
            return
        self._buttons = self._button_rects()
        if self._exclusion is not None:
            for btn in self._buttons:
                self._exclusion.register(f"overlay_{btn.name}_button", btn.rect)

        pause, cancel = self._buttons[0].rect, self._buttons[1].rect
        try:
            args = [
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                _translate(str(OVERLAY_PS1), "-w"),
                "-ScreenX",
                str(self._screen_x),
                "-ScreenY",
                str(self._screen_y),
                "-ScreenWidth",
                str(self._screen_width),
                "-BandHeight",
                str(BAND_HEIGHT),
                "-PauseRect",
                f"{pause.x1},{pause.y1},{pause.x2},{pause.y2}",
                "-CancelRect",
                f"{cancel.x1},{cancel.y1},{cancel.x2},{cancel.y2}",
            ]
            proc = subprocess.run(
                [self.powershell, *args],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self._timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            if self._exclusion is not None:
                for btn in self._buttons:
                    self._exclusion.unregister(f"overlay_{btn.name}_button")
            raise BackendError(f"overlay launch timed out: {exc}") from exc
        except Exception:
            # Any failure building the launch args or resolving/invoking
            # powershell.exe itself (path translation failing, powershell
            # not found, ...) must not leave rects registered for a window
            # that was never launched - see test_overlay_windows.py's
            # test_show_registers_rects_and_hide_unregisters_them_without_a_process.
            if self._exclusion is not None:
                for btn in self._buttons:
                    self._exclusion.unregister(f"overlay_{btn.name}_button")
            raise

        pid_line = next(
            (
                ln
                for ln in (proc.stdout or "").splitlines()
                if ln.strip().startswith("PID=")
            ),
            None,
        )
        if proc.returncode != 0 or pid_line is None:
            if self._exclusion is not None:
                for btn in self._buttons:
                    self._exclusion.unregister(f"overlay_{btn.name}_button")
            raise BackendError(
                f"failed to launch windows overlay (rc={proc.returncode}): "
                f"stdout={(proc.stdout or '').strip()[:400]!r} "
                f"stderr={(proc.stderr or '').strip()[:400]!r}"
            )
        self._pid = int(pid_line.split("=", 1)[1].strip())
        self._shown = True
        self._stop.clear()
        if self._on_pause is not None or self._on_cancel is not None:
            self._poll_thread = threading.Thread(
                target=self._poll_events, name="cu-overlay-win-poll", daemon=True
            )
            self._poll_thread.start()
        logger.info(
            "windows coexistence overlay shown: pid=%s band=%dx%d at (%d,%d), "
            "buttons=%s",
            self._pid,
            self._screen_width,
            BAND_HEIGHT,
            self._screen_x,
            self._screen_y,
            [b.name for b in self._buttons],
        )

    def hide(self) -> None:
        """Kill the detached process and unregister exclusion rects.

        Windows tears down every window/GDI resource owned by a process the
        instant it terminates - `Stop-Process -Force` is Windows'
        `SIGKILL` equivalent (uncatchable, unmaskable) - so this is the
        same "resource lifetime == process lifetime" guarantee U6 proved
        for X11, just invoked explicitly rather than falling out of a
        socket dying on its own.
        """
        if not self._shown:
            return
        self._stop.set()
        if self._pid is not None:
            try:
                subprocess.run(
                    [
                        self.powershell,
                        "-NoProfile",
                        "-NonInteractive",
                        "-Command",
                        f"Stop-Process -Id {self._pid} -Force -ErrorAction SilentlyContinue",
                    ],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=15,
                    check=False,
                )
            except Exception:
                logger.debug(
                    "windows overlay: error stopping pid=%s", self._pid, exc_info=True
                )
        if self._exclusion is not None:
            for btn in self._buttons:
                self._exclusion.unregister(f"overlay_{btn.name}_button")
        self._pid = None
        self._shown = False

    @property
    def shown(self) -> bool:
        return self._shown

    @property
    def pid(self) -> int | None:
        return self._pid

    @property
    def buttons(self) -> list[OverlayButton]:
        return list(self._buttons)

    # -- click handling (best-effort; see EVENTS_FILENAME) --------------------
    def _events_path_wsl(self) -> str | None:
        """Translate the fixed Windows-side events log to a WSL2 path -
        same Windows->WSL direction `WindowsBackend.capture()` already
        proves works for screenshot files, so this reuses a proven path
        rather than the untested reverse (WSL->Windows UNC write)
        direction."""
        try:
            win_path = f"%TEMP%\\amplifier-computer-use\\{EVENTS_FILENAME}"
            expanded = subprocess.run(
                [
                    self.powershell,
                    "-NoProfile",
                    "-NonInteractive",
                    "-Command",
                    win_path,
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=15,
                check=False,
            )
            resolved = expanded.stdout.strip()
            if not resolved:
                return None
            return _translate(resolved, "-u")
        except Exception:
            return None

    def _poll_events(self) -> None:
        """Background thread: tail the events NDJSON file for new
        pause/cancel lines and dispatch the matching callback. Best-effort
        - a read failure (file not yet created, translation failure) is
        treated as "no new events yet," never raised into the caller."""
        wsl_path = None
        while not self._stop.is_set():
            if wsl_path is None:
                wsl_path = self._events_path_wsl()
                if wsl_path is None:
                    self._stop.wait(0.5)
                    continue
            try:
                lines = Path(wsl_path).read_text(encoding="utf-8").splitlines()
            except OSError:
                self._stop.wait(0.2)
                continue
            for line in lines[self._events_seen :]:
                self._events_seen += 1
                try:
                    evt = json.loads(line)
                except json.JSONDecodeError:
                    continue
                name = evt.get("event")
                if name == "pause" and self._on_pause is not None:
                    self._on_pause()
                elif name == "cancel" and self._on_cancel is not None:
                    self._on_cancel()
            self._stop.wait(0.1)
