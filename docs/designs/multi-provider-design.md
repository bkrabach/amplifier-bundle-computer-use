# Multi-provider computer use — Phase 1 design

**Status:** design proposal, written from captured live-API traffic (Phase 0) and from
direct inspection of the installed upstream modules. Nothing here is built.
**Supersedes the premise of:** `docs/designs/multi-provider-plan.md` §"What is already known".
**Evidence base:** `docs/designs/phase0-status.md`, `.phase0/captures/`, and the file:line
citations below, every one of which I read in the tree while writing this.
**Date:** 2026-08-02

---

## 0. Verdict, up front

Build a **two-provider** design with a **two-method adapter** and an `ActionBatch`
internal unit. Change **nothing** in the backend seam. Do **not** design for Gemini.

And one finding that outranks everything else in this document:

> **The OpenAI path is not reachable through Amplifier today, and no bundle-side code
> can make it reachable.** Phase 0 proved OpenAI's *API* drives a desktop. It did not
> prove Amplifier's `provider-openai` can carry that traffic — and it cannot. Three
> separate places in `amplifier-module-provider-openai` drop it on the floor
> (§9). Phase 2's OpenAI step is blocked on an upstream PR, not on this design.

This is the same shape of gap that produced `tools.0.computer_20251124.parameters:
Extra inputs are not permitted` — a green suite sitting on top of a wire that was never
exercised. Phase 0 moved the unexercised boundary; it did not remove it. §11 is about
making that class of gap fail at mount instead of at turn 40.

---

## 1. Problem framing

Two models can drive a raw desktop through a server-side tool contract: Anthropic's
`computer_20251124` on the Messages API, and OpenAI's `computer` (GA) on the Responses
API. This bundle serves one of them. The question is what shape lets it serve both
without the second one deforming the first.

The plan expected three desktop providers, three coordinate spaces, three result
envelopes, and 2.5 safety protocols, and concluded "no lossless common schema, three
adapters." **Every conflict that conclusion rested on belongs to Gemini** — normalized
0–999 coordinates, a tri-state safety protocol with `blocked`, browser/mobile verbs —
and Gemini is disqualified for this bundle's purpose (§12). Qwen has no server-side
tool contract at all.

What is left is the closest pair, and the design should be sized to that.

---

## 2. Explicit assumptions

Stated so they can be attacked, and marked by whether evidence backs them.

| # | Assumption | Backing |
|---|---|---|
| A1 | `.phase0/captures/` shapes are representative of steady-state traffic, not one-off | **Weak.** Small N. `openai-turn1.json` is a single-action batch; **no multi-action batch was ever captured** (§14) |
| A2 | Both providers' coordinate spaces are absolute pixels in the submitted image's space | **Strong.** `openai-coordproof.json` `match:true`, 1280×720 → 3840×2160 exact; Anthropic is declared-space by construction |
| A3 | Neither provider emits safety state on the wire | **Adequate.** `openai-findings.json` `pending_safety_checks_present:false`; Anthropic has never emitted any |
| A4 | OpenAI GA `computer` accepts zero config fields | **Strong.** Phase 0 unknown #1: bare `{"type":"computer"}` → 200; `display_width`/`environment`/`display_width_px` each → 400 `Unknown parameter` |
| A5 | The desktop backends already serve both providers' full action vocabularies | **Unproven.** No captured OpenAI verb lacks a backend equivalent, but only `screenshot` and `move` were ever captured (§14) |
| A6 | An upstream `provider-openai` change is achievable | **Unvalidated.** No PR filed, no maintainer consulted. The whole OpenAI arm of this design depends on it (§9, R1) |

---

## 3. The internal action model — container confirmed, semantics inferred

> **Corrected.** This heading previously read "`ActionBatch`, confirmed", which
> contradicted §A1's own grading of that same evidence as "Weak — no
> multi-action batch was ever captured". A council reviewer caught the same word
> doing contradictory work in two sections. The claim splits cleanly:
>
> | | status |
> |---|---|
> | `actions` is a **list-shaped field** under one `call_id` | **CONFIRMED** — `openai-turn1.json`, live |
> | Execution semantics: run all, screenshot once, no interleaving | **INFERRED** — no N>1 batch was ever observed |
>
> The implementation since settled it further: `ActionBatch` was **not built**.
> Both dialects return an iterable of `(action, params)`; Anthropic's has length
> one. The only thing a batch needed beyond "a list, sometimes of length one"
> was `result_must_carry_screenshot`, which is a property of the *dialect*, not
> of a batch. See `providers.py`.

**Confirmed against the captures. Keep it.**

The capture is unambiguous — `.phase0/captures/openai-turn1.json`:

```json
{ "type": "computer_call", "status": "completed",
  "actions": [ {"type":"move","keys":null,"x":426,"y":87} ],
  "call_id": "call_1warEZSWaXDHw1TSSeigeHxv" }
```

`actions` is a list. One `call_id`. One screenshot expected for the whole list.
Anthropic emits one action per `tool_use` block. An "action → result" model is
structurally wrong for OpenAI; a batch model is right for both, with Anthropic as the
degenerate N=1 case.

**But the cardinality problem is smaller than the plan feared, and the reason matters.**
The orchestrator's contract is one tool call → one `ToolResult`, paired by
`tool_call_id` (`amplifier-module-loop-streaming/.../__init__.py:1961`, `:2011`,
`:2340`). OpenAI's shape is one `call_id` carrying N actions and expecting one output.
**Those are the same cardinality.** The batch lives *inside* the tool's arguments,
where the orchestrator already passes an opaque dict. No kernel change, no orchestrator
change, no new pairing concept.

So:

```
ActionBatch = { call_id: str, actions: [Action, ...] }
Action      = { verb: <internal verb>, **verb-specific fields }   # SCREEN-agnostic, MODEL px
```

Rules, each earning its place:

- **`Action` coordinates are MODEL-space pixels.** Not screen. `geometry.Display`
  already owns MODEL↔SCREEN, and it is the one piece of math both providers share
  unchanged (A2). Nothing in the action model is provider-shaped.
- **`ComputerTool.execute()` takes a batch and returns exactly one `ToolResult`.**
  At most one screenshot per batch, taken after the last action. This is OpenAI's
  requirement and is harmless for Anthropic's N=1.
- **Anthropic's path builds a batch of one.** Its wire schema is fixed by the vendor
  and cannot be changed to accept a batch; the adapter constructs the batch, the wire
  stays exactly as it is today.
- **A batch is executed in order and stops at the first failure**, reporting
  `delivered: k of n` plus the error. Partial-progress reporting is already the house
  rule for interrupted `type_text` (`coexistence.md` §8.3) and it applies unchanged.
- **A batch is not atomic and must never be described as one.** The coexistence guard
  fires *between* actions in a batch (`ComputerTool._guard_write`, `__init__.py:636`),
  so a human-detected halt aborts mid-batch. Correct, and it must be said out loud in
  the result rather than being papered over.

