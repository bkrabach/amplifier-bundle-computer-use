# Backlog

Work that is known, wanted, and deliberately **not** being done yet. Ordered by
theme, not priority — except the first section, which blocks everything else.

Nothing here is a vague aspiration. If an item can't be stated concretely enough
to build, it doesn't belong on this list.

---

## Core experience — must land before anything below

- ~~**Per-monitor targeting through the remote path.**~~ **DONE - and this
  entry was already stale.** Re-checked against the real four-monitor 4K
  Windows desktop this feature was built for (`user@windows-host`, live
  hardware, not a mock): the full remote round trip -
  `ComputerTool` (monitor selection) -> `RemoteBackend.capture_scaled` ->
  the real NDJSON wire -> `RemoteAgent._op_capture_scaled` ->
  `WindowsBackend.capture(region=...)` - already crops at the far end and
  reports the selected monitor's own dimensions, not the virtual-desktop
  bounding box (`ScreenGeometry(width=9626, height=4323, origin_x=0,
  origin_y=-2163)`).
  - Default (unconfigured) path scopes to the primary monitor: `current_monitor
    = DISPLAY3`, screenshot returns `1280x720` (never `9626x4323`).
  - An explicit non-primary, NEGATIVE-origin monitor (`DISPLAY1`, `x=1946,
    y=-2160`) returns `1280x720` and a two-independent-capture-methods
    pixel diff (a full-desktop capture cropped locally vs. a direct
    region-scoped `CopyFromScreen`) came back byte-identical (mean abs diff
    `0.00`) - the region reaching the agent is exactly right, not an
    approximation.
  - A LIVE mid-session switch (`desktop.select_monitor`, the actual
    model-facing action) from `DISPLAY3` to a different negative-origin
    monitor propagates to the very next `computer` screenshot: new origin,
    new dimensions, no stale state.
  - The coordinate path was checked, not assumed: `Display.to_screen()` on
    the targeted (negative-origin) monitor mapped into that monitor's own
    real bounds, matching `test_geometry.py`'s existing negative-origin unit
    coverage.
  - The one real gap found was a **test coverage** gap, not a code defect:
    `tests/test_remote_monitor_scoping_e2e.py`'s fixture was entirely
    non-negative and could not have caught a sign error in the region math
    anywhere along that round trip. Closed with
    `test_negative_origin_monitor_survives_the_full_remote_round_trip` and
    `test_live_monitor_switch_between_negative_origin_monitors_over_the_remote_round_trip`,
    using the exact real-hardware layout as the fixture.

- **Run it as a real Amplifier session, end to end.** Component-level proof is
  not product-level proof. The hook, native tool promotion, the screenshot
  marker → image-block rewrite, and the write gate have never all executed
  together in one session.

- ~~**Windows capture + input, verified.**~~ **DONE.** Capture proven through a
  real Amplifier session (1280x720 per-monitor, model described the actual
  desktop). Input proven end to end: Win key -> `notepad` -> Enter ->
  `CU-INPUT-PROOF-7741` typed, read back three ways (document text, tab title,
  and a status bar reading `Ln 1, Col 20 / 19 characters` matching the string
  length exactly), then closed without saving.

- ~~**macOS click and type into a real application.**~~ **DONE, and the TCC
  premise was wrong.** This was recorded as blocked on an Automation TCC prompt
  that "cannot be approved over SSH". Checked directly: System Events Automation
  is *already granted* for the SSH chain (`rc=0`, and a re-check returning in
  0.1s with no re-prompt proves the grant persisted). `focus_window` is not
  blocked. The earlier apparent failure was a human dismissing the Spotlight
  window mid-test, misread as a permissions problem - the same misdiagnosis this
  bundle's own scenarios exist to catch.

---

## Indicator polish

- **Live countdown in the macOS announce dialog.** The dialog currently states
  its timeout in plain text ("dismisses itself in 30 seconds") because §7.3
  requires the timeout be disclosed rather than run as a hidden clock. A *live
  countdown* would be strictly better: it turns a static claim into visible,
  verifiable state, so someone who looks up mid-way knows how long they actually
  have rather than having to remember when it appeared.

  Requested by the owner after the noticeability test, where the static
  disclosure was judged sufficient to notice and act on but the countdown was
  named as the obvious improvement.

  Implementation note: `osascript`'s `display dialog ... giving up after N` is
  a single blocking call and cannot repaint its own text, so this needs a
  different mechanism than the current one-shot — likely a loop of short-lived
  dialogs, or a different presentation layer entirely. Cost is real; the
  disclosure requirement is already satisfied without it. Not urgent.

---

## Human/agent coexistence

