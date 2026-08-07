# Band Lifetime — scoping the disclosure indicator to the activity it discloses

**Status:** **Shipped — revision 2, narrowed to the adversarial review's approved scope.** Revision 1 (below, mostly unchanged) proposed a trailing-window-plus-reaper-thread mechanism. A six-lens adversarial panel returned **5-of-6 FAIL** on that apparatus specifically, with a scoped exemption for the correctness core (the channel-keyed depth counter, the held-input ledger, bounded teardown). See **§0** for what shipped, what was cut, and why.
**Scope:** When the Linux/Windows disclosure band is raised and, the part that does not exist today, when it is lowered.
**Authority it must not contradict:** `docs/designs/coexistence.md` §7 (announcing the agent). This document **deliberately revises §7.2** and preserves §7.1, §7.3, §7.4, §7.5 and §7.6 unchanged. See §3 (wording updated in revision 2 to match the shipped, narrower mechanism).
**Companions:** `docs/designs/remote-transport.md` (transport, ledger, op classes)
**Explicitly out of scope:** `coexistence_guard.py`, `presence.py`, `halt_state.py`. This design depends only on their observable behaviour and proposes no change to any of them. Confirmed again in revision 2: closing the ledger-scoping and local-wiring gaps (§0.2) required no change to any of the three.
**Baseline:** `e9a7128`
**Date:** 2026-08-06 (revision 1), 2026-08-07 (revision 2 — shipped)

---

## 0. Revision 2 — what actually shipped, and why (read this first)

### 0.1 The panel's verdict, in one line

**Ship the channel-keyed depth counter and close the three mechanism gaps it exposed. Do not ship the reaper thread / lock / trailing-window `T` apparatus** — that piece FAILed 5-of-6 lenses as unearned complexity, and it waits behind a mechanically-measured Windows raise-cost gate (§11.2/§12), not a prose promise.

### 0.2 The three mechanism gaps the panel's own greps found, and their resolution

| # | Finding | Resolution |
|---|---|---|
| 1 | `HeldInputLedger.hold()` was called **only** from `remote_agent.py` — a local `left_mouse_down` had zero ledger enforcement (no deadman, no release on halt/pause/target-change). | **Closed.** `ComputerTool._hold_mouse_button`/`_release_mouse_button` (`__init__.py`) wire `left_mouse_down`/`left_mouse_up` into the ledger, mirroring `RemoteAgent._op_mouse_down`/`_op_mouse_up` exactly. Verified: `test_left_mouse_down_registers_a_hold_and_up_releases_it`, `test_release_all_now_actually_releases_a_locally_held_mouse_button`. |
| 2 | `HeldInputLedger()` was instantiated fresh inside every `_build_coexistence_guard()` call — one per `mount()`, not one per channel — so a parent session and a delegated child sharing one overlay (`_announcement_decisions`) got two disconnected ledgers. | **Closed.** `_get_channel_ledger(channel_key)` (`__init__.py`) — a get-or-create registry keyed exactly like `_announcement_decisions`, shared by every `ComputerTool` mount() driving the same channel. Verified: `test_channel_ledger_is_shared_across_every_mount_for_the_same_channel`. |
| 3 | The proposed reaper called `channel.lower()` **inside** `with channel.lock:` — a hung `Stop-Process` would wedge the reaper inside the lock forever, and the design's own loud-failure detector (`thread.is_alive()`) would never fire because the thread never gets back to it. | **Resolved by removing the reaper**, not by hardening it (§0.3) — but `WindowsOverlay.hide()`'s `Stop-Process` failure handling was *also* hardened independently of that decision: a timeout is now logged at **ERROR** (was `DEBUG`) and `_shown`/`_pid` are left as-is rather than unconditionally reset to "torn down" — failing toward the safe direction instead of silently claiming a teardown that could not be confirmed. Verified: `test_hide_does_not_silently_mark_the_band_hidden_when_stop_process_hangs`. |

### 0.3 Resolving the ambiguity between "ship at `T=∞`" and "Alt A" — **Alt A shipped**

The panel's language admitted two readings: ship the counter with a **trailing window fixed at `T=∞`** (band never auto-lowers — closes the F8 cross-tool hazard below, but the reported 6h37m band is *unchanged*, since nothing ever brings it down), or ship **Alt A** (§11.1 below: raise before every `execute()`, lower in the `finally`, no thread, no constant — closes F8 *and* the reported defect, because the band actually comes down once idle).

**Alt A is what shipped.** `T=∞` would have left the user's actual complaint — a band up for 6h37m after one screenshot — completely unfixed while still claiming to be "the fix." Alt A satisfies the invariant (§4) exactly, contains no reaper thread and no tunable, and is *simpler* than the `T=∞` reading, not more complex: there is no timer to reason about, no `REAPER_TICK`, nothing to set to infinity. The mechanism:

```
ComputerTool._band_enter()   — called once per execute() call, BEFORE any action runs:
    with channel_state.lock:
        channel_state.depth += 1
        if not handle.shown:
            handle.show()          # re-raise; §7.6 policy on failure (_handle_channel_failure, reused verbatim)

ComputerTool._band_exit(token) — called once per execute() call, in a `finally`, ALWAYS:
    with channel_state.lock:
        channel_state.depth -= 1
        depth_now = channel_state.depth
    if depth_now != 0:
        return
    if ledger.held_tokens:          # §4.2's second invariant clause (F10)
        return
    with channel_state.lock:
        if channel_state.depth != 0:   # re-checked: a new action may have started
            return
        handle.hide()
```

This is the §5.1 pseudocode below with the reaper deleted and the periodic tick replaced by "checked exactly once, synchronously, at the one moment it can possibly matter" — the same correctness properties (raise inside the same critical section as the increment; depth is a counter, not a timestamp; re-checked immediately before the one blocking call) survive intact, because they never depended on the reaper *existing*, only on the *ordering* the reaper's pseudocode also followed.

**What this costs, stated plainly (per §7's own "no silent degradation" discipline and the task's own instruction to say so):** `hide()` is now on the action path, not off it on a background thread. A hung `Stop-Process` (bounded at `STOP_PROCESS_TIMEOUT_SECONDS = 15s`, `overlay_windows.py`) now blocks the *agent's own next action* on this channel for up to ~15–20s instead of silently wedging an invisible reaper. This is **louder, not free** — the task's own framing — and it is the correct trade: a slow, visible failure the agent's own turn surfaces beats a fast, invisible one nobody notices until the next incident report.