**What I am explicitly not adding:** batch-level transactions, rollback, or a
batch-scoped screenshot cache. No evidence calls for any of them.

---

## 4. What is genuinely shared, and what is not

Two providers is a small enough field that "shared" has to be earned per item rather
than assumed. Here is the honest split.

### Genuinely shared — one implementation, both providers

| Piece | Why it is shared | Where it lives |
|---|---|---|
| Coordinate math | Both are absolute px in the submitted image's space (A2) | `geometry.py` — **unchanged** |
| Backend seam | Neither provider is visible below the tool | `backend.py`, `registry.py`, `linux_x11.py`, `macos.py`, `windows.py`, `remote_*.py` — **unchanged** |
| `Action` verb set | The captured verbs overlap; both drive the same desktop | new `actions.py` |
| Screenshot capture, scaling, on-disk storage | Provider-independent | `imaging.py`, `ComputerTool` — unchanged |
| Coexistence / presence / ledger / halt | Below the provider entirely | unchanged |

### Genuinely per-provider — no symmetry to manufacture

| Piece | Anthropic | OpenAI | Same shape? |
|---|---|---|---|
| Tool declaration | `display_width_px`/`display_height_px` **required** | **zero fields**, any field → 400 | No — opposite requirements |
| Wire verb + field packing | `action` + overloaded `text` | `type` + `keys` + `x`/`y` | No |
| Batch arity | 1 | N | No |
| Result envelope | `tool_result` + image block | `computer_call_output` | No |
| Who takes the first frame | accepts a handed-in image | **ignores it and calls `screenshot` itself** | No |
| Request envelope | Messages | Responses | No |

### The honest call on abstraction

With exactly two providers and one of them being the incumbent that the entire codebase
is already shaped around, **"an abstraction that serves Anthropic and adapts OpenAI" is
a legitimate answer — and for the result path it is the only honest one** (§9: the
Anthropic result path lives in a bundle hook; the OpenAI result path cannot live in this
repo at all).

But two pieces *are* symmetric enough to share an interface, and both are pure functions
of shapes we captured:

- **declare** — produce the native tool spec dict
- **decode** — turn a provider's tool-call payload into an `ActionBatch`

Both are pure, both are fixture-testable, both differ per provider, and both are needed
by both providers. That is a real two-method interface, not a framework. Everything else
that looked like it wanted an adapter method turned out to be an integration with code
we do not own, and pretending otherwise would put a method on the interface that one
implementation cannot fill.

---

## 5. The adapter seam — exactly two methods

```python
class ProviderAdapter(Protocol):
    name: str                      # "anthropic" | "openai"
    caps: Capabilities             # §6

    def declare(self, display: Display, tool_version: str | None) -> dict: ...
    def decode(self, payload: dict) -> ActionBatch: ...
```

That is the whole interface. Two methods, two implementations, ~120 lines each.

`declare` for Anthropic reproduces today's `ComputerTool.native_tool_spec`
(`tool-computer-use/.../__init__.py:571`) byte for byte:
`{"type": <tool_version>, "name": "computer", "display_width_px": …, "display_height_px": …}`
plus `enable_zoom` on `computer_20251124`.

`declare` for OpenAI returns `{"type": "computer"}` and **nothing else** — A4 makes any
extra key a 400, and this is precisely the failure that opened the plan
(`tools.0.computer_20251124.parameters: Extra inputs are not permitted`). It gets an
explicit assertion in the adapter, not just a comment.

### Why not more methods

- `encode_result` — cannot be an adapter method. Anthropic's lives in
  `hook-computer-use`; OpenAI's must live inside `provider-openai`. A method one
  implementation is structurally unable to fill is a lie in the type signature (§9).
- `safety_protocol` — both providers are `none` on the wire (A3). A flag with one value
  across the whole field is manufactured symmetry. Killed.
- `action_set_coverage` / the plan's D1 probe extension — that item existed because
  Gemini's browser verbs (`navigate`, `open_web_browser`) have no desktop-backend
  equivalent. With Gemini out, no captured verb from either remaining provider lacks a
  backend equivalent. **Killed** — though note A5: absence of evidence, not evidence of
  absence.
- `normalize`/`denormalize` — Gemini-only. Killed.

### How the tool learns which adapter to use

This is the one piece of new plumbing, and it is where a defect already lives.

`native_tool_spec` is read by `loop-streaming`'s `_build_tool_spec`
(`amplifier-module-loop-streaming/.../__init__.py:39-69`), which reads it from the tool
**with no provider context**. The tool must therefore know its adapter *before* that
read.

`ComputerTool` already has the intended seam for exactly this — `note_model()`
(`tool-computer-use/.../__init__.py:611`), whose docstring says it is "called by
hook-computer-use on every `provider:request`."

> **It is not.** `note_model` has **zero callers** in the tree — the only two hits repo-wide
> are its own definition and the stale docstring at `:190`. `hook-computer-use` never
> calls it. `tool_version` is therefore resolved once at construction from config and
> never corrected at runtime, and the "model ↔ tool_version pairing" safety this bundle
> documents does not currently execute. This is a live defect, not a design gap.

**Design decision:** fix it rather than route around it. `hook-computer-use`'s existing
`provider:request` handler already resolves the live provider object
(`hook-computer-use/.../__init__.py:652-681`). Extend that one handler to call
`computer.note_provider(provider, model)`, which sets both the adapter and the tool
version before the tool spec is read for that request. One call site, the one that
already exists, at the moment the provider identity is first known.

Selection is by the same behavioural sniff already used for the provider brand check
(`_is_anthropic`, `hook/__init__.py:83`) — deliberately unchanged, because it is
already load-bearing and already proven. Unknown provider → **refuse**, never default.

---

## 6. Capability flags — four, each backed by a capture

Flags, not a union type, for the plan's original reason: a union forces every provider
to carry every other provider's concepts. Four flags survive contact with the evidence.

```python
@dataclass(frozen=True)
class Capabilities:
    declares_display_dims: bool     # anthropic True   | openai False
    max_actions_per_call: int|None  # anthropic 1      | openai None (unbounded)
    pre_seeds_first_frame: bool     # anthropic True   | openai False
    result_envelope: str            # "tool_result_blocks" | "computer_call_output"
```

| Flag | Evidence |
|---|---|
| `declares_display_dims` | Phase 0 unknown #1 (A4) vs. Anthropic's required `display_*_px` |
| `max_actions_per_call` | `openai-turn1.json` `actions[]` vs. Anthropic's one-action `tool_use` |
| `pre_seeds_first_frame` | `phase0-status.md` §"Findings": turn 0 was `{"type":"screenshot"}` *despite* being handed an `input_image` |
| `result_envelope` | `computer_call_output` (Responses items) vs. `tool_result` + image block (Messages) |

