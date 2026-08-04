# Multi-provider computer use — spike → design → implement

Status: **plan only.** Nothing below is built. Phase 0 exists to make Phase 1
worth writing; do not skip to Phase 2 because the shape "seems obvious."

## Why this plan is shaped this way

Today, three things all passed and one thing failed:

- 393 unit tests: green
- a 4-stage in-process chain test (orchestrator → provider → beta → cache): green
- a live Anthropic API call: **rejected every request**

```
invalid_request_error
tools.0.computer_20251124.parameters: Extra inputs are not permitted
```

The chain test verified that `type='computer_20251124'` *survived*. Nothing
checked what **else** rode along. `ToolSpec.parameters` is a required dict, so
every native ToolSpec carried a key the native schema forbids.

That failure is the entire justification for Phase 0. Multi-provider work has
six documented unknowns (below) that no amount of reading resolves — the docs
are ambiguous or absent on exactly the fields that decide the design. **A design
written before those are answered would be fiction.**

## What is already known

Research (2026-08-02, primary sources) found genuine native computer-use tools at
exactly three providers. Azure OpenAI and AWS Bedrock are *transports* for
OpenAI's and Anthropic's tools, not independent designs. Mistral and xAI
verifiably have none — their built-in tool catalogs are enumerable and contain
no GUI tool.

| | Anthropic | OpenAI | Gemini |
|---|---|---|---|
| Tool type | `computer_20251124` | `computer` (GA) | `computer_use` |
| API | Messages | Responses | Interactions |
| Coordinates | px, in a space you **declare** | px, in the image's space, **undeclared** | **normalized 0–999** |
| Actions per call | 1 | **N, batched** | 1 |
| Results per call | 1 | **1 per batch, not per action** | 1 |
| Screenshot return | `tool_result` + image block | `computer_call_output` | `function_result` + text + image |
| Safety | none on the wire | `pending`/`acknowledged` checklist | per-action tri-state incl. `blocked` |
| Server state | none (stateless) | `previous_response_id` | `previous_interaction_id` |

**There is no lossless common schema across any two of the three.** The closest
pair is Anthropic ↔ OpenAI, and even there Anthropic *requires* a field
(`display_width_px`) that OpenAI rejects the concept of.

Three conflicts decide the architecture:

1. **Coordinate spaces are three, not two.** Gemini's normalization destroys
   aspect-ratio information in the coordinate itself; on a 3840px monitor one
   unit ≈ 3.8px. A scale-factor model — which is what `geometry.py` is — cannot
   represent it. Gemini needs a normalize/denormalize pair, not a scale factor.
2. **Cardinality.** OpenAI batches N actions under one `call_id` and expects
   exactly **one** screenshot for the whole batch. An abstraction modelling
   "action → result" is structurally wrong for OpenAI. The internal model needs
   `ActionBatch`.
3. **OpenAI and Gemini are not the Messages API.** The hook's remaining half
   rewrites string tool-results into image blocks. There is no analogue for
   `computer_call_output` (Responses items) or `function_result` (Interactions
   steps) — those are different item types in different request envelopes.
   **The kernel gap gets worse under multi-provider, not better.**

## The six unknowns Phase 0 must answer

Docs are ambiguous or silent on each. Guessing any of them wrong is a rewrite.

1. Does OpenAI GA `computer` accept **any** config fields (`display_width`,
   `environment`)? Every GA example is a bare `{"type":"computer"}`. No schema
   page found. Strongly indicated "no config" — unconfirmed.
2. OpenAI action object schema. Field names (`x`, `y`, `button`, `keys`,
   `scroll_x/y`, `path`) are inferred from code samples, not a published schema.
   Unknown: does `wait` take a duration? does `scroll` require `x`/`y`?
3. Does OpenAI GA still emit `pending_safety_checks`? The field exists and Azure
   samples branch on it; the OpenAI GA guide never mentions it. Possibly
   preview-era vestigial.
4. Anthropic's concrete field names for `scroll` and modifier-keys-with-click.
   Docs sections are JS-rendered and did not survive fetch.
5. Gemini coordinate range: `0–999` per the action tables, "1000x1000" per the
   prose, divide-by-1000 in the sample code.
6. Gemini `scroll.magnitude_in_pixels` — named pixels, bounded like a normalized
   value. Genuinely ambiguous.

Also open, lower stakes: whether `computer_20241022` is still accepted; whether
Amazon Nova Act exposes a wire-level tool type or is SDK-only; Qwen / GLM /
Moonshot / Doubao (unverified, third-party claims only); Meta Llama (no tools
reference located — a gap, not a verified negative).