### 0.4 What is explicitly deferred, and its gate

The reaper thread, the per-channel lock held across a periodic tick, and the trailing window `T` (§5.2, §5.4 Windows row, §9's "recommended design" column, §11 "Recommended (trailing window + reaper)" column) are **not shipped**. They are preserved below **unedited**, as a historical record of the rejected apparatus and the reasoning against it — not as a queued follow-on. See **§11.3 (new)** for the exact, mechanical gate that would have to pass before reopening this: a measured Windows raise-cost distribution proving Alt A's per-episode re-raise cost is genuinely unaffordable, not a guess.

### 0.5 §7.2 wording — corrected for revision 2

§3 below still contains revision 1's proposed §7.2 replacement text, which described the trailing-window mechanism. That text was never applied to `coexistence.md` (revision 1 was a proposal). The wording that ships alongside this revision, replacing it, is:

> ### 7.2 Per driving episode, not per session and not per action
>
> The announcement is raised before the first action of a driving episode and lowered the instant that episode's last in-flight `execute()` call returns — checked immediately, synchronously, not on a timer. An episode is delimited by activity, not by process lifetime: see `docs/designs/band-lifetime.md`. A batch of N actions in one `execute()` call (an OpenAI-style batch, §4.2) is one episode — one raise, one lower — never once per constituent action.
>
> The announcement still lives on the target, beside the injector; only the raise/lower *commands* cross the network for a remote target, and today only the *raise* is implemented there (`RemoteAgent._op_announce_raise`) — the remote overlay's lifetime remains tied to the target agent's own process/stdin-EOF path (`coexistence.md` §9.1), unchanged by this revision. Extending Alt A's raise/lower symmetry to the remote transport is deferred (§0.4 is local-only; the remote case would need a new wire op, out of this change's scope).

---

## 1. Problem framing

### 1.1 What was reported

An external user captured two live disclosure bands on one Windows desktop, both owned by **live, idle** sessions — so this is not the orphan leak that `da7972b` closed.

| Windows PID | Age at capture | Owning session |
|---|---|---|
| 38544 | **6 h 37 m** | alive, idle, `pts/25` |
| 44580 | 10 m | alive, idle, `pts/29` |

One band had been on screen for six and a half hours because a session took a single screenshot that morning. Two sessions produced two stacked bands.

### 1.2 What the code does, and why that is not a bug in the ordinary sense

`ComputerTool._ensure_announced` (`tool-computer-use/__init__.py:980`) guards on `self._announced`. It fires once, on this session's first real action, and builds a channel handle stored at `self._announcement` (`:380`) whose own comment states the intent plainly:

> `# an overlay object to keep alive for the tool's lifetime`

There is no lowering path. `LinuxOverlay.hide()` and `WindowsOverlay.hide()` exist and are correct, but the only callers are `atexit` (`:2345`), the X server's own teardown on connection death (U6), and `RemoteAgent._teardown_overlay` on stdin EOF. All three are **process-death** paths.

So the band's lifetime is `[first real action, process exit]`. That is exactly what the code says. The defect is that **"the tool's lifetime" is the wrong scope**, and nothing in the system bounds it.

### 1.3 Why this is a safety defect, not a cosmetic one

The reporter's argument is correct and this design adopts it:

1. **A permanently-present indicator carries no information.** The band's job is to change the human's belief. A signal that is always on cannot change anyone's belief about anything. It becomes furniture, and users learn to ignore furniture — which is the precise opposite of what you want from a stop button.
2. **It is not dismissible.** `WS_EX_TOOLWINDOW` + `ShowInTaskbar=false` means invisible to Alt-Tab and the taskbar. There is no supported route to remove it short of ending the session or hunting the PID through WMI.
3. **It stacks.** N processes that ever touched the tool produce N bands.

Point 1 is the load-bearing one. §7.1's rule — *"an announcement that failed to render is a stop button the human cannot reach"* — has a corollary this design is built on: **an announcement the human has learned to ignore is also a stop button the human cannot reach.** One fails at the pixel layer; the other fails at the attention layer. §7.1 is not weakened by this document; it is extended to cover the second failure mode.

### 1.4 This is the same argument `5b9ebbf` already accepted, applied to the other end

`5b9ebbf` moved the *start* of the band's life from mount-time to first real action, on exactly this reasoning: bind the indicator to the activity it discloses. That commit fixed the leading edge. This document fixes the trailing edge. **It composes with `5b9ebbf` and does not undo it** — the first-use gate, the sticky refusal, and the channel-decision cache all survive intact (§8.1).

---

## 2. Explicit assumptions

Every one of these is a place this design can be wrong.

1. **A session that has taken no desktop action for `T` seconds is not driving.** The whole design rests on this. It is true by construction for injection (nothing is in flight) but it is a *predictive* claim about the near future, and the future is a model's decision this process cannot see.
2. **The raise is fast enough to sit on the critical path.** Verified for Linux (0.28 ms show / 0.31 ms hide, U6, `coexistence.md` §10.1). **Not verified for Windows** — the raise is a `powershell.exe` launch plus a `ready` handshake polled at 100 ms granularity with a 30 s ceiling (`overlay_windows.py:275`, `:402`). §7.2 in `coexistence.md` was written before the Windows overlay existed and assumed the Linux cost.
3. **A human perceives a band that appears and disappears as more informative than one that is always there.** This is an O11-class human-factors claim. It is the premise of the entire design and **it has not been tested.** See §12, Q4.
4. **Two disclosure bands on one desktop are worse than one.** True for the reported case (both idle). Arguably *false* for two genuinely concurrent driving sessions, where two bands is accurate information.
5. **`time.monotonic()` is an adequate clock.** On Linux it excludes suspend time, so a laptop that sleeps mid-session lowers the band late (§10, F7). Judged acceptable; not free.
6. **`hide()`/`show()` on both overlay classes are already idempotent and correct.** Both check a `_shown` flag and return early. Verified by reading; not by test at raise/lower frequency.

---

