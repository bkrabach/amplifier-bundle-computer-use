#requires -Version 5.1
<#
  Amplifier computer-use : Windows on-desktop coexistence overlay.
  docs/coexistence.md \u00a77. See overlay_windows.py for the full design
  rationale (why this looks different from the Linux X11 overlay, the
  click-without-activate technique, and why geometry is passed in rather than
  recomputed here).

  Usage (normal invocation - what WindowsOverlay.show() actually runs):
    powershell.exe -File overlay_windows.ps1 -ScreenX 0 -ScreenY 0 -ScreenWidth 1920 `
      -BandHeight 36 -PauseRect "x1,y1,x2,y2" -CancelRect "x1,y1,x2,y2"

  This process immediately relaunches ITSELF as a separate, detached, hidden
  process (passing -Detached), prints that child's PID to stdout as
  "PID=<n>", and exits. The caller captures the PID and is responsible for
  killing it later (Stop-Process -Force) - see overlay_windows.py's
  WindowsOverlay.hide().

  The -Detached invocation is the one that actually builds the window and
  runs the message loop; it never exits on its own.
#>
param(
  [Parameter(Mandatory = $true)][int]$ScreenX,
  [Parameter(Mandatory = $true)][int]$ScreenY,
  [Parameter(Mandatory = $true)][int]$ScreenWidth,
  [int]$BandHeight = 36,
  [Parameter(Mandatory = $true)][string]$PauseRect,
  [Parameter(Mandatory = $true)][string]$CancelRect,
  [switch]$Detached
)
$ErrorActionPreference = 'Stop'

if (-not $Detached) {
  # ---- Relaunch self, detached and hidden; hand back the child's PID. ----
  $selfArgs = @(
    '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-File', $PSCommandPath,
    '-ScreenX', $ScreenX, '-ScreenY', $ScreenY, '-ScreenWidth', $ScreenWidth,
    '-BandHeight', $BandHeight, '-PauseRect', $PauseRect, '-CancelRect', $CancelRect,
    '-Detached'
  )
  $psExe = (Get-Process -Id $PID).Path
  $child = Start-Process -FilePath $psExe -ArgumentList $selfArgs -WindowStyle Hidden -PassThru
  Write-Output "PID=$($child.Id)"
  exit 0
}

# ---- Detached child: build and run the actual overlay. ----
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
# Hashtable back to a string ("System.Collections.Hashtable" via ToString)
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
using System.Windows.Forms;

public class OverlayBand : Form {
    public Rectangle PauseRect;
    public Rectangle CancelRect;
    public Color PauseColor;
    public Color CancelColor;
    public Color BandColor;
    public Action<string> OnButtonClick;

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
(@{ event = 'ready'; at = (Get-Date).ToString('o'); pid = $PID } | ConvertTo-Json -Compress) |
  Add-Content -LiteralPath $eventsFile -Encoding UTF8

[System.Windows.Forms.Application]::Run()