Both items below come from the same observation: this tool drives a desktop a
human may also be sitting at. Both are now **largely built and verified**.md` for the design and evidence base
(`coexistence-probes.md`: U1b, U1c, U3, U4, U5, U6, U7, O1, O2, O5).

### Human-presence signal to the agent, with a handoff protocol — built and proven

The presence detector (`presence.py`, `coexistence_guard.py`) reconciles the
target's own idle-time counter against the agent's own injection timestamps,
per elementary event (not per operation), and halts before the next write the
moment a human is detected — unconditionally, with no configuration key able
to disable it (`CoexistenceGuard`'s halt invariant, `test_halt_invariant.py`).

Evidence:
- **Linux X11** — `GUARD_MS["linux-x11"] = 5.0`ms, proven by the ship gate
  (`scripts/verify_coexistence.py`): 100 trials, 91% detection, zero false
  positives, measured masked fraction 9.00% vs. 8.33% predicted
  (`GUARD/cadence`).
- **macOS** — `GUARD_MS["macos"] = 10.0`ms, measured on real hardware (a live
  MacBook, macOS 26.6 arm64) from a 300-sample distribution of
  inject-to-visible-in-idle latency: p50 0.58ms, max 8.56ms, 0/300 false
  positives at the 10ms band.
- **Verified against a real human at the keyboard** — a paced `type_text` run
  halted correctly at chunk 34 of 40 when a human touched the trackpad:
  `HaltedError` raised, margin +29.84ms, `release_all` fired exactly once.
- **Windows** — `GUARD_MS["windows-wsl2"] = 20.0`ms, `GUARD_MEASURED = True`.
  Measured on a real Windows 11 desktop (`windows-host`, over its live
  WSL2 interop boundary) across three independent 300-sample runs (900
  samples total) of the reconciliation margin `PresenceMonitor` actually
  computes: all three runs independently topped out at exactly 16.000ms
  (the documented `GetTickCount` tick ceiling), 0/900 false positives at
  20ms and above. Intra-`type_text` detection remains not viable on
  Windows regardless (masked fraction `20/60 = 33%` at production
  cadence) — see `presence.py`'s `GUARD_MS` comment for the full sweep.

What shipped alongside detection: the halt invariant (the design notes §6.0, unconditional — no config key disables it); target
binding (abort on focus change mid-operation, §8.6); pause/cancel with
held-input release via the existing ledger; and, as of this pass, `type_text`
pacing (`type_pacing.py`) — a measured full-speed `type_text` run (202
characters in 0.07s) produced an inter-character gap 28x narrower than
`GUARD_MS["macos"]`, masking the detector for the whole operation; pacing now
keeps the gap wider than the guard band whenever a coexistence guard is
active.

**Still open:**
- Windows `GUARD` is unmeasured (`GUARD_MEASURED["windows-wsl2"] = False`) and
  on hold — no probe has run against a real Windows target; the `32.0`ms
  figure in `GUARD_MS` is a sound inference from documented `GetLastInputInfo`
  quantisation, not evidence, and must not be presented as proven.
- Per-action `request`/auto-takeover consent protocol was deliberately cut
  from v1 (the design notes §13, D1) — it never traced back to
  the incident that motivated this feature; re-open only on a demonstrated
  need.

### Visible on-desktop indicator while the agent is driving — built and proven (Linux, macOS)

Evidence:
- **Linux** — the override-redirect overlay (`overlay_linux.py`) renders (198
  sampled pixels changed on-screen), does not steal focus (identical focus
  window ID before and after `show()`), `hide()` restores with zero residual,
  and registers 2 exclusion rects at the injection call site so the agent
  cannot click its own Pause/Cancel controls (`exclusion.py`,
  `coexistence_guard.py`).
- **macOS** — `announce_macos.py`'s `osascript display dialog` session-start
  announcement: `announce()` returns after 15.3s against a stated 15s
  timeout with `gave_up=True` correctly distinguished from an actual button
  press.

**Still open:**
- The Windows overlay is not built (folded into transport Phase 4 per the
  design doc — one persistent PowerShell process serving injection,
  presence sampling, and the overlay together).

---

## Provider support

- **Multi-provider native computer-use.** Today this is Anthropic-only, gated by
  a string sniff on the provider's module name. OpenAI (`computer_use_preview`,
  Responses API) and Gemini (`computer_use`) both ship equivalents with
  structurally incompatible wire shapes — there is no lossless common schema, so
  the shared mechanism is "carry an opaque provider-addressed payload" with each
  provider module translating.

- **Upstream PRs to `amplifier-module-provider-anthropic`** — each independently
  landable, none requiring a kernel change:
  1. `_apply_tool_cache_control` stamps `tools[-1]` blindly; it should stamp the
     last *function* tool. The existing `web_search` path already does this
     correctly and is the precedent.
  2. `model_dump()` without `exclude_none=True` emits `visibility: null`, which
     the API rejects — the reason this bundle hand-writes plain dicts.
  3. `_build_request_beta_headers` should derive required betas from the native
     tool types present in the request. **This one structurally retires this
     bundle's need to touch `provider._beta_headers` at all.**

- **Orchestrator tool-spec passthrough.** `loop-streaming` rebuilds every
  `ToolSpec` from three fields, discarding the native shape — which is the sole
  reason this bundle monkey-patches `provider.complete`. Fixing it upstream
  deletes the patch.

- **`ToolResult` structured content.** A kernel-level way for a tool result to
  carry ordered content blocks would retire the screenshot marker protocol
  entirely, and likely the whole hook module. Needs a second consumer before it
  is worth proposing.

---

## Performance

- **Persistent PowerShell bridge.** Every Windows action spawns a fresh
  `powershell.exe`, paying CLR startup and `Add-Type` compilation per click. The
  same persistent-NDJSON pattern already used for the remote agent applies one
  layer down. This is the single largest latency win available on Windows, and
  remote SSH stacks on top of it rather than fixing it.

---

## Security hardening

Findings from the adversarial review of the design notes not
yet closed:

- Name prompt-injection-via-screen-content as a first-class threat in the design
  document. The model reads an untrusted screen; nothing today distinguishes
  operator instructions from text that merely appears in a screenshot.
- Narrow the "SSH adds no new authority" claim. It holds for network surface. It
  does not hold for authority: synthetic input defeats human-presence-gated
  consent UI (UAC, OAuth prompts), and reaches already-unlocked GUI session state
  a shell cannot cheaply reach.
- `chmod 0600` on screenshot files and per-session scoping of the shot
  directory — currently a flat shared directory relying on inherited umask.
- Windows-side screenshot temp-file TOCTOU window.
- Clipboard reads flow verbatim to the provider API and into durable logs with
  no gating outside `read_only`. Needs a stated policy.
- Audit-log coverage is specified only for `type_text`; `set_clipboard`, `key`,
  `hold_key`, and captures are unspecified.
- Held-input ledger has no release path on `SIGKILL`/OOM of the agent process.
- Build the `command=`/`restrict` SSH key restriction the design's comparison
  table already claims as a security property.
- Audit log is written by the same principal it audits — no tamper resistance.

---

## Repository / ecosystem

- Microsoft OSS readiness: `SECURITY.md`, `CODE_OF_CONDUCT.md`,
  `CONTRIBUTING.md`, `SUPPORT.md`, `.github/` (CI, CODEOWNERS, issue and PR
  templates, dependabot), root lint/type configuration, `CHANGELOG.md`.
- CI that runs the test suite on Linux without any desktop present.
- Agent-facing guidance on **when not to use this tool**: if it is a browser page
  you control, Playwright is deterministic, headless, parallel, free of focus
  contention, and far cheaper. Computer-use is for native apps, OS dialogs, and
  black-box GUIs. The tell: if you are about to read pixels to find a button
  that has a DOM node, the wrong tool is in hand.

## ~~macOS `type_text` silently no-ops~~ — RETRACTED 2026-08-03

**This was the locked-screen defect, not a separate bug.** Re-tested on the same
host with the same code once the screen was unlocked:

```
key('cmd+space')     changed=True    <- Spotlight opened
type_text('zzqq-probe')              <- returned without raising
after type_text      changed=True    <- LANDED
```

`type_text` works. It was reported as a distinct defect because `key` appeared to
work in the same run while `type_text` did not — an asymmetry that looked like
strong evidence of two different code paths. It was noise.

**This is the THIRD wrong diagnosis a locked screen produced in one session:**
first "Accessibility TCC not granted", then "the type path posts to a specific
app rather than the system-wide event tap", then this. Every one was confident,
evidence-shaped, and wrong.

That track record is the argument for the lock guard shipped in `7d98701`: a
human reasoning from symptoms gets this wrong every single time, so the machine
has to answer it. The guard now names the state before anyone starts theorizing.

## Local / open-weight model support (researched 2026-08-03, not started)

**Why it's interesting:** every local serving stack (vLLM, Ollama, llama.cpp,
LM Studio) exposes OpenAI-compatible chat-completions with `"type": "function"`
only. No native computer-use tool type exists anywhere in that world —
confirmed by reading vLLM's own Anthropic-Messages `AnthropicTool` model, which
has `name / description / input_schema` and **no `type` field at all**, so
`{"type": "computer_20250124", ...}` is structurally unrepresentable rather than
merely unimplemented.

That is the seam's `BREAK 1` (declaration must go in `tools[]`) — but NOT
`BREAK 2`. Which matters, because of one exception:

**Holo3.1** (H Company, Apache 2.0, 2026-06) is post-trained for **standard
OpenAI function calling**: pass `tools=[...]` with `tool_choice="required"`,
read `message.tool_calls`. The action comes back as a **parsed tool call**, not
text to regex. From H's own agent-loop doc: *"Native function calling: the model
returns OpenAI-style tool_calls. Holo3.1 only; Holo3 does not support it."*

So it is a genuine third integration category — not a server-side computer-use
tool type (you author the schema), but structurally compatible with the half of
our seam that `BREAK 2` covers.

### Two separate bets, do not conflate them

| | Sizes | Hardware | Status |
|---|---|---|---|
| **A. General user** | Holo3.1 **4B / 9B** | consumer GPU / Apple Silicon | **quality at these sizes UNVERIFIED by us** |
| **B. This box** | Holo3.1 **35B-A3B NVFP4** | DGX Spark specifically | H publishes a Spark launch line; 6.8s → 3.3s/step |

(B) was the first thing found and is the more impressive demo. (A) is the one
that matters for anyone else, and its central unknown is whether 4B is good
enough to drive a desktop at all — H's blog gives AndroidWorld 4B/9B 58%→72%,
but the per-size OSWorld table on the model card renders as an image and was not
readable. **Verify before building.**

### The coordinate trap, recorded now so it is not rediscovered

Holo and GUI-Owl both say "0–1000" and **mean different things**:

- **Holo3.1** — normalized to *the image you sent*. `abs_x = int(x/1000 * width)`.
  No smart_resize.
- **GUI-Owl-1.5** — normalized to the *smart_resize'd* dims. Error bounded ~±15px:
  *"small, systematic, and exactly the kind of thing that reads as 'the model is
  a bit imprecise.'"*

Same family as the 261px miss. Verify against a known target empirically before
trusting either doc.

### EVALUATED 2026-08-03 — Holo3.1 4B and 9B are NOT viable

Ran both sizes against real screenshots from two desktops, N=3 per target.
Ground truth = where Anthropic and OpenAI independently agree (both already
proven driving these exact desktops); disagreements >60px discarded rather than
hand-labelled.

|  | 4B | 9B |
|---|---|---|
| tool_call emission | 19/21 (90%) | 21/21 (100%) |
| median error | 29px | 24px |
| within 25px | 2/7 | 4/7 |
| latency | 2.9s | 4.2s |
| **worst single sample** | **1096px** | **312px** |

**The integration is fine — the model is not.** H's coordinate formula
(`x/1000 * width`, no smart_resize) is CONFIRMED correct; good samples land
within 0–5px, impossible if the space were wrong.

**The disqualifier is variance, not median.** Same image, same prompt, three
samples: `start_button` 9B = 129px / 9px / 5px. `clock` = 237px / 1px / 0px.
Median is the wrong statistic for a click agent — a 312px miss on a 1280px image
clicks something else, and on a real desktop that is an action, not a retry.
Going 4B→9B cut worst-case 1096→312px but did not remove it.

Second mode: window-relative targets (`active_titlebar`, `close_button`) are
wrong on ALL THREE samples at 9B, and 9B is *worse* than 4B on both. That is
comprehension ("which window is frontmost"), not grounding, and it got worse
with scale.

**Bar to revisit:** worst-case inside ~25px across repeats. Not median. If
35B-A3B clears that, the dialect work is small and now understood.

**Two serving traps found:** the Spark-tuned vLLM already on the box fails with
`Unrecognized keys in rope_parameters: {mrope_section, mrope_interleaved}` (too
old for Qwen3.5-VL), and H's own published Spark line uses
`--gpu-memory-utilization 0.8`, which fails on unified memory where only ~half
of 121GB reads as free. 0.22 worked.

Full data: `holo-eval/RESULTS.md` (workspace, not this repo).

### Ruled out

- **GUI-Owl-1.5** (MIT, best open OSWorld with released weights, 56.5 @32B) —
  pure harness, both breaks apply. Its published PC driver also `NameError`s as
  shipped (`dashscope` used but never imported).
- **UI-TARS** — open weights frozen at 2025-04-18. UI-TARS-2 is paper + hosted
  API only. Also has two contradictory coordinate conventions in one repo.
- **Qwen-UI-Agent** — paper 2026-07-30, no weights.
- **OS-Atlas / ShowUI / CogAgent** — dormant, grounding-only, no agent loop.
- **Molmo/MolmoAct** — active but robotics VLA, wrong domain.

### Also relevant

`HoloDesktop CLI` (`github.com/hcompai/holo-desktop-cli`) already drives real
desktops against a local model server and supports `--base-url`. Worth reading
as prior art — and note its Linux constraint matches ours: *"Requires an X11
session; Wayland is not supported by the input backend."*