## 3. Reconciliation with `coexistence.md` §7.2 — deliberate, not accidental

§7.2 is titled *"Per session, not per action"* and says:

> The announcement is raised when a driving session begins and lowered at session end. It is **not** shown and hidden per action. Per-action flicker is worse UX, and per-action state changes are only free because of a structural fact worth stating:
>
> > **The announcement lives on the target, beside the injector. It never crosses the network.**

**This document contradicts that section's conclusion and adopts its reasoning.** Three things have to be said precisely, so the contradiction is a decision rather than an accident.

**(a) §7.2's *safety* content is preserved without change.** §7.2 makes no safety claim. Its two stated justifications are UX (flicker) and cost (raise/lower is free). Both are engineering claims about the mechanism, and §5 addresses each on its own terms. Nothing in §7.1 (the disclosure guarantee), §7.3 (per-platform models), §7.4 (mapped ≠ visible), §7.5 (geometric exclusion), or §7.6 (refuse to drive with no channel) depends on §7.2's conclusion.

**(b) §7.2's phrase "at session end" assumed a bounded session.** The section reads as if a "driving session" is a coherent episode with an end. In the implementation, "session end" is *process exit*, and a `pts/25` shell can sit open for six hours. §7.2 did not choose that; it inherited it. The reported defect is the gap between the two readings.

**(c) The structural claim is now only half true, and the half that changed matters.** "The announcement lives on the target, beside the injector" is still exactly right — `_handle_remote_overlay_announce` (`__init__.py:2465`) asks the *target* to raise its own overlay, and the pixels never cross the wire. But the **raise and lower commands do** cross the wire, as a control op (`RemoteBackend.announce_raise` → `RemoteAgent._op_announce_raise`). Under a per-session lifetime that cost was paid once and was invisible. Under a per-episode lifetime it is paid per episode, and §5.4 sizes it honestly rather than inheriting §7.2's "free."

**Proposed replacement wording for §7.2**, to be applied in the same change that lands this design:

> ### 7.2 Per driving episode, not per session and not per action
>
> The announcement is raised before the first elementary event of a driving episode and lowered once the episode ends. An episode is delimited by activity, not by process lifetime — see `docs/designs/band-lifetime.md`. It is not raised and lowered per action: a trailing window absorbs the gaps inside a burst, so a sequence of actions a human would perceive as one continuous episode of the machine being driven produces exactly one raise and one lower.
>
> The announcement still lives on the target, beside the injector; only the raise/lower *commands* cross the network, at one control-op round trip each, and only on episode boundaries — never on the per-event injection path.

---

## 4. The crux — a precise, testable definition of "driving"

Everything else in this document follows from one sentence.

> ### The invariant
>
> **The band must be up at every instant at which the agent is applying force to the target machine: an operation is in flight (so an elementary event could be injected at any instant), or the agent is currently holding an input down.**

This is stated as a *necessary* condition on the band being up. It is deliberately not stated as *sufficient* — the band may be up at other times, and §5.2's trailing window is exactly that permission, used for anti-flicker and cost. Splitting necessity from sufficiency is what makes the design testable: **the invariant is a safety property with a hard test; the trailing window is a tuning parameter with a metric.**

### 4.1 Why this definition and not the obvious alternatives

| Candidate definition | Verdict |
|---|---|
| A backend syscall is in flight | **Too narrow.** Drops disclosure *between* the elementary events of a single `type_text`, which is where the human most needs it. |
| A tool call (`execute()`) is in flight | **Correct but incomplete.** Misses held inputs: `left_mouse_down` returns while a button is still down, and the ledger is holding it. |
| An operation is in flight **or** an input is held | **This.** Covers every instant at which the agent's action can reach the machine. |
| The session is armed | **Today's defect.** Unbounded, and unbounded is uninformative. |

### 4.2 What falls out with no special cases

The value of stating one invariant rather than a table of rules is that the hard cases stop being cases:

- **Halted.** `CoexistenceGuard._halted` is a one-way latch with no clear method inside a session (`coexistence_guard.py:277`, `test_halt_invariant.py`). A halted guard raises `HaltedError` before every elementary event, so **no event can ever be injected on it again**. The invariant does not require the band. It comes down after the trailing window, exactly like idle. *This directly answers "a halted session is armed but not driving — what should the band say?": nothing, because it is not there. The halt is already durable on disk (`record_halt`, `__init__.py:1510`) and already reported to the model on every turn (`hook-computer-use-halt-notice`). A permanent on-screen notice about a past halt is furniture with a different label.*
- **Paused.** `PauseController.check()` raises `PausedError` before every elementary event and `release_all` has already fired, so the ledger is empty. No injection is possible, no input is held. **The invariant does not require the band, and the trailing window covers the moment right after the human clicks Pause.** See §6.3 for what this costs and why it is still the right call.
- **Cancelled.** `record_halt` is written and the guard halts on its next check. Same as halted. `coexistence.md` §8.5 already specifies "Overlay: vanishes" for cancel — this design makes that true by mechanism instead of by process death.
- **A 30 s `wait` action, a `hold_key(duration=60)`, a 200-character paced `type_text`.** All are one `execute()`; the operation is in flight for the whole duration; the band is up for the whole duration. No timeout can lower it, because the in-flight test is a counter, not a timestamp (§5.1).
- **An OpenAI batch of N actions.** One `execute()` (`__init__.py:1418`, the loop is inside). One raise, one lower.
- **`desktop` tool actions.** `DesktopTool.execute` shares the same `ComputerTool` instance (`__init__.py:1660`). Same counter, same band.
- **Reads.** `screenshot`, `zoom`, `list_windows`, `get_clipboard` are all in scope, exactly as `5b9ebbf` decided for the leading edge. A screenshot is a capture of a human's screen; the band is up for it. There is one gate, not one for writes and a silent hole for reads.

---

## 5. Recommended design (revision 1 — DEFERRED, not shipped; see §0)

> **This entire section describes the reaper-thread/trailing-window apparatus that the adversarial review FAILed and that revision 2 (§0) did NOT ship.** It is kept below verbatim as the historical record of what was proposed and why it was rejected, and as the specification for what §11.3's mechanical gate would unlock if it is ever measured and passed. **What actually shipped is Alt A, §0.3/§11.1, with the reaper deleted.**

