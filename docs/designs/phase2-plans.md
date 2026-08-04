# Phase 2 — implementation plans, for review

Written after Phase 0 (live-API spikes) and Phase 1 (design + 6-lens council).
**Do not implement from this without reading the two blocking findings first.**

## The two findings that shape every plan below

**1. Gemini and Qwen are out.** Gemini is browser-bound — shown a desktop it
called `open_web_browser`, then the API refused our result: `400 "requires the
URL of the web page"`. `ENVIRONMENT_DESKTOP` gave the identical rejection. Qwen
has no wire tool type at all. Neither is a desktop provider.

**2. ~~The OpenAI arm is blocked by OpenAI's own SDK.~~ RETRACTED — see below.**
Verified by replaying our real captured GA payload:

```
SDK 2.8.1  (installed)   actions[]: False   pending_safety_checks: REQUIRED
SDK 2.52.0 (latest)      actions[]: True    pending_safety_checks: REQUIRED
replay openai-turn1.json through 2.52.0  ->  REJECTED
    1 validation error for ResponseComputerToolCall
    pending_safety_checks  Field required
```

Live GA omits `pending_safety_checks` entirely. The SDK marks it required with
no default. **The official Python SDK cannot parse the official API's own GA
computer-use response**, and upgrading does not fix it — 2.52.0 gained
`actions[]` but kept the required field.

The provider calls `client.responses.create(...)`, so the SDK deserializes
before any provider code runs. PR #58 is **necessary but not sufficient**.

---

## Plan A — Anthropic. Ships value now. Not blocked on anything.

This is the plan the council asked for: *"Ship the Anthropic-only decode-table
refactor and the `note_provider` fix now. Defer `ActionBatch`, `Protocol`, and
`Capabilities` until a second real implementation exists to extract them from."*

**A1. Fix `note_model` — it has zero callers.**
Its docstring (`tool/__init__.py:190`) claims hook-computer-use calls it on every
`provider:request`. The hook never does. Model↔tool_version pairing is resolved
once from config and never corrected. Real defect, found while designing.
*Proves:* a live session where the configured model and the actual model differ,
and the tool version follows the actual one.

**A2. Decode table for Anthropic actions, driven by the captured fixtures.**
`tests/fixtures/captures/anthropic-unknown4.json` holds the real shapes:
`scroll_direction`/`scroll_amount`, and modifiers riding in `text` — the same
field `type` uses, overloaded by action. Encode that overloading explicitly
rather than leaving it implicit in branching code.
*Proves:* live session drives the real desktop; existing suite stays green.

**A3. Do NOT build `ProviderAdapter`, `Capabilities`, or `ActionBatch` yet.**
Council, unanimous: an interface extracted from one implementation and one guess
is manufactured symmetry. Extract when a second real implementation exists.

**Risk:** low. Nothing here touches the proven path's contract.

---

## Plan B — OpenAI. Blocked. Do not start the adapter.

**B0. GATE — CLEARED.** The raw-response path dissolves the blocker. Live call,
SDK 2.52.0:

```
responses.with_raw_response exists: True
RAW parse OK. output items: ['reasoning', 'computer_call']
computer_call keys: ['actions', 'call_id', 'id', 'status', 'type']
has pending_safety_checks: False
actions: [{"type": "screenshot"}]
```

`with_raw_response` returns the JSON body without running the typed model, so
`ResponseComputerToolCall`'s required-field defect never applies. This is an
established pattern in the ecosystem — `provider-anthropic` already uses
`with_raw_response` on its non-streaming path.

The raw body also independently re-confirms two Phase 0 findings from a second
code path: `actions[]` present, `pending_safety_checks` absent.

**Consequence:** the OpenAI arm is viable, and Phase 2 is two plans, not one.