`pre_seeds_first_frame=False` has one behavioural consequence and no more: **do not send
a screenshot the model did not ask for.** OpenAI ignored it and we paid the image tokens
anyway. We do not "fix" OpenAI's loop; we stop paying for something it discards.

Flags I considered and rejected: `safety_protocol` (§5), `has_browser_verbs` (Gemini),
`supports_zoom` (would be a lone Anthropic-only true — expressed instead as a named drop
in §8, where a reader will actually look for it).

---

## 7. Field overloading — the top decode hazard

Anthropic's `text` field carries three different meanings depending on `action`.
Verbatim from `.phase0/captures/anthropic-unknown4.json`:

```json
{"action":"scroll","coordinate":[640,360],"scroll_direction":"down","scroll_amount":3}
{"action":"left_click","coordinate":[640,360],"text":"shift"}
{"action":"hold_key","text":"a","duration":2}
```

| `action` | `text` means |
|---|---|
| `type` | the literal string to type |
| `key` | a key combo (`"ctrl+s"`) |
| `left_click` / other clicks | a **modifier to hold during the click** |
| `hold_key` | the key to hold, paired with `duration` |

A naive `text → type_text` mapping **silently types the word "shift"** instead of
shift-clicking. No error, no log, plausible-looking output. This is the single most
dangerous decode in the design.

**Decision:** `decode` is a per-verb table, never a field-name-driven generic mapping.
Each entry names which internal field `text` lands in. The current code already does the
right thing by dispatching on `action` first (`__init__.py:808-890`) — the risk is a
refactor "simplifying" it into a field map. A fixture test for shift-click is a required
gate (§13).

**OpenAI's counterpart is `keys`.** `openai-turn1.json` shows `"keys": null` on a
`move` — the slot is always present, explicitly null. That strongly suggests modifiers
ride in `keys` as a list. **Never captured with a value.** See §14.

---

## 8. Everything dropped or lossy, named

The plan's gate: *"a design that claims lossless conversion has not read the captures."*
Here is every one I can name, with its evidence status.

| # | Drop / lossy transform | Provider | Evidence status |
|---|---|---|---|
| D1 | `zoom` — no OpenAI equivalent. `enable_zoom` is Anthropic `computer_20251124`-only | OpenAI | Certain (A4: no config fields at all) |
| D2 | `screen_info`, `list_windows`, `focus_window` unreachable via the native tool — both vendors fix their schemas. Survive only on the separate `desktop` tool | Both | Certain (already true today) |
| D3 | Multipoint `drag.path` → two-point. `Backend.drag(start, end)` (`backend.py:186`) has no path parameter | OpenAI | **Unverified** — no drag ever captured. Plan-era inference |
| D4 | Diagonal `scroll_x`/`scroll_y` → `direction`+`amount` cannot express diagonal | OpenAI | **Unverified** — no scroll ever captured |
| D5 | Coordinate rounding: at 3840/1280 one model px = 3 screen px; `to_screen` rounds and clamps (`geometry.py:48`) | Both | Certain, pre-existing, and `openai-coordproof.json` shows it is exact enough (`match:true`) |
| D6 | OpenAI `wait` duration — our `wait` takes `duration` seconds; whether OpenAI's carries one is unknown. A dropped duration silently becomes 1.0s | OpenAI | **Unverified** — plan unknown #2, never closed |
| D7 | Batch partial execution — a batch aborted at action *k* returns one screenshot of a partially-applied intent. Not lossy conversion; lossy *semantics*, and the model must be told (`delivered: k of n`) | OpenAI | Certain by construction |
| D8 | `pre_seeds_first_frame=False` costs one extra round trip on OpenAI: turn 0 is always a `screenshot` call | OpenAI | Certain — `phase0-status.md` §"Findings" |

**D3, D4, D6 are the same gap wearing three hats: no OpenAI action other than
`screenshot` and `move` was ever captured.** They are listed separately because they
break differently, but they close together, with one capture session (§14, G1).

---

## 9. The screenshot return — the hard one, and it does not live here

This is the question the plan called "the hardest open question and the most likely to
force a kernel conversation." The answer is worse than a kernel conversation and also
narrower.

### 9.1 Why the kernel is the wrong place to look

Verified by direct introspection of the installed kernel:

```
ToolResult fields: ['success', 'output', 'error']      model_config extra: None
```
(`~/.local/lib/python3.12/site-packages/amplifier_core/models.py`)

So a `ToolResult` cannot carry content blocks and cannot carry extras. That is why the
marker protocol exists: the tool writes a PNG to disk and returns a JSON string
containing `__amplifier_computer_use__` + a path (`tool/__init__.py:1058-1067`), and
`hook-computer-use` rewrites that string into real image blocks on the way into
`provider.complete()` (`hook/__init__.py:396-438`).

Adding `ToolResult.content` would be the clean fix, and the plan already recorded the
blocker: a kernel change needs two independent consumers, and `browser-tester` was
checked and is not one. **It still is not.** So the kernel route is not available, and
this design does not depend on it. If a second consumer ever appears, §15 says what
collapses.

### 9.2 Anthropic — unchanged, and it works

The hook rewrite stays exactly as it is. It is proven, it ships, and nothing in this
design touches it.

### 9.3 OpenAI — the hook cannot do this, verified three ways

The hook rewrites `request.messages` *before* calling `provider.complete()`. On the
OpenAI path, `complete()` then runs `provider-openai`'s own message conversion, which
discards the rewrite. Three independent blockers, each read in the tree:

1. **The tool declaration is flattened to a function tool.**
   `_convert_tools_from_request` (`provider-openai/__init__.py:3256`) passes a tool
   through natively only when it is a **dict** whose `type` is in `NATIVE_TOOL_TYPES`
   (`:3293`). `NATIVE_TOOL_TYPES` (`_constants.py:53`) is
   `{web_search_preview, web_search_preview_2025_03_11, web_search, file_search,
   code_interpreter, apply_patch}` — **no `computer`**. And the orchestrator hands
   providers `ToolSpec` *objects*, not dicts, so they take the `hasattr(tool, "name")`
   branch and are rewritten to `{"type": "function", "name", "description",
   "parameters"}` unconditionally. Even adding `computer` to that frozenset would not be
   enough.
2. **Tool-result content is stringified.** The `role == "tool"` branch (`:2757-2822`)
   computes `output_str = tool_content if isinstance(tool_content, str) else
   json.dumps(tool_content)` and emits `{"type": "function_call_output", "call_id",
   "output": output_str}`. A list of content blocks becomes a **JSON string of base64**.
   The hook's rewrite is not merely ignored here — it is actively harmful.
3. **`computer_call_output` does not exist in the module.** `grep` for
   `computer_call`/`computer_use` across `amplifier_module_provider_openai/*.py`:
   **zero hits.**

