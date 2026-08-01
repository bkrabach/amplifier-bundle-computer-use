# Coexistence Technical Spikes — Probe Results

Empirical answers only. No design proposals. Where a platform makes something
impossible, that is reported plainly with the evidence that shows it.

**Hosts used:**
- Local: `spark-1` (aarch64 Linux), real GNOME session on `DISPLAY=:1` (a session
  nobody is looking at, per authorization) and a scratch `Xvfb :99` started and
  torn down by these probes.
- Remote: `brians-macbook-pro-os` (macOS 25.6.0 arm64) and `alienware-r13` (WSL2
  side of a live Win11 desktop) — read-only or zero-visible-impact commands only,
  per hard safety constraints. No window was opened, no focus changed, no click
  or keystroke sent, and no visible pointer movement occurred on either host.

**SSH discipline used throughout:** the mandated `timeout`+backgrounded-`ssh`+
poll-for-rc pattern (helper script `/tmp/safe_ssh.sh`), `scp` on its own channel.
No bare blocking `ssh` was run.

---

## U1b — Windows idle confounding (zero-impact event)

**Question:** Does `SendInput`/`mouse_event` reset `GetLastInputInfo`, verified
empirically with a zero-visible-impact event only?

Two zero-impact primitives were tested, because they turned out to behave
differently — that difference is itself the finding.

### Test 1: `SetCursorPos` to the cursor's own current coordinates

Script (`u1b_win_idle.ps1`, relevant excerpt):
```powershell
$idle1 = [IdleU1B]::GetIdleMs()
$pt = New-Object IdleU1B+POINT
[IdleU1B]::GetCursorPos([ref]$pt) | Out-Null
$result = [IdleU1B]::SetCursorPos($pt.X, $pt.Y)   # move cursor to its OWN position
Start-Sleep -Milliseconds 200
$idle2 = [IdleU1B]::GetIdleMs()
```
Run via:
```
scp u1b_win_idle.ps1 alienware-r13:/tmp/u1b_win_idle.ps1
ssh alienware-r13 '/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe \
    -NoProfile -ExecutionPolicy Bypass -File $(wslpath -w /tmp/u1b_win_idle.ps1)'
```

**Output:**
```
IDLE_BEFORE_MS=18421
CURSOR_POS_X=1414 Y=930
SETCURSORPOS_RESULT=True
IDLE_AFTER_MS=18656
CURSOR_POS_AFTER_X=1414 Y=930
```
Idle went **up** by ~235ms (matching the 200ms sleep + overhead) — it was **not
reset**. `SetCursorPos` does not go through the input queue, so it never
updates `dwTime` in `GetLastInputInfo`.

### Test 2: `SendInput` with a relative move of `dx=0, dy=0`

Same zero visible displacement, but via the actual Win32 input-injection path
(`MOUSEEVENTF_MOVE`, the API family production synthetic-driving code uses),
run the same way:

**Output:**
```
IDLE_BEFORE_MS=10235
SENDINPUT_RETURNED=1 (1=success, 0=failure)
LAST_WIN32_ERROR=203
IDLE_AFTER_MS=219
```
Idle dropped from 10235ms to 219ms — **reset**, even though the cursor never
visibly moved.

**Verdict: ANSWERED-YES**, with a nuance the task didn't anticipate: the
zero-impact primitive matters. `SetCursorPos` (position-only) does **not**
confound idle. `SendInput`/`mouse_event` (the real input-injection path,
still zero-impact at `dx=dy=0`) **does** confound idle, confirming documented
Windows behavior empirically. Any production driving code built on `SendInput`
will self-confound idle-based presence detection exactly as Linux XTEST and
macOS `CGEventPost` do.

---

## U1c — Is the idle confound correctable?

**Question:** If the agent records `T_inject`, then later reads idle `I` at
`T_now`, does `T_now - I` correctly identify a *later, independent* human input
event rather than collapsing back to the agent's own `T_inject`?