### 5.1 Mechanism — a band state machine owned by the channel

Four pieces of state, one lock, one daemon thread. They live on a **channel-state object keyed by `_channel_identity(backend)`** (`__init__.py:2188`) — *not* on `ComputerTool`. §10, F8 explains why this placement is load-bearing rather than tidy.

| Field | Meaning |
|---|---|
| `depth: int` | Number of `execute()` calls currently in flight against this channel, across every `ComputerTool` sharing it |
| `last_activity: float` | `time.monotonic()` at the moment `depth` last reached 0 |
| `raised: bool` | Whether the channel currently believes the band is up |
| `lock: threading.Lock` | Serialises every transition below |

```
execute() / DesktopTool.execute():
    with channel.lock:
        channel.depth += 1
        channel.ensure_raised()          # blocks until verified up, or raises
    try:
        ... run the action(s) ...
    finally:
        with channel.lock:
            channel.depth -= 1
            if channel.depth == 0:
                channel.last_activity = monotonic()

reaper thread, every REAPER_TICK (250 ms):
    with channel.lock:
        if not channel.raised:                       continue
        if channel.depth > 0:                        continue
        if ledger_has_held_inputs():                 continue
        if monotonic() - channel.last_activity <= T: continue
        channel.lower()
```

Three properties make this correct rather than merely plausible:

1. **`ensure_raised()` is inside the same critical section as the `depth` increment.** The reaper cannot observe `depth == 0` between the increment and the raise, so it cannot lower a band that an action is about to need. There is no window.
2. **`depth` is a counter, not a timestamp.** No elapsed-time test can fire during an in-flight action, regardless of how long that action runs.
3. **The reaper only ever lowers.** It has no path to raise. A wedged reaper leaves the band *up*, which is the safe direction (§10, F6).

**Lock ordering.** `ComputerTool._announce_lock` (per-instance, first-use consent) may be held while acquiring `channel.lock`; never the reverse. The reaper acquires only `channel.lock`. The ordering is acyclic by construction and there is exactly one place that takes both.

### 5.2 The trailing window `T` — what it is and is not

`T` is **not** a safety parameter. The invariant holds at `T = 0`. `T` exists for two reasons:

- **Anti-strobe.** Two actions 800 ms apart should not produce a lower and a raise between them.
- **Cost amortisation.** Where the raise is expensive, `T` is what makes the design affordable at all.

`T` must exceed the inter-action gap *within a burst*, where a burst is a run of actions a human would perceive as one continuous episode. Note the distinction that makes this tractable: a 45-second gap between two clicks is **not flicker** — the desktop was genuinely un-driven for 45 seconds and the band correctly reflected that. Only short gaps need absorbing.

**Recommended starting values, and the basis for each:**

| Channel | `T` | Basis |
|---|---|---|
| Linux X11 | **5 s** | Raise is 0.28 ms (U6). `T = 0` is viable; 5 s is anti-strobe insurance costing nothing. |
| Windows | **90 s** | Raise is a `powershell.exe` launch + `ready` handshake. Sized to make a re-raise rare, not to make the claim tight. See the honest cost below. |
| Remote (Linux/Windows target) | target platform's `T` | Add no hysteresis for RTT: 1–8 ms on a tailnet is three orders of magnitude below either value. |
| macOS | **N/A** | §6.5 — this design does not apply. |

**The Windows number is the ugly part and it must be said plainly.** At `T = 90 s`, the Windows band's claim degrades from *"the agent is driving this machine"* to *"the agent drove this machine within the last 90 seconds."* That is a materially weaker claim than Linux's. It is also a **265× improvement** on the reported 6 h 37 m, achieved with no new wire protocol. It is a deliberate, temporary price, and §11 names the work that removes it.

I explicitly considered deriving `T` from `presence.LATCH_DECAY_SECONDS = 60.0` — the codebase already contains a measured "recent activity stays meaningful for 60 s" constant. **I rejected that.** That constant decays *human* presence and was measured for a different phenomenon; borrowing it would be numerology dressed as evidence. `T` must be measured for what it actually governs (§11, and §12 Q1).

### 5.3 Raise semantics — what changes and what does not

`_ensure_announced` splits into two operations with deliberately different powers:

**First episode — the consent gate, unchanged.** `_ensure_announced()` keeps everything `5b9ebbf` and the dedup fix built: the full §7.3/§7.6 evaluation, the macOS dialog, the sticky per-instance `_announce_refused`, and the process-wide `_announcement_decisions` cache. **A human who declined is never re-asked. A refused session is still structurally unable to drive.**

**Subsequent episodes — a narrower re-raise.** `channel.ensure_raised()` only re-shows a channel that was *already consented to*. It cannot ask a human anything and it cannot clear the sticky refusal. If it fails, it applies §7.6's policy to *this action only*: human detected → refuse the action loudly; no human detected → `logger.error` and proceed without a channel, exactly as `_handle_channel_failure` does today (`__init__.py:1968`).

**A re-raise failure is deliberately NOT sticky, and here is the argument.** The stickiness of `_announce_refused` exists to prevent re-asking a human who said no — its own comment says so: *"re-asking after a refusal is worse than not asking at all (it trains people to click through)."* A re-raise asks nobody. The only channel that asks is macOS, and macOS is excluded from re-raising entirely by construction (§6.5). So the property stickiness protects is preserved without stickiness, and making a transient `powershell.exe` launch hiccup permanently kill a session would be strictly worse. **This is the one place where I am trading a structural guarantee for a behavioural one, and it should get adversarial attention.**

### 5.4 Cost, measured where it is known and declared where it is not

| Channel | Raise | Lower | Frequency under this design |
|---|---|---|---|
| Linux X11 | 0.28 ms (U6) | 0.31 ms (U6) | once per episode |
| Windows | `powershell.exe` launch + orphan sweep + `ready` poll @100 ms, 30 s ceiling. **Unmeasured p50.** | `Stop-Process -Force` across the interop boundary. **Unmeasured.** | once per episode, `T = 90 s` |
| Remote | one control-op RTT (1–8 ms tailnet) + the target-side raise above | same | once per episode |