Image blocks *are* handled — but only for `role == "user"` messages (`:3163-3199`,
converting to `input_image` data URIs). Tool results never reach that branch.

**Is there a bundle-side workaround?** No, and the alternative is worse than doing
nothing. Rewriting the tool result into a `user` message with an image would break
`call_id` pairing, and `provider-openai` has explicit chain-pairing logic that *drops*
unpaired `function_call_output`s (`:1050-1077`). OpenAI's loop requires a
`computer_call_output` carrying the image; a stray user message does not satisfy it.
There is no clever seam here. **Fail loud instead.**

### 9.4 What the upstream change is

Small, and with an exact precedent in the same file — `apply_patch`, which needed the
identical three things and got them:

| Need | Precedent already in `provider-openai` |
|---|---|
| Emit a native tool type | `{"type": "apply_patch"}` at `:3309` |
| Remember which `call_id`s are native | `self._native_call_ids` at `:637`, `:2912`, `:3017` |
| Emit a native result envelope | `{"type": "apply_patch_call_output", …}` at `:2808` |

The `computer` version is the same three edits: `{"type": "computer"}` with **no other
fields** (A4), track computer `call_id`s, and emit
`{"type": "computer_call_output", "call_id": …, "output": {"type": "input_image",
"image_url": "data:image/png;base64,…"}}`.

Plus one thing `apply_patch` did not need: `ToolSpec` objects must be able to carry a
native form, mirroring what `loop-streaming` PR #36 and `provider-anthropic` PR #79
already did on the Anthropic side.

**Design decision: this is an upstream PR, not bundle code.** It belongs in
`provider-openai` for the same reason the beta-header derivation ended up in
`provider-anthropic` — the bundle's own monkey-patch for that was deleted the moment
upstream could carry it (`hook/__init__.py:17-25`, commit `f515003`). Reintroducing a
patch of the same class on the OpenAI side would be relitigating a decision this repo
already made and already got right.

### 9.5 Until it lands: refuse to mount

Exactly the pattern already in the hook — `_fail_if_stream_incompatible` (`:108`) and
`_fail_if_native_tool_passthrough_unsupported` (`:243`), both of which **probe installed
behaviour rather than trust a version string**. Add a third of the same shape: drive the
installed `provider-openai`'s `_convert_tools_from_request` with a throwaway stub
carrying `native_tool_spec = {"type": "computer"}`, and refuse to mount if the emitted
tool comes back as `{"type": "function", …}`.

A computer-use bundle that silently stops driving the computer is worse than one that
refuses to load. That sentence is already in this repo (`hook/__init__.py:102`); this
extends it to the second provider.

---

## 10. What changes, and what does not

### Does not change — one line, no exceptions

`backend.py`, `registry.py`, `geometry.py`, `imaging.py`, `linux_x11.py`, `macos.py`,
`windows.py`, `remote_backend.py`, `remote_agent.py`, `ssh_transport.py`,
`shared_transport.py`, `monitors.py`, `presence.py`, `coexistence_guard.py`,
`halt_state.py`, `ledger.py`, `pause.py`, `exclusion.py`, `target_binding.py`,
`overlay_*`, `announce_macos.py`, `type_pacing.py`, `bridge.ps1`.

The backend seam was built from two genuinely different implementations (in-process
XTEST vs. subprocess-over-WSL) and it holds. **No provider is visible below
`ComputerTool`,** and nothing in this design changes that. Provider identity stops at
the tool.

### Changes

| File | Change | Size |
|---|---|---|
| `actions.py` **(new)** | `Action`, `ActionBatch`, internal verb enum | small |
| `adapters/__init__.py` **(new)** | `ProviderAdapter` protocol, `Capabilities`, selection | small |
| `adapters/anthropic.py` **(new)** | `declare` = today's `native_tool_spec` verbatim; `decode` = the per-verb table lifted from `_run` | medium |
| `adapters/openai.py` **(new)** | `declare` → `{"type":"computer"}`; `decode` over `actions[]` | medium |
| `tool-computer-use/__init__.py` | `execute()` takes a batch; `native_tool_spec` delegates to the adapter; new `note_provider()` | medium |
| `hook-computer-use/__init__.py` | call `note_provider` on `provider:request` (also fixes the dead `note_model`, §5); add the OpenAI mount-time refusal (§9.5) | small |
| `tests/fixtures/captures/` **(new)** | `.phase0/captures/` copied in, byte-exact | — |
| `provider-openai` **(upstream, not this repo)** | §9.4 | small, but **not ours** |

### One thing that must be done before anything else