Tested on local Linux (`DISPLAY=:1`, python-xlib + `MIT-SCREEN-SAVER`
extension, confirmed present: `has_extension('MIT-SCREEN-SAVER') == True`).

**Method:** three independent process invocations, each writing its own
wall-clock timestamp to a file so no shared state could leak between them:
1. `u1c_inject.py agent 5 5 t_inject.txt` — records `T_inject`, injects an XTEST
   relative motion.
2. `sleep 3`
3. `u1c_inject.py human 10 10 t_human.txt` — a **separate process**, simulating
   independent human input, records `T_human`.
4. `u1c_read_idle.py` — reads idle via `screensaver_query_info().idle`,
   computes `inferred_last_input = T_now - idle_ms/1000`.

**Output (single run):**
```
ROLE=agent T_INJECT=1785616783.464067
ROLE=human T_INJECT=1785616786.498972      # (T_human, independent process)
T_NOW=1785616786.538841 IDLE_MS=40 INFERRED_LAST_INPUT=1785616786.498841
```
- `INFERRED_LAST_INPUT` vs `T_HUMAN`: diff = **-0.131 ms**
- `INFERRED_LAST_INPUT` vs `T_INJECT`: diff = **+3034.8 ms** (matches the 3s sleep)

The arithmetic correctly points at the human's timestamp, not the agent's.

**Jitter over 5 iterations** (1.5s wait each, diff = inferred − T_human, ms):
```
iter=1  DIFF_MS=-0.902
iter=2  DIFF_MS=-9.938
iter=3  DIFF_MS=-0.744
iter=4  DIFF_MS=-0.267
iter=5  DIFF_MS=0.002
```
Typical reconciliation error is **sub-millisecond**; one outlier at ~10ms
(iteration 2), most plausibly Python-process-spawn scheduling jitter rather
than a limit of the idle counter itself (`MIT-SCREEN-SAVER` reports in whole
milliseconds).

**Verdict: ANSWERED-YES.** The self-injection-vs-human-input arithmetic is
sound on Linux: resolution ≈1ms, occasional ~10ms outliers from process
scheduling, not from the underlying idle mechanism. This is the single most
load-bearing result of the whole spike set — see summary below.

---

## U3 — Screen lock / session-locked detection (read-only, from an SSH/agent context)

### Linux

```
loginctl list-sessions
loginctl show-session 2297   # (own session, tty2, x11, matches SCRATCH.md)
DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/1000/bus" \
  gdbus call --session --dest org.gnome.ScreenSaver \
  --object-path /org/gnome/ScreenSaver --method org.gnome.ScreenSaver.GetActive
```
**Output:**
```
TTY=tty2
Remote=no
Type=x11
Active=yes
State=active
LockedHint=no

(false,)            # ScreenSaver.GetActive
(uint32 0,)          # ScreenSaver.GetActiveTime
```
**Verdict: ANSWERED-YES.** Both `loginctl show-session` (`LockedHint`) and the
GNOME ScreenSaver D-Bus interface are readable and give a clean, direct
locked/unlocked signal.

### macOS