The Windows p50 is unmeasured and I will not invent it. What is known: the prior leak report observed detached children failing to reach `ready` on a ~9 s cadence, which is evidence that this launch path is not merely slow but occasionally *fails*. Under a per-session lifetime that risk was taken once. **Under a per-episode lifetime it is taken once per episode, and that is a real new failure surface** (§10, F2/F3). Measuring the Windows raise distribution is a gate on shipping `T = 90 s` as a defensible number rather than a guess (§11).

---

## 6. The hard cases, answered

### 6.1 Bursts

Absorbed by `T`. Two actions less than `T` apart produce zero band transitions. This is a *metric*, not an assertion — §11 measures flicker rate directly and `T` is tuned against it.

### 6.2 Long-running single actions

Covered by the counter, not by a timeout. See §4.2. The failure mode this prevents — a reaper lowering the band 60 s into a 90-second `hold_key` — is structurally impossible, not merely unlikely.

### 6.3 Halt and pause

Both fall out of the invariant with no special case (§4.2). Neither pins the band beyond the trailing window.

**What this costs, stated rather than buried.** A human who clicks Pause and then walks away sees the band for `T` and then sees nothing. Two consequences:

- **They lose visual confirmation that their pause is still in effect.** Real. Mitigated by `T` covering the moment of the click, and by the fact that a paused session cannot inject.
- **They lose the ability to pre-emptively pause an idle armed session.** Also real, and it is a genuine capability regression versus today. Counter-argument: pausing an idle session prevents nothing, because the moment the agent acts the band is back and their own hands halt it unconditionally (§6.0). But the capability does go away.

**The alternative I rejected: pin the band while paused.** It reintroduces the exact defect through a different door — human pauses, walks away, band is up for six hours. A rule that says "the band is up when driving, *and also* when paused indefinitely" is not one rule, and the indefinite branch is the one that produced this document.

**Note on `coexistence.md` §8.5.** It says the overlay "stays up (so the human can resume)." That was written against an intended Resume button. `LinuxOverlay._button_rects` and `WindowsOverlay._button_rects` ship **Pause and Cancel only**; resume is a CLI (`resolve_resume_command()`, `resume_cli.py`). So the band is not today the resume path, and lowering it removes no resume affordance that exists. If a Resume button is ever built, this decision must be revisited — it would become the human's only route back and pinning would be justified.

### 6.4 Remote transports

The decision is made on the controller; the band renders on the target; the raise and lower cross the wire as control ops.

- **Ordering is safe by construction.** The raise and the injection travel the same serialised NDJSON channel, so a raise that has been ACKed precedes any injection that follows it. If the link dies after the raise, the action dies on the same link and nothing is injected. If the link dies before the raise ACK, `ensure_raised()` fails and the action is refused.
- **A lost *lower* leaves the band up on the target** — the safe direction, but it is exactly the six-hour band again if nothing retries. **The lower is therefore idempotent and re-attempted by the reaper on every tick until the target confirms it is down.** The target-side `_teardown_overlay` on stdin EOF remains the backstop.
- **Latency does not need hysteresis.** 1–8 ms against a `T` of 5–90 s is noise. What *does* need care is that `announce_raise` sits on the critical path of the first action of every episode, adding one RTT to that action only.

### 6.5 macOS — this design does not apply, and that is structural

The macOS channel is a one-shot modal `osascript` dialog. `coexistence.md` §7.3 and probe O2 settle that an agent-drawn persistent overlay is impossible there — a settled negative, not a deferral.

**macOS keeps exactly what it has: one dialog at session start, never repeated.** Re-showing it per episode would steal focus per burst and would re-ask a human who already answered — the precise behaviour `_announcement_decisions` exists to prevent. There is nothing to lower, so there is no lifetime to scope.

This sharpens an asymmetry that §7.3 already declared, and the sharpening should be said out loud:

- **Linux / Windows:** the band is a **live indicator** — up ⟺ the agent may be driving right now.
- **macOS:** the dialog is a **consent event** — you were told once, at the start. Its live control is §6.0, the human's own hands.

**macOS therefore gets no benefit from this design and retains the weakest disclosure of the three platforms.** That is a known gap, not an omission, and it is unchanged by this document. It is also a mild argument that BACKLOG's "live countdown in the macOS dialog" is polishing the wrong axis — what macOS lacks is a persistent ambient channel, which O2 closed.

---

## 7. What "no silent degradation" means here

The repo's hard rule applies at every new decision point this design introduces.

| Situation | Behaviour |
|---|---|
| Re-raise fails, human detected | `AnnouncementRefused` → the action returns an error result. Nothing is injected. |
| Re-raise fails, no human detected | `logger.error` (not `debug`, not swallowed) + proceed, identical to `_handle_channel_failure` today. |
| Lower fails | `logger.error`, `raised` stays `True`, retried next tick. Band stays up — the safe direction. |
| Reaper thread not alive | Checked on every `execute()` (`thread.is_alive()`, one call). Not alive → `logger.error` naming the consequence: *the band will not come down.* |
| Channel reports `shown: false` on re-raise | Treated as a raise failure, above. Never as success. |

There is no path in this design where a band fails to appear and an action proceeds silently.

---

## 8. What is explicitly *not* changed

1. **The first-use consent gate.** `_ensure_announced`'s §7.3/§7.6 evaluation, the macOS dialog, the sticky refusal, and the `_announcement_decisions` channel cache are untouched.
2. **The coexistence guard.** `coexistence_guard.py`, `presence.py`, `halt_state.py`: read-only dependencies. The design reads `guard.pause.is_paused` and `guard.halted` nowhere in its decision path — it does not need to, because §4.2 shows both cases are already covered by `depth` and the ledger.
3. **`overlay_windows.ps1` and the stdin-pipe lifetime binding from `da7972b`.** Unchanged in the recommended design. The band still dies with its owning process on every exit path including `SIGKILL`.
4. **§7.4's occlusion verification, §7.5's geometric exclusion, §7.6's refusal policy.** All preserved. §7.5's exclusion rects are registered on raise and unregistered on lower — `LinuxOverlay.hide()` already does exactly this (`overlay_linux.py:175`), so the agent cannot click through the space a lowered band used to occupy.

---

## 9. The three suggested directions, evaluated

### 9.1 Scope to the action — **adopted**, as Alt A (§0.3/§11.1), not as §5's reaper apparatus (§5 deferred, see its own header note).