---

## Phase 0 — Spike. Throwaway code, real APIs, real desktop.

**Goal:** answer all six unknowns with captured wire traffic, and prove each
provider can drive a real desktop end to end. **Not** to produce reusable code.

**Explicitly not in scope:** touching the bundle, designing an abstraction,
writing a provider module. Anything that survives Phase 0 as *code* is a
mistake; what survives is the **captured traffic** and the answers.

One standalone script per provider. Each drives the existing `RemoteBackend`
against a real target and completes one task that requires seeing the screen —
open an app, read something off it, act on what it says. Not a screenshot in
isolation: a screenshot the model actually has to *use*.

Capture, per provider:
- the exact request JSON for the tool declaration (verbatim, not paraphrased)
- the exact response item shape for one action, and for a **batch** (OpenAI)
- the exact result envelope the API accepts for a screenshot
- coordinate space, proven by clicking a **known target** and confirming the hit
- what happens when a required field is omitted — the error text is the schema
  documentation the docs don't provide

Targets: `windows-host` (Windows/WSL2) and `macos-host` (macOS).
Windows first — empty desk, no collision.

**Done when:** all six unknowns have an answer backed by captured traffic, and
each of the three providers has completed one real screen-dependent task. A
provider that *cannot* be made to work is an equally good outcome — record why
and move on.

**Gate:** no Phase 1 until every unknown is either answered or explicitly
recorded as "could not determine, here is what blocks it."

---

## Phase 1 — Design, from captured traffic only

Written **after** Phase 0, citing captured traffic rather than documentation.
Any claim traceable only to a doc page is suspect — the docs were ambiguous on
all six unknowns, which is why Phase 0 exists.

Must decide, with evidence:

- **Internal action model.** Starting hypothesis: absolute px in the real
  captured screenshot's space, with `ActionBatch` as the unit (required for
  OpenAI correctness). Confirm or kill against the captures.
- **Where each adapter's transform lives.** One adapter per provider owning:
  coordinate transform, verb-name mapping (button-in-name vs button-as-field),
  key token join/split, result envelope, safety protocol, tool declaration.
- **Capability flags, not a union type.** `requires_display_dims`,
  `batches_actions`, `safety_protocol ∈ {none, checklist, per_action}`,
  `has_browser_verbs`, … A union type would force every provider to carry every
  other provider's concepts.
- **What gets dropped, named explicitly.** Multipoint `drag.path` → two-point.
  Diagonal `scroll_x/y` → direction+magnitude is **lossy**. Gemini's
  `navigate`/`open_app` have no desktop-backend equivalent at all. Each drop is
  a decision to record, not a bug to hide.
- **The `D1` probe extension.** `select_backend` currently asks "can I reach a
  display." It will need to reason about **action-set coverage**: an X11 backend
  serves Anthropic's full vocabulary but only a subset of Gemini's browser verbs.
- **Where the screenshot-return rewrite lives** for non-Messages APIs. This is
  the hardest open question and the most likely to force a kernel conversation.

**Gate:** design names every dropped field and every lossy transform. A design
that claims lossless conversion has not read the captures.

---

## Phase 2 — Implement

Order matters. Each step ends at something provably working.

1. **Refactor to the internal action model, Anthropic only.** No new provider.
   Prove the existing live session still drives the real desktop. This isolates
   "did the refactor break anything" from "does the new provider work."
2. **Second provider end to end.** Pick from Phase 0 evidence — whichever proved
   *least* painful, not whichever seems most important. Prove a real session on
   real hardware before the third.
3. **Third provider.**
4. **Kernel conversation, if still needed.** `ToolResult.content` needs two
   independent consumers; `browser-tester` was checked and is **not** one (it
   ships zero Python modules — it shells out to a CLI). A second consumer may
   emerge from multi-provider work itself.

Each step's gate is a **live session on real hardware**, not a passing test
suite. Today proved a green suite and a green chain test can both sit on top of
a request the API rejects outright.

---

## Parked, with reasons

- **Nova Act, Qwen, GLM, Moonshot, Doubao** — unverified. Revisit if a Phase 0
  provider proves cheap to add; not worth research time before then.
- **Mistral, xAI** — verified to have nothing. Recheck only on a release note.
- **`computer_20241022`** — absent from current docs. Absence from docs is not
  removal from the API; verify empirically only if something still emits it.