```
who
pmset -g
pmset -g powerstate IODisplayWrangler
ioreg -n Root -d1 -a | grep -A2 -i "screenislocked\|cgssession"
python3 -c 'import subprocess, plistlib; ... find_key(d, "IOConsoleUsers") ...'
```
**Output:**
```
brkrabac         console      Jul 30 19:20        # who: console session present

Currently in use: ... sleep 0 (sleep prevented by powerd, Microsoft Scout) ...

Internal failure: Failed to get power state information   # pmset -g powerstate FAILS over SSH

<key>CGSSessionUniqueSessionUUID</key><string>4A92555C-...</string>
<key>kCGSSessionOnConsoleKey</key><true/>
<key>kCGSSessionUserNameKey</key><string>brkrabac</string>
...
root.IOConsoleUsers => [{'CGSSessionUniqueSessionUUID': '4A92555C-4287-4253-9FB0-4AD7476FE97A',
  'kCGSSessionOnConsoleKey': True, 'kCGSSessionUserIDKey': 501,
  'kCGSSessionUserNameKey': 'brkrabac', 'kCGSessionLoginDoneKey': True, ...}]
```
`CGSessionCopyCurrentDictionary()` is dead over SSH (already established).
`pmset -g powerstate IODisplayWrangler` also **fails** over SSH ("Internal
failure"). But `ioreg -n Root -d1 -a`, parsed with `plistlib`, **does** expose
the `IOConsoleUsers` property bag (the same `CGSSession*` keys
`CGSessionCopyCurrentDictionary` would have returned) — readable purely at the
IORegistry/kernel level, not through a WindowServer connection, so it survives
the SSH context that kills the Quartz API.

There is no `CGSSessionScreenIsLocked` key present in this dump — because the
screen is currently unlocked and, per Apple's documented behavior for this
key, it's absent/false when unlocked and would need to be checked while
actually locked to see the key populate.

**Verdict: PARTIAL.** The mechanism (`ioreg` + `IOConsoleUsers`) is proven to
work over SSH and returns live, correct session identity/console-state data.
The specific "is-it-currently-locked" bit could not be exercised because doing
so would require locking the live user's screen — explicitly out of bounds
here. Left honestly unverified rather than risking the user's session.

### Windows (via WSL interop)

```powershell
$sid = [SessionU3]::WTSGetActiveConsoleSessionId()
quser
$h = [SessionU3]::OpenInputDesktop(0, $false, 0x0100)
Get-Process explorer | Select Id, SessionId
```
**Output:**
```
ACTIVE_CONSOLE_SESSION_ID=1
CURRENT_PROCESS_SESSION_ID=1
 USERNAME     SESSIONNAME   ID  STATE   IDLE TIME  LOGON TIME
>brkrabac     console        1  Active  none       7/24/2026 7:16 AM
OPEN_INPUT_DESKTOP=SUCCESS handle=2508 (interactive desktop accessible -> NOT locked)
   Id SessionId
10504         1
```
**Verdict: ANSWERED-YES.** `WTSGetActiveConsoleSessionId`, `quser`, and
`OpenInputDesktop` are all readable/callable from the WSL-interop context and
agree: session 1, active, input desktop reachable (would fail/return null if
the workstation were locked, since the secure desktop takes over the input
desktop while locked).

---

## U4 — Always-on-top overlay without stealing focus

### Linux X11

**Real GNOME session, `DISPLAY=:1` (focus-stealing test):**

Script created an X11 **override-redirect** window (bypasses the window
manager's focus/decoration policy entirely) at `(50,50,300,80)`, mapped it,
and compared input focus / `_NET_ACTIVE_WINDOW` before and after:
```
=== BEFORE overlay creation ===
input_focus_window_id=2097163 name=None
_NET_ACTIVE_WINDOW=0

=== AFTER overlay mapped ===
input_focus_window_id=2097163 name=None
_NET_ACTIVE_WINDOW=0
overlay_window_id=60817408
FOCUS_CHANGED=False
ACTIVE_WINDOW_CHANGED=False
OVERLAY_IS_FOCUS_TARGET=False
OVERLAY_IS_ACTIVE_WINDOW=False
```
**Focus-stealing verdict: ANSWERED-YES it can be avoided** — creating and
mapping the window changed neither the input focus nor `_NET_ACTIVE_WINDOW`.

**Secondary, unplanned finding on `:1`:** a screenshot taken to visually
confirm the white overlay box instead showed it obscured — pixel-sampled at
`(29,29,29)` (dark grey) across the entire expected `(50–350, 50–130)`
rectangle, not white. Root cause: a **stale GNOME polkit authentication
prompt** was live on that display (`ps` showed
`/usr/lib/polkit-1/polkit-agent-helper-1 bkrabach` running since 09:59, hours
before this probe). `query_tree` confirmed our window was mapped and was
**last in stacking order (topmost among ordinary client windows)** — yet it
still wasn't visible. This means: **a GNOME-Shell-composited modal (polkit
prompts, the lock screen shield, etc.) is rendered in a compositor layer above
the entire client window stack, and can visually obscure an override-redirect
window regardless of X11 stacking order.** The focus-stealing result above is
unaffected by this (focus/active-window tracking is independent of visual
occlusion), but visibility guarantees on a compositing desktop are weaker than
raw X stacking implies.

**Clean re-test on scratch `Xvfb :99`** (no compositor, no shell, so no such
confound) — same override-redirect window creation, then pixel-exact
screenshot analysis:
```
background (0,0): (0, 0, 0)          # Xvfb default black background
inside overlay (200,90): (255, 255, 255)   # solid white — our window, rendered
```
**Verdict: ANSWERED-YES**, definitively, in a compositor-free environment: the
window is created, appears, and does not take focus. Combined with the `:1`
result, the full picture is: **override-redirect achieves both goals (visible,
non-focus-stealing) against ordinary client windows on every Linux
compositor/WM tested, but a desktop shell's own modal UI layer can still cover
it — worth carrying into the design.**

### macOS (via SSH)

Non-visual probe only, per the hard safety rule — no window was created.
```
launchctl managername
launchctl asuser $(id -u) launchctl managername
python3 -c "import Quartz"
```
**Output:**
```
Background                                                   # our own SSH session's launchd domain
Could not switch to audit session 0x186a2: 1: Operation not permitted
ModuleNotFoundError: No module named 'Quartz'                # no PyObjC AppKit/Quartz installed
```
An SSH-launched process is bound to the **`Background`** launchd management
domain, not the **`Aqua`** (GUI) domain that owns the WindowServer connection.
Attempting to re-home into the console user's GUI/audit session with
`launchctl asuser` is **rejected outright** ("Operation not permitted") — this
is not a missing-dependency problem, it's a hard session-boundary rejection.
No PyObjC framework is installed to even attempt a direct AppKit call, and
installing one would leave a persistent modification on the user's live
machine, so that avenue was not pursued.

**Verdict: ANSWERED-NO** for a plain SSH-launched process. Per Apple's session
model, creating a window on the console user's desktop from this context would
require a different launch mechanism entirely — e.g. a `LaunchAgent` plist
registered so that **launchd itself** starts the process inside
`gui/<uid>` (which requires setup before the fact, not a live reparent of an
already-running SSH process). That mechanism was not attempted here (would be
a persistent change to the user's machine, out of scope for a probe).

### Windows (via WSL interop)

No window was created — the hard safety rule explicitly forbids "windows
opening" on `alienware-r13`. Instead, a non-visual diagnostic equivalent to
the macOS `launchctl managername` check was used: which window station/desktop
is this process attached to?
```powershell
[DesktopU4]::GetProcessWindowStation()   # -> UOI_NAME
[DesktopU4]::GetThreadDesktop($tid)      # -> UOI_NAME
```
**Output:**
```
WINDOW_STATION_NAME=WinSta0
THREAD_DESKTOP_NAME=Default (ok=1)
SESSION_ID=1
IS_INTERACTIVE_PROC=True
```
`WinSta0\Default` is the interactive window station/desktop of the console
session (a true background Windows service would report a non-interactive
station such as `Service-0x0-3e7$`). Combined with U3's
`ACTIVE_CONSOLE_SESSION_ID=1` and `CURRENT_PROCESS_SESSION_ID=1` match, this is
the standard, documented signature of a process that **can** create UI on the
interactive desktop.

**Verdict: ANSWERED-YES (feasibility only, via non-visual diagnostic)** — a
WSL-interop-launched process already runs in the same window station/desktop/
session as the interactive user. Actual window creation was not tested, per
the safety rule.

---

## U5 — Overlay accepts a pause/cancel click without stealing focus

Tested on `Xvfb :99` (clean environment; extends directly from the U4 Linux
result).

**Method:** created a regular "app" window standing in for whatever the agent
is driving, explicitly gave it input focus (`XSetInputFocus`), then created a
second override-redirect overlay window at a different screen position with
`ButtonPressMask`. Injected a synthetic click (`XTEST` `MotionNotify` +
`ButtonPress` + `ButtonRelease`) at the overlay's on-screen coordinates, then
checked (a) whether the overlay's event queue received the click and (b)
whether focus moved off the app window.

**Output:**
```
FOCUS_BEFORE_CLICK: window_id=2097152  (expect == app.id=2097152)
OVERLAY_RECEIVED_BUTTONPRESS=True at (148,38)
FOCUS_AFTER_CLICK: window_id=2097152  (expect still == app.id=2097152)
FOCUS_CHANGED_BY_CLICK=False
FOCUS_STOLEN_BY_OVERLAY=False
```
**Verdict: ANSWERED-YES on Linux.** The overlay window received the
`ButtonPress` event (proving it can be clicked), and the app window's input
focus was completely unaffected by the click — there is no window manager
involved to reassign click-to-focus for an override-redirect window, so
nothing reassigns focus unless the code explicitly calls
`XSetInputFocus`/`grab` itself. Not probed on macOS/Windows: U4 already
determined overlay creation itself is infeasible over plain SSH on macOS, and
creating any clickable window on `alienware-r13` was excluded by the hard
safety rule.

---

## U6 — Overlay lifecycle: SIGKILL cleanup and show/hide latency

Tested on `Xvfb :99`.

### SIGKILL cleanup

```
kill -9 <overlay-owning-pid>
# then query the window that pid had created:
```
**Output:**
```
process check after SIGKILL: confirmed: python process is dead
POST_KILL: window DESTROYED (BadWindow error querying it) -- ghost-free cleanup CONFIRMED
winid 2097152 still in root children: False
total children now: 0
```
**Verdict: ANSWERED-YES — good news.** The X server automatically destroys
every resource (including override-redirect windows) owned by a client
connection the instant that connection dies, whether the process exits
cleanly or is `SIGKILL`ed. No ghost indicator is possible on X11 by
construction — the window's lifetime is bound to the socket, not to any
explicit cleanup code.

### Show/hide latency

Measured the interval between issuing `map()`/`unmap()` and receiving the
corresponding `MapNotify`/`UnmapNotify` confirmation from the X server (5
repetitions after an initial warm-up pair):
```
SHOW_LATENCY_MS=0.063 got_map_notify=True
HIDE_LATENCY_MS=0.342 got_unmap_notify=True
SHOW_LATENCIES_MS= ['0.077', '0.345', '0.365', '0.268', '0.352']
HIDE_LATENCIES_MS= ['0.359', '0.458', '0.305', '0.198', '0.247']
SHOW_AVG_MS=0.281  HIDE_AVG_MS=0.313
```
**Verdict: ANSWERED-YES — sub-millisecond**, on a local/headless X server.
This is a local-IPC-bound measurement; it does not include any compositor
paint latency (the confounded `:1` environment was not re-measured for
latency) or any network hop (a genuinely remote/SSH-tunneled display was not
measured). For "must appear before each action," the server-side latency
itself is not the bottleneck on Linux; whatever the eventual design adds on
top (network, compositor paint) would dominate.

---

## U7 — Where must "paused" state live? (reasoning only, no probe)

Given U4–U6: the overlay-owning process is necessarily the same process that
holds the live connection to the display server (the X11 connection here;
the `WinSta0\Default` handle on Windows; whatever mechanism would eventually
reach `Aqua` on macOS). U6 showed that this connection is also exactly what
gets torn down — instantly and completely — the moment that process dies.
U5 showed the overlay can receive input (a click) independently of whatever
window the agent's own actions are targeting.

If "pause" is implemented as a signal the overlay process merely reports to
the agent/controller (e.g., a flag file or message the controller is expected
to check before injecting further input), then the guarantee is only as good
as the controller's cooperation — a buggy or compromised controller can keep
injecting input regardless, because the actual `XTEST`/`CGEventPost`/
`SendInput` calls still originate from, and are still fully authorized within,
the controller's own process.

For "paused" to be a property a buggy or compromised controller **cannot**
override, the flag cannot live inside the controller's own process/authority
at all. It must live in — and be enforced by — the same narrow, privileged
process that already necessarily owns the actual OS input-injection calls
(the display connection / window-station handle), with the higher-level agent
talking to that process through a narrow interface that does not include a
raw "inject this event" bypass. Concretely: the process that renders the
overlay and receives the pause click is the only correct place to also gate
the call site of every synthetic input event — not because it is convenient,
but because it is the only process both (a) already required to exist and
hold the relevant OS handle, per U4, and (b) proven in U6 to have a clean,
total, instantly-observable lifecycle. Putting the enforcement anywhere higher
in the stack (inside the agent's reasoning loop, a config flag, a "please
stop" message) makes pause a request instead of a guarantee.

---

## Summary table

| # | Question | Verdict |
|---|---|---|
| U1b | Does zero-impact synthetic input reset Windows idle? | **ANSWERED-YES** (for `SendInput`, the real driving path) / **ANSWERED-NO** (for `SetCursorPos` alone) — the primitive matters |
| U1c | Is the idle confound correctable via inject-timestamp arithmetic? | **ANSWERED-YES** — sub-ms typical resolution, ~10ms worst-case jitter (Linux) |
| U3 | Screen-lock detection from an SSH/agent context | **ANSWERED-YES** (Linux, Windows) / **PARTIAL** (macOS — mechanism proven readable over SSH via `ioreg`+`IOConsoleUsers`, but locked-state value not exercised without disrupting the live user) |
| U4 | Always-on-top overlay without stealing focus | **ANSWERED-YES** (Linux, both real GNOME and clean Xvfb — with the caveat that a GNOME-Shell-composited modal can still visually obscure it) / **ANSWERED-NO** (macOS, plain SSH — session/audit-domain boundary rejects it outright) / **ANSWERED-YES feasibility-only** (Windows via WSL interop — confirmed via non-visual window-station/desktop check, no window actually created per safety rule) |
| U5 | Overlay accepts click without stealing focus | **ANSWERED-YES** (Linux only — not probed on macOS/Windows per U4 results and safety rule) |
| U6 | Overlay lifecycle on SIGKILL + latency | **ANSWERED-YES** — ghost-free cleanup is automatic (X server tears down the connection's resources); sub-millisecond show/hide latency (local X, not representative of network/compositor-inclusive latency) |
| U7 | Where must pause state live? | Reasoning only — must live in, and be enforced by, the same privileged process that owns the actual input-injection call site, not the agent/controller's own process |

---

## Single most consequential finding for the design

**U1c, combined with U1b's nuance, is the load-bearing result.** Idle-based
presence detection is not merely confounded by the agent's own input (already
known) — it is **correctable with sub-millisecond precision** using
information the agent already has for free (its own injection timestamps),
proven with a genuinely independent second process standing in for a human,
not just a mental model. That means a real-time "is a human touching this
machine right now" signal is achievable on the presence-detection side without
needing the overlay/indicator work at all — timestamp reconciliation alone
can tell the agent "someone else just did something" within about a
millisecond, anywhere idle time is exposed (confirmed here on Linux; the same
`GetLastInputInfo` arithmetic should carry over to Windows given U1b, though
that specific reconciliation test was only run on Linux). This turns "presence
detection" from a fundamentally-broken idea (self-confounded) into a solved
problem, which changes the design's center of gravity: the overlay/pause-gate
work from U4–U7 stops being the *only* way to know a human is there, and
becomes specifically about giving the *human* a way to see and interrupt the
agent — not about detecting them in the first place.

---

## O1 — Does `osascript -e 'display dialog'` reach the console user from SSH?

The architect flagged this as the highest-value open probe: if it works, macOS gets
both an indicator AND a request/response channel at zero install cost, and the
"resident LaunchAgent" option evaporates.

```
$ ssh brkrabac@brians-macbook-pro-os \
    'osascript -e "display dialog \"...probe...\" buttons {\"Pause\",\"OK\"} \
       default button 2 giving up after 6"'
button returned:, gave up:true
exit rc=0
```

`gave up:true` is only returned by a dialog that actually **rendered and waited**.
It reached the console user's screen from a Background-domain SSH process, and it
returns which button was pressed — a complete round-trip channel, not just output.

**Verdict: ANSWERED-YES.** macOS CAN show a human-visible, human-answerable prompt
from SSH. Delivery is AppleScript/`osascript`, not a window the agent draws.

## O2 — Can an SSH-launched process create a visible NSWindow?

U4 concluded NO by inference from `launchctl asuser` being denied. Tested directly,
because `CGEventPost` demonstrably DOES reach WindowServer from that same process:

```
NSApplication: True
NSScreen.mainScreen(): <NSScreen: 0x8986fc640>
window ordered front; visible = True
O2 VERDICT: NO - not in on-screen window list (matches=0)
```

The window is created and even self-reports `visible=True`, but never appears in
`CGWindowListCopyWindowInfo(kCGWindowListOptionOnScreenOnly)`. It exists in a
detached context, not on the user's desktop.

**Verdict: ANSWERED-NO, now by direct test rather than inference.** Confirms U4.

### Combined consequence for the design

macOS is NOT "indicator impossible". It is "**agent-drawn overlay impossible,
system-drawn dialog possible**". Those are different constraints with different
designs. The persistent-overlay model (Linux/Windows) and the
modal-prompt model (macOS) are not the same UX and should not be forced into one.

---

## O5 — Is the human MASKED during sustained agent typing? (council's blocking question)

`tester-breaker` argued the detector's central safety claim fails in exactly the
regime that caused the incident: *"During any multi-character `type_text`... the
agent injects every ~50-100ms... margin stays inside the ±250ms GUARD band FOR THE
ENTIRE DURATION - a real human keystroke typed WHILE the agent is mid-operation is
never distinguishable from noise."* Two of six lenses voted FAIL on this alone.

Tested for real. Agent process injects XTEST motion every 60ms for 6s (type_text
cadence). A genuinely independent second process fires ONE event at t=3s. The
detector samples mid-gap and compares inferred-last-input against its own last
injection timestamp.

```
agent injecting every 60ms for 6s (type_text cadence); human fires once at t=3s
HUMAN_INJECT_AT 1785619846.195627
samples=98  detections=1
  detected non-agent input at 1785619846.216 margin=+25.1ms
VERDICT: DETECTOR SEES THE HUMAN MID-TYPING
```

98 samples, **exactly one** detection — the real one. No false positives across the
other 97 samples despite the agent injecting throughout. The detected timestamp
(…846.216) sits 21ms after the human process's own recorded inject (…846.195),
consistent with one sample interval.

**Verdict: ANSWERED — masking does NOT occur, but the council was right about the
cause.** Detection works at a **5ms** threshold. It would have been completely
masked at the design's proposed **±250ms** guard band, because the agent re-injects
every 60ms and the band never clears. The guard band is the defect, not the
mechanism.

**Design consequence:** the guard band must be on the order of single-digit
milliseconds, and `type_text` must expose per-keystroke injection timestamps rather
than one timestamp for the whole operation. A 250ms band silently disables human
detection for the entire duration of any typing action — the precise failure the
feature exists to prevent.