### 9.2 Make it dismissible — **rejected**, and rejected as an addition too

A third affordance that hides the band without disarming the tool.

**Why it loses.** It converts a safety indicator into an opt-out. The first thing anyone does with a band they find intrusive is dismiss it — and then they have a fully-armed agent with *no* indicator, which is strictly worse than today's defect, because now the band's **absence** also carries no information. Today the band at least means "armed." After a dismissal it means nothing in either state.

It also treats the symptom (annoyance) rather than the cause (the band is up when nothing is happening). Once §5 lands there is nothing left to dismiss: a band that is up is a band that means something *right now*, and the correct response to a band you do not want is to stop the agent, which Cancel already does.

**The steelman I owe it:** during a genuinely long driving session the band is up continuously and correctly, and a user may still find a full-width band intrusive. That is real — but the answer is reducing the band's *footprint*, not its *truthfulness*, and footprint is an open human-factors question already (O11, §7.5's noticeability/exclusion tension). Dismissal is the wrong lever for that problem.

### 9.3 De-duplicate across sessions — **deferred, with a stated trigger**

**What this design already fixes:** the reported stacking. Both bands belonged to idle sessions; under §5 both come down. Within a single process, `_announcement_decisions` already de-dups parent and delegated-child sessions against the same channel, and §5.1's channel-scoped state extends that to the band's lifetime.

**What is deferred:** cross-*process* de-dup. Two separate CLI processes driving the same desktop concurrently still produce two bands.

**Why deferring is a design position, not a punt.** De-dup and lifetime-binding are in direct tension. `da7972b` bought orphan-freedom by tying the band's life to exactly one process's stdin pipe. A band shared across processes has no single owner, so it needs a durable registry with liveness detection and a hand-off protocol for when the owner dies — which is the orphan problem, re-created, in exchange for solving a cosmetic one. Furthermore, two bands during genuinely concurrent driving is arguably *correct information*: two agents have your desktop.

**Trigger to reopen:** field evidence of ≥2 bands stacked during genuine concurrent driving (not idle sessions), after §5 has shipped.

---

## 10. Failure modes

Ordered by severity. The disclosure-drops-mid-action family is F1–F3 and F8.

> **Revision 2 note:** F1, F6, F7 below describe the deferred reaper (§5, not shipped) and no longer apply to the shipped Alt A mechanism (§0.3) — kept for the historical record. F8 and F10 are now shipped and covered by real, passing tests (noted inline). F9 shipped unchanged.

**F1 — the reaper lowers the band during an in-flight action.** *The serious one, for the DEFERRED reaper (§5).* Not applicable to shipped Alt A: there is no reaper, and the same "depth incremented inside the same critical section as the raise" property is what `_band_enter`/`_band_exit` (§0.3) preserve without a background thread to reason about at all.

**F2 — a raise fails and the action proceeds anyway.** Prevented: `ensure_raised()` raises before dispatch. **Test:** stub the channel to fail on re-raise with a human detected; assert `AnnouncementRefused` and **zero** backend calls.

**F3 — the raise reports success but the band is not visible.** §7.4's "mapped is not visible." **This design does not fix it, and makes its cadence question worse:** §7.4 prescribes verification on raise plus a ~30 s timer, and explicitly refuses per-action verification because it would put a screen capture on the input hot path. With many raises instead of one, "verify on every raise" reintroduces exactly that cost. Proposed but unresolved: verify on raise only when the last verification is older than 30 s, amortising the cost across episodes. **Named as open — §12, Q2.**

**F4 — remote link dies between raise and action.** Safe by construction (§6.4): raise and injection share the transport.

**F5 — a remote lower is lost.** Band stays up (safe direction), reaper retries every tick until confirmed. Backstop: target-side `_teardown_overlay` on stdin EOF.

**F6 — the reaper thread dies.** *Not applicable to shipped Alt A — there is no thread to die.* The failure this guarded against (a hung `lower()` making the band-down decision silently stop happening) is instead addressed directly: `_band_exit` runs synchronously on the action path, so a hung `hide()` (bounded, `overlay_windows.STOP_PROCESS_TIMEOUT_SECONDS`) is loud by construction — it blocks the very next action and is logged at ERROR (`test_hide_does_not_silently_mark_the_band_hidden_when_stop_process_hangs`), not silently absorbed by a background thread nobody is watching.

**F7 — clock and suspend.** `time.monotonic()` on Linux excludes suspend time. A laptop that sleeps 8 hours resumes with a small elapsed value and holds the band for up to `T` after resume. Slow, not wrong. Accepted; noted.

**F8 — two `ComputerTool` instances sharing one channel.** `_announcement_decisions` deliberately hands the *same* overlay handle to a parent session and a delegated child (`__init__.py`, `_build_announcement`). **If the band's lifetime state lived on `ComputerTool`, tool A finishing its own action could lower a band that tool B is actively driving under — a genuine mid-action disclosure drop.** **SHIPPED, resolved:** `_ChannelBandState`/`_get_channel_band_state` (`__init__.py`) place `depth` on a channel-keyed object, the union across every tool sharing that channel — no reaper needed to make this true; `_band_exit`'s re-check under `state.lock` is what makes it correct. **Test (real, passing):** `tests/test_band_lifetime_alt_a.py::test_f8_one_tools_idle_depth_cannot_lower_a_band_the_other_is_driving_under` — two tools, one channel; tool A finishes and idles while tool B is still in-flight; asserts the band stays up until tool B *also* finishes. Also verified on real Windows hardware (alienware-r13, `_BAND_COLOR` pixel sample) — see the task's evidence log.

**F9 — `desktop` actions bypass the counter.** Prevented: `DesktopTool.execute` participates in the same `_band_enter`/`_band_exit` pair `ComputerTool.execute` does, sharing the one `ComputerTool` instance's channel — not just the consent gate. Shipped.