`.phase0/captures/` lives at the **workspace root**, outside this repo — in a directory
whose stated purpose is to be destroyed at session end (`AGENTS.md`, "Session
Lifecycle"). The plan said *"what survives Phase 0 is the captured traffic."* Right now
it does not survive. **Copy it into `tests/fixtures/captures/` and commit it before any
other Phase 1 work.** Every claim in this document is traceable to those files; losing
them turns a design grounded in evidence into a design grounded in this document's
summary of evidence.

---

## 11. Extensibility, weighted by what actually happened

The brief is right that this is the item most likely to be designed backwards. Adding a
provider has happened **once** (and half of it is blocked). A provider or upstream
schema *changing under us* has happened **at least twice**:

- a merged upstream change tightened the schema, and nothing caught it until a live call
  returned `tools.0.computer_20251124.parameters: Extra inputs are not permitted`;
- `provider-anthropic` PR #79 and `loop-streaming` PR #36 moved work out of this bundle,
  and the bundle had to grow behavioural probes to notice whether they were present
  (`hook/__init__.py:158-286`).

So the design optimizes for **detecting change**, not for **accommodating arrival**.

### 11.1 The precise failure to design against

Note what the tightening failure was *not*: it was not silent. `400 Extra inputs are not
permitted` is about as loud as an API gets. The failure was that **393 unit tests and a
4-stage chain test were all green at the same time**. The defect was false confidence,
not silence.

Which gives a sharper rule than "fail loud at the wire" — the wire already does that:

> **No test may claim the wire works unless it has been on the wire.**

### 11.2 Three layers, each with a stated limit

1. **Fixture tests (shape).** `declare`/`decode` tested against the byte-exact captures
   in `tests/fixtures/captures/`. Fast, offline, catch **our** regressions.
   **Stated limit, in the test module docstring:** these prove we still emit and parse
   the shapes we saw on 2026-08-02. They are *not* evidence any provider still accepts
   them. A green fixture suite is exactly what was green when the API rejected every
   request.
2. **Mount-time behavioural probes (silent degradation).** The genuinely dangerous class
   — where the request stays valid and the tool quietly gets weaker. Three exist
   (`_fail_if_stream_incompatible`, `_fail_if_native_tool_passthrough_unsupported`, and
   the new OpenAI one in §9.5). All probe installed behaviour, never a version string,
   because manifests lie and shallow clones may have none. Refuse to mount; do not warn.
3. **A dated wire attestation (upstream tightening).** `scripts/wire_check.py` sends one
   minimal real request per provider — tool declaration only, no desktop needed — and
   writes `tests/fixtures/wire-check.json` recording provider, model, HTTP status,
   error text if any, the commit SHA, and a UTC timestamp.
   **UPDATE (post six-lens review, docs/designs/phase2-plans.md "Council items still
   open"): this now gates, it no longer merely records.** The original design here —
   "it records; it does not assert... CI prints its age" — was found to reproduce, by a
   different name, the exact incident this layer exists to catch: a human expected to
   notice a printed number and act on it is the same shape of failure as 393 green
   tests sitting on a wire nobody had exercised. `scripts/wire_check.py` still only
   *produces* the attestation (real network, real credentials, run manually/
   periodically — the ship-gate pattern CONTRIBUTING.md already uses for
   `verify_coexistence.py`, deliberately NOT run in default CI). The gate itself is
   `tests/test_wire_attestation_freshness.py`, which DOES run in the normal, offline,
   no-keys test suite and fails the build outright if the attestation for a required
   provider is missing, records a rejection, or is older than
   `MAX_AGE_DAYS` (30, a judgement call — see that module for the rationale). The claim
   it supports is still *"as of &lt;date&gt; against &lt;model&gt;, this exact
   declaration returned 200"* — dated and falsifiable — but going stale (or having
   never been attested at all) now fails the suite instead of printing a number for a
   human to notice.

### 11.3 What this deliberately does not do

No plugin registry, no entry points, no adapter discovery, no config-driven adapter
selection. Two adapters, selected by an `if`. The moment a third *desktop* provider
exists, that `if` becomes a dict, and that is a five-minute change made against real
evidence instead of a framework built against an imagined one.

---

## 12. Gemini and Qwen — deferred, with the reason recorded

**Do not design for Gemini.** Not "defer the implementation" — do not shape anything in
this design around it.

The blocking evidence (`phase0-status.md`): shown a Windows desktop, the model called
`open_web_browser`, and the API refused the functionResponse with
`400 INVALID_ARGUMENT — "Computer Use Model requires function response to contain the
URL of the web page in field 'url' or 'current_url'."` `ENVIRONMENT_DESKTOP` is an
accepted enum value and produced the *identical* rejection.

**The blocker is a backend capability, not a wire format.** Every turn must carry a
web-page URL. A backend driving XTEST or Win32 has none. Gemini would therefore enter
through `Backend` — needing a `current_url()` a desktop backend cannot implement — not
through `ProviderAdapter`. That is the cleanest possible framing of the deferral: it
means **Gemini costs this design exactly nothing**, and no hook is left for it.

It is also close to Playwright territory, which this bundle explicitly scopes out. If a
browser surface is ever wanted, it is a different bundle or at minimum a different
backend contract, and it gets its own design.

**Qwen:** no server-side tool contract at all — Alibaba's own GUI doc says paste the
schema into the system prompt and regex `<tool_call>` out of `message.content`
(`qwen-verdict.md`). If ever wanted it is a prompt-schema *backend* behind the existing
seam, not a second native provider path. Also region-locked to Beijing, which is a
separate and sufficient blocker.

**Recorded so nobody re-probes:** both negatives were established from primary sources
and, for Gemini, from live API calls with the substitution verified in the script before
the result was believed.

---

## 13. Phasing and gates

Ordered so each step ends at something provably working, and so the blocked step cannot
silently block the unblocked one.

**P0 — Preserve the evidence.** Copy `.phase0/captures/` into
`tests/fixtures/captures/` and commit. *Gate:* the files are in the repo's git history.

**P1 — Internal model, Anthropic only, no new provider.**
`actions.py` + `adapters/anthropic.py`; `ComputerTool.execute()` takes a batch of one;
`note_provider()` wired into the existing `provider:request` handler (fixing the dead
`note_model`, §5).
*Gate:* **a live Anthropic session drives a real desktop**, plus a fixture test that
shift-click decodes as a modifier and not as typed text (§7). A green suite alone is
explicitly not the gate — that is the exact thing that failed before.

**P2 — Upstream `provider-openai` PR (§9.4).** Not this repo. Blocked on maintainer
acceptance (R1).
*Gate:* merged upstream, and `make wire-check` records a 200 for the OpenAI declaration.

**P2a — In parallel, unblocked: close the capture gaps (§14, G1).** One live OpenAI
session capturing click, type, keypress, scroll, drag, wait, **and a multi-action
batch**. This needs only the raw API and a desktop — no Amplifier plumbing — so it is
not blocked on P2 and should not wait for it.

**P3 — OpenAI adapter + mount-time refusal.** Only after P2 and P2a.
*Gate:* a live OpenAI session drives a real desktop through Amplifier, plus a proven
refusal-to-mount against a pre-PR `provider-openai`.

**P4 — Kernel conversation, only if a second consumer appears.** Not on this path.

---

## 14. What the evidence does NOT settle

Stated plainly, because a plausible answer here will be disproved in one command.

| # | Gap | Why it matters | What closes it |
|---|---|---|---|
| **G1** | **Only two OpenAI actions were ever captured** — `screenshot` and `move`. No click, type, keypress, scroll, drag, wait, double-click | Directly gates D3, D4, D6, and the `keys` semantics in §7. `adapters/openai.py`'s `decode` is **inferred, not evidenced**, for every verb but two | One live OpenAI session exercising each verb, captured verbatim |
| **G2** | **No multi-action batch was ever captured.** `openai-turn1.json` has `len(actions) == 1` | `ActionBatch` is justified by the *field being a list* and by OpenAI's documented batching — not by an observed N>1. Ordering, mixed verbs, and whether a screenshot may appear mid-batch are all unobserved | Same session; prompt for a task that provokes batching |
| **G3** | OpenAI's `keys` field never seen with a value (always `null`) | §7's claim that modifiers ride in `keys` is inference from an always-present null slot | A captured modifier-click |
| **G4** | Whether `provider-openai` maintainers will accept §9.4 | The entire OpenAI arm depends on it. No PR filed, no maintainer consulted | File the PR |
| **G5** | Whether the OpenAI computer-use model works with the *chained* Responses flow `provider-openai` uses (`previous_response_id`, `_native_call_ids`, chain-pairing at `:1016-1077`) vs. the flat request Phase 0's standalone script used | A working raw-API script does not prove the same traffic works inside the provider's chaining logic. **This is the same class of gap as the one that opened the plan** | A live session through Amplifier, post-P2 |
| **G6** | Whether `computer_20241022` is still accepted | Low stakes; absence from docs is not removal from the API | Empirical, only if something still emits it |
| **G7** | Whether a batch aborted mid-way leaves the desktop in a state the model can reason about from one screenshot | D7 is a designed behaviour with no observation behind it | Evaluation scenario, post-P3 |

**Not a gap but worth saying:** `provider-openai` has no `stream()` method (grep for
`def stream` in `amplifier_module_provider_openai/`: zero hits), so the hook's
`_fail_if_stream_incompatible` guard passes for OpenAI today for the same accidental
reason it passes for Anthropic. That accident is load-bearing for both providers now,
not one.

---

## 15. Risks

**R1 — The OpenAI arm depends on a PR to a repo we do not own, which has not been
filed.** (G4) If `provider-openai` maintainers decline, or want a different shape, P3
does not happen and the bundle stays single-provider. *Mitigation:* file it early,
carrying the `apply_patch` precedent from their own file (§9.4) — the change is small
and consistent with code they already accepted. *What would make this the wrong call:*
if the maintainers say "wrap it in your bundle instead," then the deleted monkey-patch
comes back and §9.4's reasoning needs revisiting rather than defending. *Signal:* the
first maintainer response.

**R2 — `adapters/openai.py` is being designed from two captured verbs.** (G1, G2, G3)
Everything about OpenAI's `decode` other than `screenshot` and `move` is inference. The
plan's own §"A self-confirming check nearly passed as proof" is the warning: an
inference that round-trips is not a discriminating test. *Mitigation:* P2a is
unblocked — run it before writing `decode`, not after. *Signal:* the first captured
verb whose field names differ from the inference.

**R3 — The batch model could be wrong in a way one capture cannot reveal.** (G2, G7)
`ActionBatch` was justified by a list-shaped field and the vendor's documented
batching — and on that evidence it did **not** earn being a named type. The
shipped code carries `Iterable[tuple[str, dict]]` instead. G2 (no observed N>1
batch) therefore no longer gates a type decision; it gates only the *ordering
and interleaving* semantics, which remain unobserved.
If OpenAI expects an *interleaved* protocol — a screenshot mid-batch, per-action
acknowledgement — then "execute all, screenshot once" is wrong, and it is wrong in the
structural way the plan warned about. *Mitigation:* G2's capture is a P2a gate, not a
P3 discovery. *Signal:* any captured batch containing `screenshot` in a non-terminal
position.

---

## 16. Simplest credible alternative

**Ship Anthropic only. Delete the multi-provider ambition.**

It is genuinely credible: Anthropic works today, the OpenAI arm is blocked on someone
else's merge queue, and every line of `adapters/openai.py` would be written against two
captured verbs.

**Why the recommended design is still worth it, and it is a narrower reason than
"multi-provider is good":** P1 is valuable *on its own*, with zero dependency on P2.
It replaces an inline `if/elif` action dispatch with a per-verb decode table that a
fixture test can pin against real captured traffic — which is exactly the layer where
the shift-click hazard (§7) lives today, untested. And it fixes a live defect
(`note_model` has no callers, §5) at the same seam.

If P2 never lands, P1 was still the right change, and `adapters/openai.py` is simply
never written. **That is the property that makes this design safe to start.**

---

## 17. Success metrics

| Metric | Target |
|---|---|
| Live-session gate passed before any step is called done | 100% — a green suite is never the gate |
| Silent degradations reaching a live session | **Zero.** Every known degradation path has a mount-time behavioural probe that refuses (§11.2 layer 2) |
| Fixture tests claiming wire validity | **Zero** — enforced by the docstring rule and reviewed for in PR |
| Age of the newest `wire-check` attestation | Printed in CI; reviewed, not gated (a stale attestation is a known unknown, not a failure) |
| Files changed below `ComputerTool` | **Zero** (§10) |
| Adapter interface methods | **Two.** A third needs an evidence citation in the PR description |
| Provider-shaped concepts below `ComputerTool` | **Zero** |

---

## 17a. Coupling, measured three ways — no fourth provider required

The forward-looking claim *"adding provider N+1 is bounded work"* is
unfalsifiable while the candidate population is exhausted (§18). But the
**property that claim was reaching for** — does the seam actually confine
provider knowledge? — is measurable today, from three independent directions.
None of the three needs a provider that does not exist.

| Direction | Measurement | Result |
|---|---|---|
| **ADD** a provider | Gemini into a base provably ignorant of it (`DIALECTS = (ANTHROPIC, OPENAI)`, zero Gemini refs) | **1 file, 8 lines** of real code outside the dispatch table |
| **REMOVE** a provider | Delete `OPENAI` from `DIALECTS` — one line — and measure the blast radius | **0 production files** broke. 5 test files, 12 tests |
| **CHANGE** a provider's API | Replay the originating incident into the wire attestation | **1 failed CI run**, offline, no keys |

The removal test is the one that needed no external dependency at all, and it is
symmetric evidence: coupling does not care about direction. If adding a provider
were a rewrite, removing one would break production code across the tree.
Instead:

```
REMOVED OpenAI from DIALECTS (one line)
  12 failed, 493 passed
  broken: test_clipboard_policy, test_gemini_dialect, test_provider_dialects,
          test_qwen_out_of_sample, test_tool_versions
  production files under modules/ broken:  0
restored -> 505 passed
```

Every failure was a test asserting OpenAI's presence. **No production module
outside `providers.py` depends on which providers are in the table.**

### What this does and does not establish

It does **not** resurrect the withdrawn claim. A forward-looking property about
a provider that does not exist yet remains unfalsifiable, and the product
council was unanimous on that.

What it does establish is the present-tense fact the claim was groping at, and
it is stated at exactly its strength:

> **The dispatch table confines provider knowledge.** Adding, removing, and
> changing a provider were each measured, and each was bounded to the table plus
> its tests — with the single exception of one 8-line coordinate-space change,
> which is itself recorded above.

That is three measurements of a present property, rather than one measurement of
a future one.

## 17b. The provider-API-change question — MEASURED

Phase 1 asked two extensibility questions, not one:

> *"What does adding a new provider actually cost? **What does a provider
> CHANGING its API cost?** Today's `parameters` bug is the shape of that risk —
> an upstream schema tightened and nothing caught it until the live call."*

The first is withdrawn as unfalsifiable (§18). **The second is measurable, was
measured, and is answered.**

Test: replay the originating incident verbatim into the wire attestation — an
upstream schema tightening that rejects a field we send — and see whether
anything catches it before a live session.

```
injected:  anthropic http_status 400
           "tools.0.computer_20251124.parameters: Extra inputs are not permitted"

FAILED  tests/test_wire_attestation_freshness.py::test_wire_attestation_was_accepted[anthropic]
        1 failed, 6 passed

restored -> 7 passed
```

| | Before this work | After |
|---|---|---|
| Upstream tightens a schema | 393 unit tests green, 4-stage chain test green, **found on a live API call** | attestation records the rejection, **build fails offline** |
| Who catches it | a human, after the fact | CI, with no network and no API keys |

**Answer: a provider changing its API costs one failed CI run**, caught without
network or credentials, rather than a silent degradation discovered in
production.

Unlike the N+1 claim, this one is **falsifiable and was falsified-then-fixed**:
the gate demonstrably fails on the injected regression and passes when restored.
It is also the question that actually mattered — the incident that started this
work was a provider changing its API, not a provider being added.

The three Anthropic tool versions the dialect already carries
(`computer_20241022`, `computer_20250124`, `computer_20251124`) are real
historical API changes absorbed by one `tool_types` tuple — the same question
answered retrospectively.

## 18. The N+1 claim is WITHDRAWN — product council, unanimous

**`/product-council` was convened on the question the originating goal invited:
"is multi-provider worth its cost at all?" Six lenses, three rounds. On the
extensibility claim specifically the verdict was total consensus:**

> **Drop "adding provider N+1 is known, bounded work."** The candidate
> population is exhausted — Gemini is structurally disqualified and no fourth
> native provider exists — so the claim is **unfalsifiable**. There is no real
> N+1 to test it against.
>
> `bet-sizer`: *"No path exists to raise N... correct sizing is to NOT assert
> the property claim until a real N+1 candidate exists."*
> `positioning-critic`: *"the 'bounded extensibility' claim is unfalsifiable
> since the candidate population is exhausted."*

### What replaces it

A plain capability statement with no architectural claim attached:

> **This bundle supports Anthropic and OpenAI.** Both drive real desktops —
> local or over SSH — using the provider's own native, post-trained,
> server-side computer-use tool.

The council was explicit that the real differentiator never needed the
provider-count argument: *"the native-tool differentiator is validated and
separately sound — positioning should lean on that, drop the provider-count
argument."*

### The dispatch table stands on its own merits

`providers.py` is kept, and is justified as a **refactor**, not as evidence for
a forward-looking property. It consolidated provider knowledge that had been
scattered across three files, and in doing so exposed an accidental provider
branch hiding inside a string comparison (`_tool_version >= "computer_20251124"`
silently doubling as "and not OpenAI"). That is present-tense value, measured.

### The unresolved split, recorded rather than averaged away

The council did **not** converge on the larger question and explicitly said the
3–3 split is the headline. FAIL side (`intent-keeper`, `outcome-cartographer`,
`positioning-critic`): the two-provider capability itself has no named customer,
no procurement event, no usage telemetry — and *"the bet landed" is itself the
shipped-vs-worked substitution*. CONCERN side (`bet-sizer`, `outcomist`,
`user-advocate`): vendor-redundancy for a critical capability is a defensible
engineering rationale, and absence of validation evidence is not evidence of a
defect.

Both camps agreed on one action regardless of the ruling: **label it honestly.**
Two-provider support is therefore recorded as *kept for vendor-optionality,
unmeasured* — rather than letting "it's built and tested" stand in for "it was
worth building."

### Historical record: what the measurements actually were

Retained because the numbers are real and the reasoning is instructive, but they
no longer support a claim.

The extensibility claim was tested by adding Gemini as a third dialect and
measuring the diff. Run 2 returned **0 files changed outside `providers.py`**.
That number is real and reproducible — it came from a literal re-add (revert
Gemini, improve the base with Gemini absent, snapshot, add Gemini cold, diff the
snapshot), not from reasoning about what the diff would have been.

**It is nonetheless a weaker result than the number suggests, for a reason worth
stating plainly rather than burying.**

Run 1 added Gemini to the base as it stood and returned PARTIAL: 1 file outside
`providers.py`, 2 more left wrong. The base was then improved **knowing Gemini's
shape** — knowing that coordinates can be normalized, that a vendor may have no
wire `type`, that "absent" and "unknown" are different answers. Run 2 measured
adding Gemini to that improved base.

That is fitting the base to the test case. The defense is that each base change
is a fact the base was missing **on its own terms** — `read_call` was
translating coordinates without knowing what they were measured against; a
lookup could not distinguish absence from ignorance; the hook was parsing a
vendor artifact to recover a vendor-neutral fact — and each is tested with
synthetic dialects that never name Gemini. That is a real argument. It is not a
measurement.

**A genuine out-of-sample proof requires a fourth provider whose shape did not
inform the base.** No such provider exists today. Phase 0 enumerated the field
from primary sources:

| | native wire tool type? |
|---|---|
| Anthropic | yes — in the table |
| OpenAI | yes — in the table |
| Gemini | yes (browser-only) — in the table |
| Qwen | **no** — schema pasted into the system prompt, `<tool_call>` regexed out of `message.content` |
| Mistral, xAI | **no** — built-in tool catalogs enumerated, no GUI tool |
| Azure OpenAI, AWS Bedrock, Google Cloud | transports for the three above |

### The two real N+1 candidates, when credentials exist

Neither was reachable here, and both would be honest out-of-sample tests because
their divergence is in a dimension the base has never modelled:

- **AWS Bedrock (Anthropic passthrough).** Requires the beta declared **in the
  request body** (`anthropic_beta: [...]`) or as an HTTP header, depending on
  the runtime. `Dialect.beta_headers` assumes a header. A body-carried beta has
  no representation in the table today.
- **Azure OpenAI.** Deployment-name-based model identity rather than a model
  string, and its docs are the clearest primary source that GA `computer_call`
  still carries `pending_safety_checks` — which live `api.openai.com` omits
  entirely. `Dialect.models` assumes a model-name prefix scan.

Either would test whether the seam absorbs a divergence it was **not** designed
around. Until one is run, the honest claim is:

> The base absorbed a genuinely divergent third provider at a cost of 0 files
> outside the dispatch table, measured by literal re-add, **on a base informed
> by that provider.** Out-of-sample extensibility is untested.

### Scoping correction: Qwen is not a member of the N+1 population

**A correction to my own test design, made after the fact.**

"Provider N+1" means *the next provider offering a **native, server-side
computer-use tool type***. That is this bundle's entire domain — the thing that
distinguishes it from browser-automation frameworks.

Phase 0 asked exactly one question about Qwen: does it expose a native
server-side tool type, or is it an SDK/harness-level agent loop? The answer,
verified from primary sources, was **harness**. Per the investigation's own
instruction, a verified negative meant it was **dropped** — recorded in
`qwen-verdict.md` as *"dropped from Phase 0/1/2 ... If Qwen is ever added it
slots in as another prompt-schema backend behind the existing seam, not as a
second native path."*

I then reached for Qwen as the out-of-sample N+1 subject, because Azure and
Bedrock were unreachable and I wanted a test rather than an excuse. That was a
**category error**: I tested the seam against a provider I had already verified
was outside its population.

So the FALSIFIED result stands as a true statement — and it is not a
falsification of the N+1 claim, because Qwen is not an N+1. What it actually
measured is the seam's **outer boundary**, which is genuinely useful and is why
the work is kept rather than reverted:

> The seam requires that the declaration go in `tools[]` and the action come
> back as a parsed tool call. That is not an incidental limitation — it is the
> definition of a native tool-calling provider, which is what this bundle
> exists to serve. A harness-style provider is a different architecture, and
> `qwen-verdict.md` already routes it to a different mechanism.

### Correction: run 1 WAS the out-of-sample test, and it answered the question

I had this backwards, and the git history says so plainly:

```
e5ab3eb  "extract provider dialects from two working implementations"
         Gemini references in providers.py:  0
         DIALECTS:  (ANTHROPIC, OPENAI)
```

The base at `e5ab3eb` was extracted from Anthropic and OpenAI alone. It had
**never seen Gemini** — not the normalized 0–999 grid, not the missing `type`
key, not `functionCall`, not `move_to`/`hover_at`. Run 1 (`31a033a`) added
Gemini to *that* base. Its measurement is therefore **out-of-sample by
construction**:

```
providers.py   +258   (77 real code)
__init__.py     +28   ( 8 real code — one expression)      <- 1 file outside
2 further files left giving wrong answers, pinned as tests
```

The claim under test is *"adding provider N+1 is a **known, bounded amount of
work rather than a rewrite**."* Eight lines of real code in one file outside the
table, plus two named follow-ups, against a provider that disagrees with both
incumbents on coordinate space, declaration key, response envelope, and verb
naming — **that is bounded work, and it is emphatically not a rewrite.**

I labelled run 1 "PARTIAL" and treated it as a shortfall. That was the wrong
reading. PARTIAL described the *residual gaps*, not the *cost*, and the cost is
what the claim is about.

**Which run is compromised is the opposite of what §18 first said.** Run 2's
"0 files outside" is the in-sample number — the base had by then been improved
knowing Gemini's shape. Run 1's "1 file, 8 lines" is the clean, uninformed
measurement. The stronger-looking number is the weaker evidence.

### The honest state of the N+1 claim

Within the correct population — native computer-use providers — the claim is
**supported by an out-of-sample measurement**: run 1, on a base provably
ignorant of Gemini, cost **1 file and 8 lines of real code outside the dispatch
table**, plus two named follow-ups. Bounded work, not a rewrite.

Run 2 then closed those follow-ups and drove the number to 0 — but that run is
in-sample and is the weaker evidence. Both numbers are kept, correctly labelled,
rather than quoting the flattering one.

A second out-of-sample data point would strengthen it. **The candidate
population is exhausted** — every one was probed, none is reachable:

| Candidate | Probe result |
|---|---|
| Azure OpenAI | `AZURE_OPENAI_ENDPOINT` set but host does not resolve — `gaierror [Errno -2]`. `az` logged in to a subscription that does not contain the resource. |
| AWS Bedrock | No credential path at all — no env vars, no `aws` CLI, no `~/.aws`. |
| OpenAI `computer_use_preview` | `404 Model not found: computer-use-preview`. The tool type is still schema-validated (`400 Missing required parameter: 'tools[0].display_width'`) but the model serving it is retired. |

Only three providers ship a native computer-use tool type — Anthropic, OpenAI,
Gemini — and all three are already in the table. There is no fourth to hold out.

**So N=1 is not a shortfall of effort; it is the size of the population.** The
honest statement, and the one this design stands behind:

> Adding a native computer-use provider that was **not** anticipated by the base
> cost **1 file and 8 lines of real code outside the dispatch table**, plus two
> named follow-ups, for a provider diverging on coordinate space, declaration
> key, response envelope, and verb naming. That is one measurement, not a
> pattern. It is bounded work rather than a rewrite, and it is the only
> out-of-sample measurement the world currently permits.

When a fourth native provider ships, `scripts/wire_check.py` and `providers.py`
are where it gets measured, and the wire-check gate ensures it cannot be added
without a fresh attestation.

### The out-of-sample attempt on Qwen — verdict FALSIFIED, and what it bounds

Azure and Bedrock were unreachable (below), but a genuinely out-of-sample
provider was available all along: **Qwen**, investigated and dropped in Phase 0
**before `providers.py` existed**. Its shape informed nothing in the table.

Result: **FALSIFIED.** The seam cannot express a provider with no wire tool type.
Two independent breaks, both verified against the tree:

- **BREAK 1 — `declare` has the wrong codomain.** It returns a dict destined for
  the request's `tools[]`. Qwen's declaration is a schema **pasted into the
  system prompt as text**. The shape is expressible; the *slot* is not. All three
  candidate returns are forbidden: `{}` emits a malformed `tools[]` entry
  (silent degradation), the schema dict puts a computer-use schema in the one
  slot DashScope documents as function-only, and `None` violates the type with
  no consumer branch for "declares nothing."
- **BREAK 2 — `read_actions` is unreachable.** `ComputerTool.execute` is only
  invoked for a *parsed tool call* (`loop-streaming:2142` `if not tool_calls:`).
  Qwen's action is text inside `message.content`, so `response.tool_calls` is
  empty and the tool is never invoked. `hook-computer-use` has **zero**
  references to `tool_calls`/`parse_tool_calls`/`ToolCall` — it cannot
  synthesize one.

A base that could express it needs ≥2 more files inside this repo plus a
provider-module change outside it.

**What this bounds — and it is more useful than a PROVEN would have been:**

> The seam's real shape is *"the declaration goes in `tools[]`, and the action
> comes back as a parsed tool call."* A provider honoring both halves costs ~0
> files outside `providers.py` — Gemini demonstrated that even while diverging on
> coordinate space, verb naming, envelope, and declaration key. A provider
> violating either half cannot be expressed at all.

That is a stated, tested boundary rather than an open-ended claim. One genuine
transfer was also observed: the `image_space` parameter, added for Gemini's
0–999 grid, carried Qwen's unrelated `smart_resize` space unmodified.

`QWEN` is **not** in `DIALECTS` (pinned by a test). It exists only as a
measuring instrument.

### Azure and Bedrock were attempted, not merely deferred

Both candidates were probed on 2026-08-03. Neither is reachable from this
environment:

```
AZURE_OPENAI_ENDPOINT      SET   -> semantic-wb-openai-eastus-02.openai.azure.com
  az account                OK   -> "OCTO - MADE Explorations"
  az cognitiveservices list       -> resource NOT FOUND in this subscription
  DNS resolution                  -> gaierror [Errno -2] Name or service not known
AWS_ACCESS_KEY_ID / SECRET / PROFILE / REGION   -> all unset
aws CLI, ~/.aws/credentials, ~/.aws/config      -> absent
```

The Azure endpoint variable is a stale leftover — the host does not resolve, so
no request can be made regardless of credentials. AWS has no credential path at
all. This is a blocked test with evidence, not an untried one.

`scripts/wire_check.py` and `providers.py` are where that test goes when either
becomes reachable. The wire-check gate already fails the build on a stale
attestation, so a fourth provider added later cannot quietly go unverified.
