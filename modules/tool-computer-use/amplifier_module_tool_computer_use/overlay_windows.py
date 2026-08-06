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
to. This module instead launches `overlay_windows.ps1` directly as a
single long-lived process and tracks its PID explicitly. Teardown
(`hide()`) kills that PID with `Stop-Process -Force` (Windows' `SIGKILL`
equivalent - PowerShell cannot intercept or ignore it).

Lifetime binding - the leaked-overlay defect this closes
--------------------------------------------------------------------------
An earlier revision launched this as a DETACHED process (self-relaunched
via `Start-Process -WindowStyle Hidden`, passing `-Detached`) with
`atexit.register(overlay.hide)` (still present, one layer up in
`__init__.py`) as the ONLY teardown path. `atexit` only runs on this
process's own CLEAN exit - never on `SIGKILL`, a hard crash, or a closed
terminal. Those are exactly the common cases in daily use, and exactly
what an external report found: 25 orphaned always-on-top bands
accumulated on one machine in a single afternoon, invisible to Alt-Tab and
the taskbar (`WS_EX_TOOLWINDOW`, `ShowInTaskbar=false`), with no ordinary
way for a user to find or close them.

A Windows Job Object (`JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`) was considered
first, since it is the standard Win32 answer to "reap this on ANY parent
death." It does not reach across this particular boundary: a job object
ties a process's life to a HANDLE held by another WINDOWS process, and
there is no persistent Windows-side process in this architecture whose own
life is 1:1 with the WSL2 agent's - every backend action is a fresh,
one-shot `powershell.exe` subprocess (see `windows.py`'s module docstring).
Building that persistent bridge is `docs/designs/coexistence.md` \u00a77's
Phase C5 (folded into transport Phase 4) - a real, larger change, not this
fix.

What IS reachable, and used here: this module now launches the overlay
directly (no self-relaunch hop) via `subprocess.Popen(..., stdin=PIPE)`
and keeps that pipe's write end open, in this process, for as long as the
overlay should exist. `overlay_windows.ps1` blocks a background thread on
its OWN stdin; the read returns EOF the instant the pipe's write end
closes, which the OS guarantees happens when this process's file
descriptor table is torn down - UNCONDITIONALLY, on every exit path
including `SIGKILL` (a kernel guarantee, not a user-space hook that can
simply fail to run). Verified on real hardware for this exact WSL2/Windows
interop boundary: a Linux process holding the pipe's write end was
`kill -9`'d and the Windows-side `Console.In.ReadLine()` unblocked with EOF
within the same second - see the task's evidence log for the raw run.

Precise, honest characterization of what this is (and is not): this is
NOT a Windows Job Object and does not carry the OS's own "terminate on
handle close" primitive - it is a real pipe whose EOF the OS delivers
unconditionally on the writer's exit, observed and acted on by
`overlay_windows.ps1`'s own code once it is delivered. The residual risk
this leaves - the watcher thread itself failing to run in the target
process - is why `show()` also sweeps and removes pre-existing orphaned
overlays (the `-Detached` command-line shape only the OLD, pre-fix code
could ever have produced) before launching a new one; see
`_sweep_legacy_orphans`.

`verify_windows_overlay.py` still holds the real-hardware proof of the
band's own rendering/no-steal-focus/teardown properties, unaffected by
this change.

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
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .exclusion import ExclusionZone, Rect
from .windows import BackendError, _translate, _which_powershell

logger = logging.getLogger(__name__)

OVERLAY_PS1 = Path(__file__).parent / "overlay_windows.ps1"

#: How long `show()` polls the events file for the "ready" event before
#: giving up on a launched process and treating it as a failure (killing
#: it rather than leaving a never-ready process running - see `show()`).
#: Reuses the constructor's own `timeout` value by default (same knob that
#: used to bound the old blocking `subprocess.run` call), not a second
#: configuration surface.
READY_POLL_INTERVAL_SECONDS = 0.1

#: Bounded timeout for the one-shot legacy-orphan sweep query
#: (`_sweep_legacy_orphans`) - a single `Get-CimInstance` round trip, never
#: on the critical path for long.
SWEEP_TIMEOUT_SECONDS = 15.0

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
    single long-lived Windows process across the WSL2/Win32 boundary, with
    its lifetime tied to THIS process via a live stdin pipe (see the module
    docstring's "Lifetime binding" section for why, and the real-hardware
    proof it depends on).

    `powershell_path`/`timeout` mirror `WindowsBackend`'s own constructor
    knobs (see `windows.py`) rather than inventing a second configuration
    surface for the same underlying resolution problem. `timeout` now
    bounds how long `show()` waits for the launched process to prove it is
    actually alive (reach the "ready" event) rather than bounding a single
    blocking subprocess call - see `show()`.
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
        # The live process handle AND its stdin pipe - held open for the
        # overlay's entire life. Closing `self._proc.stdin` (in `hide()`) or
        # this whole Python process dying for ANY reason (the fix this
        # module exists for) is what the overlay process's own stdin
        # watcher reacts to - see the module docstring.
        self._proc: subprocess.Popen[bytes] | None = None

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
        """Launch the overlay process directly (no self-relaunch hop) and
        verify it actually comes up before returning. Idempotent.

        Order of operations, and why:
        1. Register button rects (unchanged from before).
        2. `_sweep_legacy_orphans()` - best-effort, never blocks or fails
           this call - removes any pre-existing overlay processes carrying
           the OLD `-Detached` command-line shape (impossible for current
           code to produce), self-healing against leaks from before this
           fix shipped or from any residual edge case (see that method).
        3. Launch via `subprocess.Popen(..., stdin=PIPE)`, NOT `.run()` -
           this process is meant to outlive this call and its lifetime
           must be tied to the pipe staying open (see module docstring).
           `proc.pid` is NOT the overlay's real Windows PID - verified on
           real hardware: WSL2 interop's `Popen.pid` names the LOCAL
           interop launcher, a different number from the actual Windows
           process's own `$PID` (e.g. local 1427133 vs the real remote
           70116, observed in the same launch). The real PID is read back
           from the "ready" event's own `pid` field instead - see
           `_wait_for_ready`.
        4. Poll the events file for the "ready" event, bounded by
           `self._timeout`. A launched-but-never-ready process is a real
           failure, not a success with unknown side effects: it is killed
           and reported loudly (`BackendError`), never left running - this
           is what the overlay leak report's recommendations 4/5 ask for
           (bound the failure, surface it) at the one place in this
           codebase that can actually observe it.
        """
        if self._shown:
            return
        self._buttons = self._button_rects()
        if self._exclusion is not None:
            for btn in self._buttons:
                self._exclusion.register(f"overlay_{btn.name}_button", btn.rect)

        self._sweep_legacy_orphans()

        pause, cancel = self._buttons[0].rect, self._buttons[1].rect
        # A nonce this exact invocation could not have seen before, echoed
        # back verbatim in the "ready" event - see `_wait_for_ready`'s
        # docstring for why readiness is verified this way (neither PID
        # matching nor file-mtime freshness survived real-hardware testing
        # across the WSL2/Windows boundary).
        token = uuid.uuid4().hex
        proc: subprocess.Popen[bytes] | None = None
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
                "-Token",
                token,
            ]
            proc = subprocess.Popen(
                [self.powershell, *args],
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
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

        real_pid, detail = self._wait_for_ready(proc, token)
        if real_pid is None:
            self._kill_failed_launch(proc)
            if self._exclusion is not None:
                for btn in self._buttons:
                    self._exclusion.unregister(f"overlay_{btn.name}_button")
            logger.error(
                "windows coexistence overlay: local process (interop pid=%s) "
                "never reached 'ready' within %.1fs - killed rather than left "
                "running unverified. %s",
                proc.pid,
                self._timeout,
                detail,
            )
            raise BackendError(
                f"overlay process (interop pid={proc.pid}) did not reach "
                f"ready within {self._timeout:.1f}s: {detail}"
            )

        self._proc = proc
        self._pid = real_pid
        self._shown = True
        self._stop.clear()
        self._write_attribution()
        if self._on_pause is not None or self._on_cancel is not None:
            self._poll_thread = threading.Thread(
                target=self._poll_events, name="cu-overlay-win-poll", daemon=True
            )
            self._poll_thread.start()
        logger.info(
            "windows coexistence overlay shown: pid=%s band=%dx%d at (%d,%d), "
            "buttons=%s, stdin-guarded (dies with this process, including "
            "SIGKILL - see module docstring)",
            self._pid,
            self._screen_width,
            BAND_HEIGHT,
            self._screen_x,
            self._screen_y,
            [b.name for b in self._buttons],
        )

    def _wait_for_ready(
        self, proc: subprocess.Popen[bytes], token: str
    ) -> tuple[int | None, str]:
        """Poll the events file for `{"event": "ready", "token": token, ...}`
        and return the REAL Windows PID it reports.

        Two things that look like they should work here do NOT, both
        verified on real hardware before landing on the design below:

        - Matching `proc.pid`: `Popen.pid` names the LOCAL WSL2 interop
          launcher, not the actual Windows-side process - e.g. local
          1430327 vs the real remote 69396 observed for the same launch.
          `show()`'s docstring has the same note.
        - Freshness by file mtime: the script deletes and recreates this
          events file as its first action on every launch, and an
          `st_mtime >= spawn_time` check assumes the two machines' clocks
          agree closely - they do not reliably: a >60s drift was measured
          between the WSL2 clock and the mounted Windows temp directory's
          own mtime stamps on this exact box, mid-task.

        What DOES work, and is used here: `token` is a nonce generated
        fresh in `show()` for this exact invocation and passed as
        `-Token`. This process could not have produced it before being
        launched, so a `ready` event carrying it is unambiguously THIS
        launch's own - no shared clock, no shared PID space required.

        Also treats the process exiting on its own (before ever reaching
        ready) as an immediate failure rather than waiting out the full
        timeout - see `show()`.
        """
        wsl_path: str | None = None
        deadline = time.monotonic() + self._timeout
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                stderr = b""
                try:
                    if proc.stderr is not None:
                        stderr = proc.stderr.read()
                except Exception:
                    pass
                return None, (
                    f"process exited early (rc={proc.returncode}) "
                    f"stderr={stderr.decode('utf-8', 'replace').strip()[:400]!r}"
                )
            if wsl_path is None:
                wsl_path = self._events_path_wsl()
            if wsl_path is not None:
                try:
                    lines = Path(wsl_path).read_text(encoding="utf-8").splitlines()
                except OSError:
                    lines = []
                for line in lines:
                    try:
                        evt = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if evt.get("event") == "ready" and evt.get("token") == token:
                        pid = evt.get("pid")
                        if isinstance(pid, int):
                            return pid, "ready"
            time.sleep(READY_POLL_INTERVAL_SECONDS)
        return None, "timed out waiting for the 'ready' event"

    def _kill_failed_launch(self, proc: subprocess.Popen[bytes]) -> None:
        """A launch that never reached ready must not be left running -
        that is exactly the "silent failure loop" the overlay leak report
        flagged (recommendation 5). Kill it directly rather than relying on
        the stdin watcher (which requires the script to have gotten far
        enough to start that thread - not guaranteed for every failure
        mode, e.g. an `Add-Type` compile error)."""
        try:
            proc.kill()
            proc.wait(timeout=5)
        except Exception:
            logger.debug(
                "windows overlay: error killing failed launch pid=%s",
                proc.pid,
                exc_info=True,
            )
        for stream in (proc.stdin, proc.stdout, proc.stderr):
            try:
                if stream is not None:
                    stream.close()
            except Exception:
                pass

    def _sweep_legacy_orphans(self) -> None:
        """Best-effort startup sweep: find and kill any overlay process
        still carrying the OLD self-relaunched `-Detached` command-line
        shape - the exact signature of the leaked processes the overlay
        leak report found accumulating (25 on one machine in an
        afternoon). Current code never launches with `-Detached` (see the
        module docstring's "Lifetime binding" section), so any process
        matching it is unambiguously an orphan from before this fix, or
        from an older cached build of this module - never a live sibling
        session's overlay (which would match by `overlay_windows.ps1`
        alone, but never `-Detached`).

        Deliberately conservative: does NOT touch overlay processes
        without `-Detached` in their command line, so a concurrent,
        legitimate sibling session's overlay (this codebase has no
        cross-process dedup for that case - see `_channel_identity`'s own
        docstring) is never killed by this sweep.

        Never raises and never blocks `show()` for long: any failure here
        (powershell unavailable, the query itself failing, a timeout) is
        logged and swallowed - a failed sweep is not a reason to refuse
        showing the new overlay.
        """
        try:
            ps = self.powershell
        except BackendError:
            return
        try:
            proc = subprocess.run(
                [
                    ps,
                    "-NoProfile",
                    "-NonInteractive",
                    "-Command",
                    "Get-CimInstance Win32_Process -Filter \"Name='powershell.exe'\" | "
                    "Where-Object { $_.CommandLine -like '*overlay_windows.ps1*' -and "
                    "$_.CommandLine -like '*-Detached*' } | "
                    "ForEach-Object { "
                    "Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue; "
                    'Write-Output "KILLED=$($_.ProcessId)" }',
                ],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=SWEEP_TIMEOUT_SECONDS,
                check=False,
            )
        except Exception:
            logger.debug("windows overlay: legacy-orphan sweep failed", exc_info=True)
            return
        killed = [
            ln.split("=", 1)[1].strip()
            for ln in (proc.stdout or "").splitlines()
            if ln.strip().startswith("KILLED=")
        ]
        if killed:
            logger.warning(
                "windows coexistence overlay: swept %d orphaned legacy overlay "
                "process(es) before launching a new one (pids=%s) - these "
                "predate the stdin-liveness fix and could not have been "
                "reaped any other way",
                len(killed),
                killed,
            )

    def _write_attribution(self) -> None:
        """Best-effort: record this overlay's pid + start time next to the
        events file, so a human (or a future cleanup tool) can identify a
        running overlay by a direct lookup instead of enumerating and
        string-matching every `powershell.exe` command line on the
        machine (the overlay leak report's recommendation 3). Deleted in
        `hide()`. Never raises - attribution is a convenience, not a
        correctness requirement."""
        wsl_path = self._events_path_wsl()
        if wsl_path is None or self._pid is None:
            return
        try:
            attribution = Path(wsl_path).parent / f"overlay-{self._pid}.json"
            attribution.write_text(
                json.dumps(
                    {
                        "pid": self._pid,
                        "started_at": datetime.now(timezone.utc).isoformat(),
                        "stdin_guarded": True,
                    }
                ),
                encoding="utf-8",
            )
        except OSError:
            logger.debug(
                "windows overlay: failed to write attribution file", exc_info=True
            )

    def hide(self) -> None:
        """Tear down the overlay and unregister exclusion rects.

        Two independent teardown signals now, not one:
        1. Closing `self._proc.stdin` here - the overlay's own stdin
           watcher sees EOF and exits itself (the same path a `SIGKILL` of
           THIS process triggers - see module docstring).
        2. The explicit `Stop-Process -Force` below (Windows' `SIGKILL`
           equivalent - uncatchable, unmaskable), kept as a fast,
           deterministic guarantee that does not depend on the watcher
           thread noticing EOF in any particular amount of time.

        Windows tears down every window/GDI resource owned by a process
        the instant it terminates, by clean exit or by force - the same
        "resource lifetime == process lifetime" guarantee U6 proved for
        X11, just invoked explicitly rather than falling out of a socket
        dying on its own.
        """
        if not self._shown:
            return
        self._stop.set()
        if self._proc is not None:
            try:
                if self._proc.stdin is not None:
                    self._proc.stdin.close()
            except Exception:
                pass
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
                    stdin=subprocess.DEVNULL,
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
        if self._proc is not None:
            try:
                self._proc.wait(timeout=5)
            except Exception:
                logger.debug(
                    "windows overlay: local process handle did not reap cleanly "
                    "for pid=%s",
                    self._pid,
                    exc_info=True,
                )
            for stream in (self._proc.stdout, self._proc.stderr):
                try:
                    if stream is not None:
                        stream.close()
                except Exception:
                    pass
        wsl_path = self._events_path_wsl()
        if wsl_path is not None and self._pid is not None:
            try:
                (Path(wsl_path).parent / f"overlay-{self._pid}.json").unlink(
                    missing_ok=True
                )
            except OSError:
                pass
        if self._exclusion is not None:
            for btn in self._buttons:
                self._exclusion.unregister(f"overlay_{btn.name}_button")
        self._pid = None
        self._proc = None
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
        direction.

        `$env:TEMP` (PowerShell environment-variable syntax), NOT `%TEMP%`
        (that is cmd.exe/batch syntax) - verified on real hardware that
        passing the literal string `%TEMP%\\...` as a `-Command` makes
        PowerShell try to load a MODULE named `%TEMP%` ("The module
        '%TEMP%' could not be loaded") rather than expand anything, so
        this always returned `None` and silently disabled both readiness
        verification and the pause/cancel click poll (`_poll_events`) -
        the latter with no test coverage able to catch it, since every
        existing test mocks this method rather than exercising the real
        command string.
        """
        try:
            expanded = subprocess.run(
                [
                    self.powershell,
                    "-NoProfile",
                    "-NonInteractive",
                    "-Command",
                    f"Write-Output ([IO.Path]::Combine($env:TEMP, 'amplifier-computer-use', {EVENTS_FILENAME!r}))",
                ],
                stdin=subprocess.DEVNULL,
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