**F10 — held inputs after `execute()` returns.** `left_mouse_down` returns with a button held. `depth` is 0 but the agent is applying force. **SHIPPED, resolved together with finding #1 (§0.2):** covered by the invariant's second clause — `_band_exit` checks `ledger.held_tokens` (channel-scoped, §0.2 finding #2) before lowering, and `left_mouse_down` now actually registers the hold (§0.2 finding #1; previously it did not, for a local session). The ledger's deadman (`DEFAULT_DEADMAN_SECONDS = 5.0`, `ledger.py`) is generic to `HeldInputLedger` regardless of caller, so it now applies to a locally-held button exactly as it always did to a remote one. **Test (real, passing):** `test_band_stays_up_while_a_mouse_button_is_held_even_at_depth_zero`, `test_left_mouse_down_registers_a_hold_and_up_releases_it`, `test_release_all_now_actually_releases_a_locally_held_mouse_button`.

---

## 11. Tradeoffs

| Dimension | **Recommended** (trailing window + reaper) | **Alt A** (raise/lower per `execute()`, no window) | **Status quo** (session lifetime) |
|---|---|---|---|
| **Latency** | One raise per episode. Linux: 0.28 ms, noise. Windows: a launch on the first action of an episode, ~once per 90 s. | Linux fine. **Windows: a process launch on *every* action** — a 10–50× regression on a tool whose action round trip is otherwise tens of ms. | Zero after the first action. |
| **Complexity** | +1 daemon thread, +1 lock, +3 fields, +1 tunable per channel. Contained in one object. | **Lowest.** No thread, no timer, no constant, no tuning. | Lowest — but the simplicity is the defect. |
| **Reliability** | New failure surface: one raise per episode, each a chance to hit the Windows launch failure. Mitigated by a large Windows `T`. | **Worst.** One launch per action multiplies the documented ~9 s failure loop by action count. | Best on this axis — one launch, ever. |
| **Cost** | Bounded process churn. | Unbounded process churn on Windows. | None. |
| **Security / safety** | **Best.** Band presence carries information again; §7.1 holds at both the pixel and attention layers. | Equal on the invariant; worse in practice because strobing trains the same ignore-response. | **Worst.** Indicator decays to furniture; stop button unreachable at the attention layer. |
| **Scalability** | Scales with episodes. Channel-scoped state handles N tools per desktop. | Scales with *actions* — the wrong variable. | Scales with processes: N sessions → N permanent bands. |
| **Reversibility** | **High.** `T → ∞` restores today's behaviour exactly; `T → 0` becomes Alt A. The whole design is one tunable away from either neighbour. | High. | n/a |
| **Org fit** | Matches `5b9ebbf`'s own argument, applied to the other end. | Matches it more purely. | Contradicted by its own team's fix. |
| **Optimises for** | Truthful disclosure at acceptable cost on the slow platform. | Minimum concepts. | Minimum moving parts. |
| **Sacrifices** | A thread and a per-channel constant; a weaker Windows claim until §11's follow-on lands. | Windows viability. | The indicator's meaning. |

**The dominant tradeoff is complexity versus Windows raise cost, and it is decided entirely by one unmeasured number.** If the Windows raise turns out to be cheap and reliable, Alt A wins outright and this design is over-built.

### 11.1 The simplest credible alternative — Alt A, and why it loses

> **Raise before every `execute()`, lower in the `finally`. No trailing window, no reaper thread, no `T`.**

This is genuinely simpler and it satisfies the invariant *exactly* — the band is up precisely when force is being applied, with no approximation and no tunable. It removes a thread, a lock, a constant, and a whole class of timing failure modes (F1, F6, F7 all disappear). **On a Linux-only build it is the correct design and this document would be over-engineering.**

It loses on one measured fact and one observed one:

1. `WindowsOverlay.hide()` kills the overlay process and `show()` relaunches it (`overlay_windows.py`, module docstring). Alt A therefore means **one `powershell.exe` launch, orphan sweep, and `ready` handshake per desktop action.**
2. The prior leak report observed that launch path failing repeatedly on live hardware (detached children never reaching `ready`, ~9 s cadence). Multiplying that path by action count converts an occasional failure into a frequent one.

**Alt A remains the recommended fallback** if the reaper proves problematic, and it becomes the *preferred* design the moment the Windows show/hide-without-relaunch work lands (§11.2). The recommendation is Alt A plus one parameter, and that parameter exists only to pay for a Windows implementation detail.

*Note:* "keep everything, but lower the band after N minutes of inactivity" is not a third alternative — it is this design at a large `T`. That the recommendation degrades gracefully into it is the reversibility property in the table above.

### 11.2 The follow-on that removes the Windows price

`overlay_windows.py` already holds a live stdin pipe to the overlay process for its entire life — that pipe is the lifetime binding `da7972b` introduced, and the PowerShell side already blocks a thread on it waiting for EOF.

**Teaching that side to read commands rather than only detect EOF turns raise/lower into `ShowWindow(SW_SHOW)` / `SW_HIDE` — sub-millisecond, no launch, no failure surface.** Windows then behaves like Linux, `T` drops to 5 s, and Alt A becomes viable everywhere.

Deliberately **not** in this design: it changes `overlay_windows.ps1` and its command protocol, it is unprobed, and the lifetime fix (§5) delivers a 265× improvement without it. Sequencing it separately keeps the risky change out of the safety fix. Residual cost of doing it: the overlay *process* then lives for the tool's lifetime even while the band is hidden — much less bad than a visible band, but not nothing, and worth a second look before adopting.

### 11.3 The mechanical gate for reopening the deferred reaper/`T` work (new, revision 2)

The adversarial review's instruction, applied literally: the reaper/trailing-window apparatus (§5) "waits behind a mechanically enforced Windows measurement — a test or CI check, not a prose promise in §12 — and only gets built if the number shows the simpler per-action approach is genuinely unaffordable."

**The gate, stated as a testable condition:**

1. Measure `WindowsOverlay.show()`'s real p50/p95/failure-rate on live hardware, under Alt A's actual usage pattern (one re-raise per episode after an idle gap, not a synthetic microbenchmark) — the same measurement §12 step 1 already called for, now load-bearing rather than a "nice to have."
2. **Only if** p95 exceeds a threshold that makes Alt A's per-episode re-raise cost a real, user-visible latency regression (not merely nonzero — Alt A already accepts a nonzero Windows re-raise cost; §0.3 says so) does building the reaper become a candidate again.
3. The measurement must be captured as a **test or CI check** — a number the codebase can point to — not restated as a belief in a future revision of this document.