**Correction to my own blocker claim.** I reported the SDK as a hard blocker
after testing `model_validate()` — the *strict* path. The SDK defaults to
*lenient* `construct_type()` (`_strict_response_validation = False`), and an
**unpatched live call succeeds today**. Verified:
`STRICT -> REJECTED` / `LENIENT -> PARSES, pending_safety_checks = None` /
`UNPATCHED live -> SUCCEEDED`. The type-declaration gap is real but only bites
under `OPENAI_STRICT_RESPONSE_VALIDATION=true` or a future default flip. The
raw-response path is therefore **defensive hardening, not a crash fix** — and
the OpenAI arm was never actually blocked.
PR #58 must switch its response parsing to the raw path — as filed it parses via
the typed model and would still fail at runtime despite 603 green tests, because
those tests mock the SDK boundary.

**B1. PR #58** (`microsoft/amplifier-module-provider-openai`, open, 603 tests
green) — necessary regardless, insufficient alone. Do not merge until B0 is
settled, or it lands as reachable-looking code that still cannot run.

**B2–B4** (adapter, decode table, live session) — **do not start.** Council: the
design was writing `adapters/openai.py` from exactly two captured verbs
(`screenshot`, `move`), with **no multi-action batch ever observed**. Capture
more verbs and a real N>1 batch first; that needs no Amplifier plumbing and can
run the moment B0 clears.

**Risk:** high, and outside our control. Two of three options depend on repos we
do not own.

---

## Plan C — Anthropic updates the shared base would require

Deliberately empty, and that is the finding. Because Plan A defers the
abstraction (A3), **there is no shared base yet**, so there are no Anthropic
changes to fit into one. This is the section that would have quietly become
"Anthropic plus adapters" if the abstraction had been built on spec. It gets
written when B0 clears and a second implementation actually exists.

---

## Sequencing

```
NOW        A1, A2          unblocked, ships value, low risk
NEXT       B0              one check: does the raw-response path dissolve it?
THEN       capture verbs   no plumbing needed, kills risk R2
IF B0 OK   B1 merge -> B2-B4 -> Plan C written for real
IF B0 NOT  Plan A is Phase 2. Say so plainly and stop.
```

## Council items still open against the design doc

- §3 says `ActionBatch` is "confirmed against the captures"; the same doc grades
  that evidence "Weak — no multi-action batch was ever captured." Split the
  claim: the **list-shaped container** is confirmed by the wire; the **execution
  semantics** are inference.
- ~~Wire-check is advisory ("records; does not assert"). The worst incident here
  was 393 green tests on a wire nobody exercised. An advisory check reproduces
  that failure by a different name — gate it.~~ **CLOSED.** `scripts/wire_check.py`
  now only produces the dated attestation (real network, run manually — the
  ship-gate pattern, not default CI); `tests/test_wire_attestation_freshness.py`
  is the actual gate, runs offline in the normal suite, and fails the build if the
  attestation is missing, records a rejection, or is older than 30 days. See
  `docs/designs/multi-provider-design.md` §11.2 point 3.
- ~~Out-of-range model coordinates are silently clamped to the nearest valid pixel
  and the action "succeeds" against the wrong target. Live silent-degradation
  path in a codebase with a hard no-fallbacks rule.~~ **CLOSED.**
  `Display.to_screen` (`geometry.py`) now raises `CoordinateOutOfRangeError` (a
  `ValueError`, caught by `ComputerTool.execute`'s existing handler and surfaced
  to the model as an ordinary tool error) for any model coordinate more than
  `_EDGE_TOLERANCE_PX` (2px) outside the image the model was shown. Coordinates
  at the image's own edge - including the common "used the dimension instead of
  dimension-1" off-by-one - still clamp exactly as before.
- ~~Refuse-to-mount has no specified operator-facing message. Fail-loud-to-the-
  system and fail-loud-to-the-human are different properties; only the first
  was built.~~ **CLOSED.** `NoBackendAvailable` (`registry.py`) now appends
  explicit remediation (fix the failing backend, or set
  `config.target='ssh://user@host'`) to its message. The capability-probe
  rejection in `_wrap_provider` (hook-computer-use) now logs at WARNING, not
  INFO, and states both possibilities a negative probe cannot distinguish
  (wrong vendor vs. right vendor on an old build) plus what to do for each.
