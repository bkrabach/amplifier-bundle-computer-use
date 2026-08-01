# Backlog

Work that is known, wanted, and deliberately **not** being done yet. Ordered by
theme, not priority — except the first section, which blocks everything else.

Nothing here is a vague aspiration. If an item can't be stated concretely enough
to build, it doesn't belong on this list.

---

## Core experience — must land before anything below

- **Per-monitor targeting through the remote path.** Per-monitor selection works
  locally, but a remote capture currently returns the whole virtual-desktop
  bounding box. On a real four-monitor 4K desktop that is a 7.52x downscale with
  ~20% of the frame being dead space no display sits behind. Unusable for real
  work. `ComputerTool` owns monitor selection; `RemoteBackend` bypasses it.

- **Run it as a real Amplifier session, end to end.** Component-level proof is
  not product-level proof. The hook, native tool promotion, the screenshot
  marker → image-block rewrite, and the write gate have never all executed
  together in one session.

- **Windows capture + input, verified.** Probe, monitor enumeration, geometry,
  and cursor position are verified against live hardware. Capture and input are
  not.

- **macOS click and type into a real application.** Blocked on a macOS
  Automation TCC prompt that cannot be approved over SSH. The underlying
  `CGEventPost` primitive is verified; app-targeted input is not.

---

## Human/agent coexistence

Both items below come from the same observation: this tool drives a desktop a
human may also be sitting at, and today neither party can see the other.

### Visible on-desktop indicator while the agent is driving

When the tool is operating on a machine, that machine's desktop should show a
clear, always-on-top indicator so anyone physically present knows the agent is
active — and can stand back rather than fighting it for the pointer, keyboard,
clipboard, and window focus.

The indicator must also be a **control**, not just a light:

- **Pause** — agent input suspended, human takes the desktop, agent is told
  plainly that it is paused rather than left to wonder why its clicks stopped
  landing.
- **Cancel** — session ends, held inputs released via the existing ledger.

Design notes / open questions:
- Must render on all three platforms; each has a different always-on-top and
  overlay story.
- Must not itself steal focus — an indicator that grabs focus recreates the
  problem it exists to solve.
- Must survive the agent crashing. An indicator that lingers after the agent is
  gone is bad; one that disappears while the agent is still driving is worse.
- Pause state belongs to the *target*, not the controller: a compromised or
  buggy controller must not be able to ignore a local human's pause.

### Human-presence signal to the agent, with a handoff protocol

The agent should know whether a human is actively using the desktop before it
starts driving, and be given a deliberate way to acquire control:

- **Take over immediately** — for machines the human has explicitly ceded.
- **Request access** — prompt on the target, with an auto-take-over after a
  configurable timeout if nobody answers.
- **Wait for inactivity** — take over only after an idle threshold is crossed.

Design notes / open questions:
- Idle detection differs per platform (`XScreenSaver`/idle time on X11,
  `CGEventSourceSecondsSinceLastEventType` on macOS, `GetLastInputInfo` on
  Windows) and none is a perfect proxy for "a human is present."
- Interacts directly with the contention policy in
  `docs/designs/remote-transport.md` §11 — resolve them together, not separately.
- Auto-take-over on timeout is a safety-relevant default. It should be opt-in
  per target, not global, and it must be audited.

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

Findings from the adversarial review of `docs/designs/remote-transport.md` not
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