**Until that measurement exists and fails this threshold, Alt A is not "the interim solution" — it is the shipped, correct design**, per §11.1's own words: "On a Linux-only build it is the correct design and this document would be over-engineering" — and nothing in revision 2's real-hardware verification (§0.3) found the Windows re-raise cost to be a problem in practice for the tested scenario (single-episode re-raise, ~sub-second, see the task's evidence log).

---

## 12. Migration and rollout

> **Revision 2: steps below are the ORIGINAL (deferred) reaper rollout plan, kept verbatim.** What actually shipped, in one step: **land the channel-scoped state, the channel-scoped ledger, the local ledger wiring, and Alt A raise/lower (no `T`, no reaper) — all four pieces together, in one change**, because all four are the correctness core the adversarial review exempted (§0.2/§0.3), and none of them is safely separable from the others (the depth counter needs the ledger to know about held inputs before lowering; the local ledger wiring is what makes "held inputs" mean anything locally at all). Verified: `tests/test_band_lifetime_alt_a.py` (9 tests, all passing) plus real-hardware pixel-sample verification on `alienware-r13` (Windows/WSL2) — band-up, band-down-on-idle, band-stays-up-during-F8, band-down-once-both-tools-idle, all confirmed via an independent PowerShell `_BAND_COLOR` sample, not just the Python object's own `shown` flag.
>
> **§11.3 (new) states the mechanical gate for reopening the deferred reaper/`T` work.** The steps below do not apply until that gate is measured and passed.

1. **Measure the Windows raise distribution first.** p50/p95/failure-rate for `WindowsOverlay.show()` on real hardware. This is a gate: `T = 90 s` is a guess until this number exists, and the whole recommendation-vs-Alt-A decision turns on it.
2. **Land the channel-scoped state and the counter with `T = ∞`.** Behaviour identical to today, F8's cross-tool hazard closed, all invariant tests in place and passing. Zero user-visible change.
3. **Land the reaper with `T` finite, Linux first.** Linux's raise is proven free, so it carries the least risk and produces the metrics fastest.
4. **Enable on Windows with the measured `T`.**
5. **Tune `T` against the flicker and duty-cycle metrics (§13).**
6. **Revisit for Alt A** once §11.2 lands.

**Rollback:** set `T = ∞`. Exact restoration of current behaviour with one value, at any step.

---

## 13. Success metrics

| # | Metric | Target | What it catches |
|---|---|---|---|
| 1 | Elementary events injected while `raised == False` | **0, enforced as a test** | The invariant. Non-negotiable. Assert `channel.raised` at entry and exit of every `_run`. |
| 2 | Band-up duty cycle (`band_up_seconds / session_wallclock`) | Tracks actual driving. ~0.005 for a one-screenshot-in-6 h session; today ~1.0 | The reported defect, directly. |
| 3 | Max band-up duration with zero intervening actions | ≈ `T` | A wedged reaper (F6). Materially exceeding `T` means the thread is stuck. |
| 4 | Flicker rate: raises occurring within 2 s of a preceding lower | ≈ 0 | `T` too small. |
| 5 | Raises per session, and raise latency p50/p95 per channel | Frequent **and** slow together = `T` too small | The cost side of the tuning. |
| 6 | Re-raise failure rate, split transient vs. refusal | ≈ 0 | The new failure surface (§5.3). Non-zero means the non-sticky decision needs revisiting. |
| 7 | `execute()` calls that found the reaper dead | **0** | F6, loudly. |
| 8 | Human-factors: do users report the band as informative rather than ignorable? | Qualitative | Assumption 3 — the premise of the whole design. |

Metrics 1 and 7 are pass/fail gates. The rest are tuning signals.

---

## 14. Open questions I could not resolve

**Q1 — What is `T`, really?** I can defend the *shape* of the answer (must exceed the intra-burst inter-action gap; must be sized by raise cost) but not the numbers. Needs: the distribution of inter-action gaps within real driving episodes, per workload, plus the Windows raise measurement (§12.1). Both values in §5.2 are starting points, not findings.

**Q2 — §7.4 occlusion verification cadence under repeated raises.** Verify every raise and a screen capture lands on the action hot path, which §7.4 explicitly refuses. Verify none and a mid-session raise can be occluded and unnoticed for up to 30 s. The amortised proposal in F3 is untested and I have no principled basis for choosing between them without measuring capture cost against real raise frequency.

**Q3 — Does the Windows stdin command channel work?** §11.2 depends on `overlay_windows.ps1` reading commands on the pipe it already holds. Unprobed. It materially changes which design is correct, so it should be probed early even though the work is deferred.

**Q4 — Is the design's premise true?** The entire argument rests on "a band that appears and disappears is more informative than one always present." That is an O11-class human-factors claim and **nobody has tested it.** I believe it, the reporter believes it, and the reasoning is sound — but it is possible that a band coming and going is experienced as noise rather than signal. This is the single assumption most worth attacking.

**Q5 — Is losing pre-emptive pause on an idle session acceptable?** A product call, not a technical one (§6.3). If not, the natural answer is an out-of-band disarm — a `--halt` counterpart to the existing resume CLI, writing the same durable record `record_halt` already writes — which restores the capability without a permanent band. That would be a new CLI consumer of an existing public function, not a change to `halt_state.py`, but it is adjacent enough to the out-of-scope fence that it needs an explicit decision rather than my assumption.

**Q6 — Local held-input deadman. RESOLVED in revision 2 (§0.2, finding #1).** `HeldInputLedger.hold()` is now called from the local `left_mouse_down` path too (`ComputerTool._hold_mouse_button`), and `HeldInputLedger`'s deadman (`DEFAULT_DEADMAN_SECONDS = 5.0`, `ledger.py`) is generic — it was never remote-specific, only remote-*wired*. A local `left_mouse_down` with no matching `mouse_up` now releases after 5s exactly like a remote one, and the band correctly stays up for that same 5s window (§4.2's second invariant clause) rather than pinning indefinitely.

**Q7 — Should the band's content change when pinned for a non-driving reason?** Deliberately left out of v1 (§8). Under the recommendation there is no such reason left — halt and pause both release the band — so the question is moot *unless* Q5's answer or a future Resume button reintroduces pinning. Noted so the omission is on the record.
