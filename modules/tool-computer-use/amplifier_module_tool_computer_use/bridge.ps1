#requires -Version 5.1
<#
  Amplifier computer-use : WSL2 -> Windows desktop bridge.

  Usage:  powershell.exe -NoProfile -ExecutionPolicy Bypass -File bridge.ps1 -RequestFile <path.json>
  Input :  JSON object { "action": "...", ... }  (Anthropic computer-tool action shape)
  Output:  a single line of JSON on stdout: {"ok":true,...} or {"ok":false,"error":"..."}

  All coordinates are PHYSICAL screen pixels of the virtual desktop.
#>
param([Parameter(Mandatory = $true)][string]$RequestFile)

$ErrorActionPreference = 'Stop'
# PowerShell defaults to the console's OEM codepage; window titles then arrive as
# undecodable bytes on the WSL side. Force UTF-8 on the way out.
$OutputEncoding = New-Object System.Text.UTF8Encoding $false
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding $false
Add-Type -AssemblyName System.Windows.Forms, System.Drawing

Add-Type @"
using System;
using System.Runtime.InteropServices;
using System.Text;

public static class CU {
    [DllImport("user32.dll")] public static extern bool SetProcessDPIAware();
    [DllImport("user32.dll")] public static extern bool SetCursorPos(int X, int Y);
    [DllImport("user32.dll")] public static extern bool GetCursorPos(out POINT p);
    [DllImport("user32.dll")] public static extern void mouse_event(uint f, int dx, int dy, int data, IntPtr extra);
    [DllImport("user32.dll")] public static extern void keybd_event(byte vk, byte scan, uint flags, IntPtr extra);
    [DllImport("user32.dll")] public static extern short VkKeyScan(char ch);
    [DllImport("user32.dll")] public static extern uint SendInput(uint n, INPUT[] inputs, int size);
    [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
    [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr h);
    [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr h, int cmd);
    [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr h);
    [DllImport("user32.dll")] public static extern bool IsIconic(IntPtr h);
    [DllImport("user32.dll", CharSet = CharSet.Unicode)] public static extern int  GetWindowTextLength(IntPtr h);
    [DllImport("user32.dll", CharSet = CharSet.Unicode)] public static extern int  GetWindowText(IntPtr h, StringBuilder s, int n);
    [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr h, out RECT r);
    [DllImport("user32.dll")] public static extern bool EnumWindows(EnumProc cb, IntPtr p);
    [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr h, out uint pid);

    public delegate bool EnumProc(IntPtr h, IntPtr p);
    [StructLayout(LayoutKind.Sequential)] public struct POINT { public int X; public int Y; }
    [StructLayout(LayoutKind.Sequential)] public struct RECT { public int L, T, R, B; }
    [StructLayout(LayoutKind.Sequential)] public struct KEYBDINPUT {
        public ushort wVk; public ushort wScan; public uint dwFlags; public uint time; public IntPtr dwExtraInfo;
    }
    // Size=40 matches the real x64 INPUT union; ki lands at offset 8 via IntPtr alignment.
    [StructLayout(LayoutKind.Sequential, Size = 40)] public struct INPUT {
        public uint type; public KEYBDINPUT ki;
    }
}
"@ -ReferencedAssemblies System.Drawing

[void][CU]::SetProcessDPIAware()

# ---- constants ---------------------------------------------------------------
$MOUSE = @{ ldown = 0x02; lup = 0x04; rdown = 0x08; rup = 0x10; mdown = 0x20; mup = 0x40; wheel = 0x0800; hwheel = 0x1000 }
$KEYUP = 0x0002
$UNICODE = 0x0004

# xdotool / X11 key names -> Windows virtual key codes (Claude emits these names)
$VK = @{
  'return' = 0x0D; 'enter' = 0x0D; 'kp_enter' = 0x0D; 'tab' = 0x09; 'escape' = 0x1B; 'esc' = 0x1B
  'backspace' = 0x08; 'delete' = 0x2E; 'space' = 0x20; 'insert' = 0x2D
  'home' = 0x24; 'end' = 0x23; 'page_up' = 0x21; 'prior' = 0x21; 'page_down' = 0x22; 'next' = 0x22
  'left' = 0x25; 'up' = 0x26; 'right' = 0x27; 'down' = 0x28
  'ctrl' = 0x11; 'control' = 0x11; 'alt' = 0x12; 'shift' = 0x10
  'super' = 0x5B; 'win' = 0x5B; 'cmd' = 0x5B; 'meta' = 0x5B
  'capslock' = 0x14; 'numlock' = 0x90; 'printscreen' = 0x2C; 'print' = 0x2C; 'menu' = 0x5D; 'pause' = 0x13
  'f1' = 0x70; 'f2' = 0x71; 'f3' = 0x72; 'f4' = 0x73; 'f5' = 0x74; 'f6' = 0x75
  'f7' = 0x76; 'f8' = 0x77; 'f9' = 0x78; 'f10' = 0x79; 'f11' = 0x7A; 'f12' = 0x7B
}

# ---- helpers -----------------------------------------------------------------
function Get-VirtualScreen { [System.Windows.Forms.SystemInformation]::VirtualScreen }

function Resolve-Vk([string]$name) {
  $n = $name.ToLower()
  if ($VK.ContainsKey($n)) { return @{ vk = $VK[$n]; shift = $false } }
  if ($n.Length -eq 1) {
    $s = [CU]::VkKeyScan($name[0])
    if ($s -eq -1) { throw "unmappable key '$name'" }
    return @{ vk = ($s -band 0xFF); shift = (($s -shr 8) -band 1) -eq 1 }
  }
  throw "unknown key name '$name'"
}

function Send-KeyCombo([string]$combo, [double]$holdSeconds = 0) {
  # "ctrl+s", "alt+Tab", "shift+Home", "Return"
  $parts = $combo -split '\+' | Where-Object { $_ -ne '' }
  $mods = @(); $mainKey = $null
  foreach ($p in $parts) {
    $lp = $p.ToLower()
    if ($lp -in @('ctrl', 'control', 'alt', 'shift', 'super', 'win', 'cmd', 'meta')) { $mods += $VK[$lp] } else { $mainKey = $p }
  }
  $needShift = $false
  $mainVk = $null
  if ($null -ne $mainKey) { $r = Resolve-Vk $mainKey; $mainVk = $r.vk; $needShift = $r.shift }
  if ($needShift -and ($VK['shift'] -notin $mods)) { $mods += $VK['shift'] }

  foreach ($m in $mods) { [CU]::keybd_event([byte]$m, 0, 0, [IntPtr]::Zero) }
  if ($null -ne $mainVk) { [CU]::keybd_event([byte]$mainVk, 0, 0, [IntPtr]::Zero) }
  if ($holdSeconds -gt 0) { Start-Sleep -Milliseconds ([int]($holdSeconds * 1000)) }
  if ($null -ne $mainVk) { [CU]::keybd_event([byte]$mainVk, 0, $KEYUP, [IntPtr]::Zero) }
  [array]::Reverse($mods)
  foreach ($m in $mods) { [CU]::keybd_event([byte]$m, 0, $KEYUP, [IntPtr]::Zero) }
}

function Send-UnicodeText([string]$text) {
  # KEYEVENTF_UNICODE sends the literal character - immune to SendKeys escaping and layout issues.
  foreach ($ch in $text.ToCharArray()) {
    if ($ch -eq "`n" -or $ch -eq "`r") { Send-KeyCombo 'Return'; continue }
    if ($ch -eq "`t") { Send-KeyCombo 'Tab'; continue }
    $down = New-Object CU+INPUT; $down.type = 1
    $kd = New-Object CU+KEYBDINPUT; $kd.wVk = 0; $kd.wScan = [uint16][char]$ch; $kd.dwFlags = $UNICODE
    $down.ki = $kd
    $up = New-Object CU+INPUT; $up.type = 1
    $ku = New-Object CU+KEYBDINPUT; $ku.wVk = 0; $ku.wScan = [uint16][char]$ch; $ku.dwFlags = $UNICODE -bor $KEYUP
    $up.ki = $ku
    $sz = [System.Runtime.InteropServices.Marshal]::SizeOf([type]([CU+INPUT]))
    [void][CU]::SendInput(2, [CU+INPUT[]]@($down, $up), $sz)
    Start-Sleep -Milliseconds 8
  }
}

function Move-To([int]$x, [int]$y) { [void][CU]::SetCursorPos($x, $y); Start-Sleep -Milliseconds 20 }

function Invoke-Click([string]$button, [int]$times) {
  $d = $MOUSE["$($button)down"]; $u = $MOUSE["$($button)up"]
  for ($i = 0; $i -lt $times; $i++) {
    [CU]::mouse_event($d, 0, 0, 0, [IntPtr]::Zero)
    Start-Sleep -Milliseconds 25
    [CU]::mouse_event($u, 0, 0, 0, [IntPtr]::Zero)
    if ($i -lt $times - 1) { Start-Sleep -Milliseconds 90 }
  }
}

function Save-Screenshot([int]$x, [int]$y, [int]$w, [int]$h) {
  $dir = Join-Path $env:TEMP 'amplifier-computer-use'
  New-Item -ItemType Directory -Force -Path $dir | Out-Null
  Get-ChildItem $dir -Filter 'shot-*.png' -ErrorAction SilentlyContinue |
    Where-Object { $_.LastWriteTime -lt (Get-Date).AddMinutes(-30) } | Remove-Item -Force -ErrorAction SilentlyContinue
  $path = Join-Path $dir ("shot-{0}.png" -f ([guid]::NewGuid().ToString('N')))
  $bmp = New-Object System.Drawing.Bitmap $w, $h
  $g = [System.Drawing.Graphics]::FromImage($bmp)
  try {
    $g.CopyFromScreen($x, $y, 0, 0, (New-Object System.Drawing.Size($w, $h)))
    $bmp.Save($path, [System.Drawing.Imaging.ImageFormat]::Png)
  } finally { $g.Dispose(); $bmp.Dispose() }
  return $path
}

function Get-Coord($req, [string]$name) {
  $c = $req.$name
  if ($null -eq $c) { throw "action '$($req.action)' requires '$name'" }
  return @([int]$c[0], [int]$c[1])
}

# ---- dispatch ----------------------------------------------------------------
$out = @{ ok = $true }
try {
  $req = Get-Content -Raw -LiteralPath $RequestFile | ConvertFrom-Json
  $action = "$($req.action)".ToLower()
  $vs = Get-VirtualScreen

  switch ($action) {
    'screenshot' {
      $out.path = Save-Screenshot $vs.X $vs.Y $vs.Width $vs.Height
      $out.width = $vs.Width; $out.height = $vs.Height
    }
    'zoom' {
      $c = $req.coordinate
      if ($null -ne $req.region) { $c = $req.region }
      if (($null -eq $c -or $c.Count -lt 4) -and $null -ne $req.start_coordinate -and $null -ne $req.coordinate) {
        $c = @($req.start_coordinate[0], $req.start_coordinate[1], $req.coordinate[0], $req.coordinate[1])
      }
      if ($null -eq $c -or $c.Count -lt 4) { throw "zoom requires coordinate [x1,y1,x2,y2]" }
      $x1 = [int]$c[0]; $y1 = [int]$c[1]; $x2 = [int]$c[2]; $y2 = [int]$c[3]
      $out.path = Save-Screenshot $x1 $y1 ([Math]::Max(1, $x2 - $x1)) ([Math]::Max(1, $y2 - $y1))
      $out.width = $x2 - $x1; $out.height = $y2 - $y1
    }
    'cursor_position' {
      $p = New-Object CU+POINT; [void][CU]::GetCursorPos([ref]$p)
      $out.coordinate = @($p.X, $p.Y)
    }
    'mouse_move' { $c = Get-Coord $req 'coordinate'; Move-To $c[0] $c[1]; $out.coordinate = $c }
    'left_click' { $c = Get-Coord $req 'coordinate'; Move-To $c[0] $c[1]; Invoke-Click 'l' 1; $out.coordinate = $c }
    'right_click' { $c = Get-Coord $req 'coordinate'; Move-To $c[0] $c[1]; Invoke-Click 'r' 1; $out.coordinate = $c }
    'middle_click' { $c = Get-Coord $req 'coordinate'; Move-To $c[0] $c[1]; Invoke-Click 'm' 1; $out.coordinate = $c }
    'double_click' { $c = Get-Coord $req 'coordinate'; Move-To $c[0] $c[1]; Invoke-Click 'l' 2; $out.coordinate = $c }
    'triple_click' { $c = Get-Coord $req 'coordinate'; Move-To $c[0] $c[1]; Invoke-Click 'l' 3; $out.coordinate = $c }
    'left_mouse_down' {
      if ($req.coordinate) { $c = Get-Coord $req 'coordinate'; Move-To $c[0] $c[1] }
      [CU]::mouse_event($MOUSE.ldown, 0, 0, 0, [IntPtr]::Zero)
    }
    'left_mouse_up' {
      if ($req.coordinate) { $c = Get-Coord $req 'coordinate'; Move-To $c[0] $c[1] }
      [CU]::mouse_event($MOUSE.lup, 0, 0, 0, [IntPtr]::Zero)
    }
    'left_click_drag' {
      $s = if ($req.start_coordinate) { Get-Coord $req 'start_coordinate' } else { $p = New-Object CU+POINT; [void][CU]::GetCursorPos([ref]$p); @($p.X, $p.Y) }
      $e = Get-Coord $req 'coordinate'
      Move-To $s[0] $s[1]
      [CU]::mouse_event($MOUSE.ldown, 0, 0, 0, [IntPtr]::Zero); Start-Sleep -Milliseconds 60
      $steps = 24
      for ($i = 1; $i -le $steps; $i++) {
        Move-To ([int]($s[0] + ($e[0] - $s[0]) * $i / $steps)) ([int]($s[1] + ($e[1] - $s[1]) * $i / $steps))
      }
      Start-Sleep -Milliseconds 60
      [CU]::mouse_event($MOUSE.lup, 0, 0, 0, [IntPtr]::Zero)
      $out.start = $s; $out.end = $e
    }
    'scroll' {
      if ($req.coordinate) { $c = Get-Coord $req 'coordinate'; Move-To $c[0] $c[1] }
      $dir = "$(if ($req.scroll_direction) { $req.scroll_direction } else { $req.direction })".ToLower()
      $amtRaw = if ($null -ne $req.scroll_amount) { $req.scroll_amount } elseif ($null -ne $req.amount) { $req.amount } else { 3 }
      $clicks = [Math]::Max(1, [int]$amtRaw)
      for ($i = 0; $i -lt $clicks; $i++) {
        switch ($dir) {
          'up' { [CU]::mouse_event($MOUSE.wheel, 0, 0, 120, [IntPtr]::Zero) }
          'down' { [CU]::mouse_event($MOUSE.wheel, 0, 0, -120, [IntPtr]::Zero) }
          'right' { [CU]::mouse_event($MOUSE.hwheel, 0, 0, 120, [IntPtr]::Zero) }
          'left' { [CU]::mouse_event($MOUSE.hwheel, 0, 0, -120, [IntPtr]::Zero) }
          default { throw "scroll direction must be up/down/left/right (got '$dir')" }
        }
        Start-Sleep -Milliseconds 40
      }
      $out.direction = $dir; $out.clicks = $clicks
    }
    'key' {
      $t = if ($req.text) { $req.text } else { $req.key }
      if (-not $t) { throw "key requires 'text'" }
      foreach ($combo in ("$t" -split '\s+' | Where-Object { $_ })) { Send-KeyCombo $combo; Start-Sleep -Milliseconds 30 }
      $out.keys = "$t"
    }
    'hold_key' {
      $t = if ($req.text) { $req.text } else { $req.key }
      $dur = if ($null -ne $req.duration) { [double]$req.duration } else { 1.0 }
      if ($dur -gt 10) { throw "hold_key duration capped at 10s" }
      Send-KeyCombo "$t" $dur
      $out.keys = "$t"; $out.duration = $dur
    }
    'type' {
      $t = "$($req.text)"
      if ([string]::IsNullOrEmpty($t)) { throw "type requires 'text'" }
      Send-UnicodeText $t
      $out.typed = $t.Length
    }
    'wait' {
      $dur = if ($null -ne $req.duration) { [double]$req.duration } else { 1.0 }
      if ($dur -gt 30) { throw "wait duration capped at 30s" }
      Start-Sleep -Milliseconds ([int]($dur * 1000))
      $out.duration = $dur
    }
    'screen_info' {
      $out.width = $vs.Width; $out.height = $vs.Height; $out.x = $vs.X; $out.y = $vs.Y
      $out.screens = @([System.Windows.Forms.Screen]::AllScreens | ForEach-Object {
          @{ name = $_.DeviceName; primary = $_.Primary; bounds = @($_.Bounds.X, $_.Bounds.Y, $_.Bounds.Width, $_.Bounds.Height) }
        })
    }
    'get_clipboard' {
      # Get-Clipboard/Set-Clipboard handle the STA requirement internally; the
      # System.Windows.Forms.Clipboard API does not and throws under -File.
      $t = Get-Clipboard -Raw -ErrorAction SilentlyContinue
      $out.text = "$t"
    }
    'set_clipboard' {
      $val = "$($req.text)"
      Set-Clipboard -Value $val
      $out.set = $val.Length
    }
    'list_windows' {
      $list = New-Object System.Collections.Generic.List[object]
      $cb = [CU+EnumProc] {
        param($h, $p)
        if ([CU]::IsWindowVisible($h)) {
          $len = [CU]::GetWindowTextLength($h)
          if ($len -gt 0) {
            $sb = New-Object System.Text.StringBuilder ($len + 1)
            [void][CU]::GetWindowText($h, $sb, $sb.Capacity)
            $r = New-Object CU+RECT; [void][CU]::GetWindowRect($h, [ref]$r)
            $procId = 0; [void][CU]::GetWindowThreadProcessId($h, [ref]$procId)
            if (($r.R - $r.L) -gt 0 -and ($r.B - $r.T) -gt 0) {
              $list.Add(@{
                  handle = $h.ToString(); title = $sb.ToString(); pid = [int]$pid
                  minimized = [bool][CU]::IsIconic($h); rect = @($r.L, $r.T, $r.R, $r.B)
                }) | Out-Null
            }
          }
        }
        return $true
      }
      [void][CU]::EnumWindows($cb, [IntPtr]::Zero)
      $fg = [CU]::GetForegroundWindow()
      $out.windows = $list.ToArray(); $out.foreground = $fg.ToString()
    }
    'focus_window' {
      $h = [IntPtr][int64]"$($req.handle)"
      if ([CU]::IsIconic($h)) { [void][CU]::ShowWindow($h, 9) }   # SW_RESTORE
      # ALT nudge defeats Windows' foreground-lock heuristic
      [CU]::keybd_event(0x12, 0, 0, [IntPtr]::Zero)
      [void][CU]::SetForegroundWindow($h)
      [CU]::keybd_event(0x12, 0, $KEYUP, [IntPtr]::Zero)
      Start-Sleep -Milliseconds 250
      $out.focused = ([CU]::GetForegroundWindow()).ToString()
      $out.requested = $h.ToString()
    }
    default { throw "unknown action '$($req.action)'" }
  }
}
catch {
  $out = @{ ok = $false; error = "$($_.Exception.Message)" }
}

$out | ConvertTo-Json -Depth 6 -Compress
