#requires -Version 5.1
<#
  Amplifier computer-use : Windows on-desktop coexistence overlay.
  docs/designs/coexistence.md \u00a77. See overlay_windows.py for the full design
  rationale (why this looks different from the Linux X11 overlay, the
  click-without-activate technique, and why geometry is passed in rather than
  recomputed here).

  Usage (the only invocation - what WindowsOverlay.show() actually runs):
    powershell.exe -File overlay_windows.ps1 -ScreenX 0 -ScreenY 0 -ScreenWidth 1920 `
      -BandHeight 36 -PauseRect "x1,y1,x2,y2" -CancelRect "x1,y1,x2,y2"

  SINGLE-HOP by design (this used to relaunch itself via `Start-Process
  -Detached`, a separate top-level Windows process with no lifetime tie to
  its caller - see git history / the overlay leak report this closes for
  why that was the defect, not a feature). `WindowsOverlay.show()` now
  launches THIS invocation directly via `subprocess.Popen(..., stdin=PIPE)`
  and never lets it exit; there is no second hop, and the process this
  script runs as IS the overlay process for its entire life. Two direct
  consequences:

  1. `overlay_windows.py`'s caller gets the REAL overlay PID back immediately
     (`Popen.pid`) - no more parsing a "PID=<n>" line off an intermediary
     process's stdout to learn a DIFFERENT process's identity.
  2. This script's own stdin is now the caller's live pipe, which is exactly
     what the stdin watcher below depends on (see that section).

  Stdin-EOF watchdog: the real fix for the leaked-window defect
  --------------------------------------------------------------------------
  `atexit.register(overlay.hide)` (still present, in `overlay_windows.py`)
  only runs on the AGENT process's own CLEAN exit - never on SIGKILL, a hard
  crash, or a closed terminal, which are exactly the common cases that
  produced the reported leak (25 orphaned bands accumulated on one machine
  in an afternoon). A Windows Job Object (`JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`)
  was considered and is NOT reachable from here: it ties a process's life to
  another WINDOWS process holding a job handle, and there is no persistent
  Windows-side process in this architecture whose own life is 1:1 with the
  WSL2 agent's (every backend action is a fresh, one-shot `powershell.exe`
  subprocess - see `windows.py`'s module docstring). Building that persistent
  bridge is `docs/designs/remote-transport.md`'s deferred Phase C5/4, not
  this fix.

  What IS reachable, and used here instead: the caller keeps this process's
  OWN stdin as a live pipe (`subprocess.Popen(..., stdin=subprocess.PIPE)`)
  for as long as the overlay should exist. A background thread below blocks
  on `Console.In.ReadLine()`; the read returns `null` (EOF) the instant the
  pipe's write end closes - which the OS guarantees happens when the owning
  Linux/WSL2 process's file descriptor table is torn down, UNCONDITIONALLY,
  on every exit path including `SIGKILL` (this is a kernel-level guarantee,
  not a user-space hook like `atexit` that can simply fail to run). Verified
  on real hardware for this exact WSL2/Windows-interop boundary before this
  was written: a Linux process holding the pipe's write end was `kill -9`'d,
  and the Windows-side `[Console]::In.ReadLine()` unblocked with EOF within
  the same second - see the task's evidence log for the raw run.

  Precise, honest characterization of what this guarantees (see also this
  script's caller for the same claim in the Python-level docstring): this is
  NOT an OS-level "kill this process when that other process dies" primitive
  the way a Job Object is - it is a real pipe whose EOF the OS delivers
  unconditionally on the writer's exit, observed and acted on by this
  script's own code. If this script's watcher thread itself were somehow
  stuck or dead, EOF would still arrive at the OS level but nothing here
  would act on it. That residual risk is why `WindowsOverlay.show()` also
  sweeps and removes any pre-existing orphaned overlay (the `-Detached`
  command-line shape only the OLD, pre-fix code could have produced) before
  launching a new one - see that function's docstring.
#>
param(
  [Parameter(Mandatory = $true)][int]$ScreenX,
  [Parameter(Mandatory = $true)][int]$ScreenY,
  [Parameter(Mandatory = $true)][int]$ScreenWidth,
  [int]$BandHeight = 36,
  [Parameter(Mandatory = $true)][string]$PauseRect,
  [Parameter(Mandatory = $true)][string]$CancelRect,
  # A caller-supplied nonce, echoed back verbatim in the "ready" event -
  # see `WindowsOverlay._wait_for_ready`'s docstring for why readiness is
  # verified this way rather than by PID or by file timing: the caller
  # (WSL2 Python) and this process (native Windows) do not share a PID
  # space (`Popen.pid` names the LOCAL interop launcher, never the real
  # Windows PID - verified on real hardware), and their clocks are not
  # guaranteed to agree closely enough to use file-mtime freshness either
  # (also verified on real hardware: a >60s drift was observed between
  # the WSL2 clock and the mounted Windows temp directory's own mtime
  # stamps). A token this script did not invent and could not have seen
  # before this exact invocation is the only unambiguous signal.
  [Parameter(Mandatory = $true)][string]$Token
)
$ErrorActionPreference = 'Stop'

$OutputEncoding = New-Object System.Text.UTF8Encoding $false
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding $false
Add-Type -AssemblyName System.Windows.Forms, System.Drawing

$eventsDir = Join-Path $env:TEMP 'amplifier-computer-use'
New-Item -ItemType Directory -Force -Path $eventsDir | Out-Null
$eventsFile = Join-Path $eventsDir 'overlay-events.ndjson'
if (Test-Path -LiteralPath $eventsFile) { Remove-Item -LiteralPath $eventsFile -Force }

function Parse-Rect([string]$s) {
  $p = $s -split ',' | ForEach-Object { [int]$_ }
  return @{ X1 = $p[0]; Y1 = $p[1]; X2 = $p[2]; Y2 = $p[3] }
}

# NOTE: named distinctly from the (case-insensitively identical)
# $PauseRect/$CancelRect [string] script parameters on purpose. PowerShell
# parameter type constraints stick to the VARIABLE SLOT for its lifetime -
# `$pauseRect = Parse-Rect $PauseRect` looks like a fresh local but is
# actually the SAME slot as the `[string]$PauseRect` parameter (variable
# names are case-insensitive), so PowerShell silently coerces the
# Hashtable back to a string (\"System.Collections.Hashtable\" via ToString)
# to satisfy that stale constraint - then the next consumer that wants a
# real Hashtable fails with a confusing ParameterBindingArgumentTransformation
# error. Distinct names sidestep the collision entirely.
$pauseBox = Parse-Rect $PauseRect
$cancelBox = Parse-Rect $CancelRect
# Client-space (relative to the band form's own top-left), since Paint/
# MouseClick coordinates and Rectangle fills are all in client space -
# ScreenX/ScreenY is the form's OWN origin, so this is just the offset.
$pauseClient = New-Object System.Drawing.Rectangle(
  ($pauseBox.X1 - $ScreenX), ($pauseBox.Y1 - $ScreenY),
  ($pauseBox.X2 - $pauseBox.X1), ($pauseBox.Y2 - $pauseBox.Y1))
$cancelClient = New-Object System.Drawing.Rectangle(
  ($cancelBox.X1 - $ScreenX), ($cancelBox.Y1 - $ScreenY),
  ($cancelBox.X2 - $cancelBox.X1), ($cancelBox.Y2 - $cancelBox.Y1))

# A SINGLE top-level window for the whole band, manually painted (band
# background + two button rects + labels) and manually hit-tested on
# click. Earlier revisions used THREE independent top-level windows (band
# + two buttons) with the band made click-through (WS_EX_TRANSPARENT) and
# its Region punched with holes at the button rects - proven on real
# hardware to have UNDEFINED relative paint order (three never-activated
# topmost windows have no creation/Show()-order guarantee, since
# activation - what normally promotes z-order - never happens for any of
# them by design), so the band could and did paint over the buttons.
# A single window sidesteps that failure mode entirely: there is only one
# window, so there is no inter-window z-order to get wrong. The one
# property this trades away vs the Linux X11 overlay (SHAPE/ShapeInput,
# real per-pixel input transparency) is that clicks OUTSIDE the two
# buttons but INSIDE the band strip are swallowed rather than passed
# through to whatever is underneath - a real, stated platform difference,
# not hidden. WS_EX_NOACTIVATE still gives the required "does not steal
# focus, including on a button click" property for the whole window.
Add-Type @"
using System;
using System.Drawing;
using System.Threading;
using System.Windows.Forms;

public class OverlayBand : Form {
    public Rectangle PauseRect;
    public Rectangle CancelRect;
    public Color PauseColor;
    public Color CancelColor;
    public Color BandColor;
    public Action<string> OnButtonClick;
    private Thread _stdinWatcher;

    public OverlayBand() {
        FormBorderStyle = FormBorderStyle.None;
        ShowInTaskbar = false;
        StartPosition = FormStartPosition.Manual;
        DoubleBuffered = true;
    }
    protected override bool ShowWithoutActivation { get { return true; } }
    protected override CreateParams CreateParams {
        get {
            const int WS_EX_NOACTIVATE = 0x08000000;
            const int WS_EX_TOPMOST    = 0x00000008;
            const int WS_EX_TOOLWINDOW = 0x00000080;
            CreateParams cp = base.CreateParams;
            cp.ExStyle |= WS_EX_NOACTIVATE | WS_EX_TOPMOST | WS_EX_TOOLWINDOW;
            return cp;
        }
    }
    protected override void OnPaint(PaintEventArgs e) {
        using (var bandBrush = new SolidBrush(BandColor))
            e.Graphics.FillRectangle(bandBrush, ClientRectangle);
        using (var pauseBrush = new SolidBrush(PauseColor))
            e.Graphics.FillRectangle(pauseBrush, PauseRect);
        using (var cancelBrush = new SolidBrush(CancelColor))
            e.Graphics.FillRectangle(cancelBrush, CancelRect);
        var fmt = new StringFormat { Alignment = StringAlignment.Center, LineAlignment = StringAlignment.Center };
        using (var textBrush = new SolidBrush(Color.White))
        using (var font = new Font(FontFamily.GenericSansSerif, 9))
        {
            e.Graphics.DrawString("Pause", font, textBrush, PauseRect, fmt);
            e.Graphics.DrawString("Cancel", font, textBrush, CancelRect, fmt);
        }
    }
    protected override void OnMouseDown(MouseEventArgs e) {
        base.OnMouseDown(e);
        if (OnButtonClick == null) return;
        if (PauseRect.Contains(e.Location)) OnButtonClick("pause");
        else if (CancelRect.Contains(e.Location)) OnButtonClick("cancel");
    }

    // Blocks reading this process's OWN stdin on a background thread. The
    // caller (overlay_windows.py's WindowsOverlay.show()) launches this
    // whole process with stdin=PIPE and never closes or writes to it while
    // the overlay should stay up - so a read here only ever unblocks with
    // EOF, and only when the caller's end of that pipe closes (normal exit,
    // SIGKILL, crash, or a closed terminal all close it the same way, at
    // the OS level - see the module docstring above for the real-hardware
    // proof this depends on). BeginInvoke marshals the shutdown onto the UI
    // thread, which is the only thread allowed to touch Application/Form
    // state; Environment.Exit is the fallback if that marshal itself fails
    // (e.g. the form was never created/already disposed).
    public void StartStdinWatcher() {
        _stdinWatcher = new Thread(() => {
            try {
                while (Console.In.ReadLine() != null) { }
            } catch {
                // Any failure reading stdin is treated the same as EOF -
                // there is no safe way to keep running with an unreadable
                // liveness channel.
            }
            try {
                this.BeginInvoke(new Action(() => Application.Exit()));
            } catch {
                Environment.Exit(0);
            }
        });
        _stdinWatcher.IsBackground = true;
        _stdinWatcher.Name = "cu-overlay-stdin-watcher";
        _stdinWatcher.Start();
    }
}
"@ -ReferencedAssemblies System.Windows.Forms, System.Drawing

$band = New-Object OverlayBand
$band.Bounds = New-Object System.Drawing.Rectangle($ScreenX, $ScreenY, $ScreenWidth, $BandHeight)
$band.BandColor = [System.Drawing.Color]::FromArgb(0xB8, 0x86, 0x0B)
$band.PauseColor = [System.Drawing.Color]::FromArgb(0x55, 0x55, 0x55)
$band.CancelColor = [System.Drawing.Color]::FromArgb(0xA4, 0x1E, 0x1E)
$band.PauseRect = $pauseClient
$band.CancelRect = $cancelClient
$band.OnButtonClick = {
  param($name)
  $line = (@{ event = $name; at = (Get-Date).ToString('o'); pid = $PID } | ConvertTo-Json -Compress)
  Add-Content -LiteralPath $eventsFile -Value $line -Encoding UTF8
}

$diag = @{
  event      = 'diag'
  pauseRect  = "$($band.PauseRect)"
  cancelRect = "$($band.CancelRect)"
  bounds     = "$($band.Bounds)"
  bandColor  = "$($band.BandColor)"
} | ConvertTo-Json -Compress
Add-Content -LiteralPath $eventsFile -Value $diag -Encoding UTF8

$band.Show()
$band.StartStdinWatcher()
(@{ event = 'ready'; at = (Get-Date).ToString('o'); pid = $PID; token = $Token } | ConvertTo-Json -Compress) |
  Add-Content -LiteralPath $eventsFile -Encoding UTF8

[System.Windows.Forms.Application]::Run()
