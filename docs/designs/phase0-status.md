# Phase 0 — status

## Providers

| Provider | Verdict | Evidence |
|---|---|---|
| Anthropic | control, already shipping | live session drives real desktop |
| OpenAI | **PROVEN** | live API + live desktop, coordinate proof, 3 unknowns closed |
| Gemini | **BLOCKED for desktop** | coordinate space PROVEN; desktop task structurally refused - see below |
| Qwen | **HARNESS ONLY - dropped** | verified by own fetch of DashScope reference |

## Unknowns

| # | Question | Status | Answer |
|---|---|---|---|
| 1 | OpenAI GA `computer` config fields? | **CLOSED** | **ZERO.** bare 200; `display_width`/`environment`/`display_width_px` all 400 `Unknown parameter` |
| 2 | OpenAI action object schema | **CLOSED** | `actions[]` (batched key). Real action: `{"type":"move","keys":null,"x":426,"y":87}` - `keys` slot always present, explicitly null |
| 3 | OpenAI `pending_safety_checks` still emitted? | **CLOSED** | **Key absent entirely** - not null, not `[]`. Preview-era vestigial, as suspected |
| 4 | Anthropic `scroll` / modifier field names | **CLOSED** | `scroll_direction` + `scroll_amount`. **Modifiers ride in `text`** - the same field `type` uses, overloaded for two meanings. `hold_key` takes `text` + `duration` |
| 5 | Gemini coord range 0-999 vs 0-1000 | **CLOSED** | **NORMALIZED 0-999.** Decisive: `y=999` returned on a 720px-tall image - impossible in pixel space. Edge targets pin to exactly 999 |
| 6 | Gemini `scroll.magnitude_in_pixels` px or normalized? | **CLOSED** | Field is **`magnitude`**, not `magnitude_in_pixels`. Value `999` on a max-scroll request - the same normalized ceiling as coordinates. Not pixels |

## Captured traffic
`.phase0/captures/` - openai-turn{0,1,2}.json, openai-coordproof.json,
openai-findings.json, gemini-response-1.json, gemini-coordproof.json,
gemini-unknown5.json, qwen-verdict.md

## Findings the design must absorb

**OpenAI ignores a supplied `input_image` and demands its own screenshot first.**
Turn 0 was `{"type":"screenshot"}` despite being handed a screenshot as
`input_image`. Its loop wants the TOOL to produce the image. That is a different
contract from the Anthropic bundle's current shape and is not a detail - it
changes who owns the first frame.

**Gemini verb names differ from the documented set.** Live traffic returned
`move_to` and `hover_at`; research had `move`. Verb-name mapping must come from
captures, not docs.

**Coordinate spaces confirmed as three distinct models:**
 - Anthropic: px in a space you DECLARE
 - OpenAI: px in the submitted image's space, undeclared (1280x720 -> 3840x2160
   mapped exactly, match=True, zero drift)
 - Gemini: normalized 0-999, aspect-agnostic

**A self-confirming check nearly passed as proof.** The first Gemini coordinate
run moved to the normalized reading and reported `match=True` - but the move was
commanded, so it only proved the mover worked. The edge-probe (targets pinned at
999 on a 720px image) is what actually discriminates. Any future coordinate
claim needs a discriminating test, not a round-trip.

## GEMINI IS BROWSER-BOUND — the blocking finding

Shown a Windows **desktop** screenshot, the model's first call was
`open_web_browser`, and the API then **refused** our functionResponse:

    400 INVALID_ARGUMENT
    "Computer Use Model requires function response to contain the URL of the
     web page in field 'url' or 'current_url'."

This is not a misconfiguration. `ENVIRONMENT_DESKTOP` **is** an accepted value
(probed: BROWSER/DESKTOP/MOBILE/UNSPECIFIED all 200; `DESKTOP` and a bogus value
both 400 with the enum name). Re-running the identical task under
`ENVIRONMENT_DESKTOP` produced the **same** `open_web_browser` call and the
**same** URL-required rejection. Substitution verified in the script before
believing the result.

So on `gemini-2.5-computer-use-preview-10-2025` — the only computer-use model
the API lists — every turn must carry a web-page URL. A desktop backend driving
XTEST or Win32 has no URL to supply. **Gemini cannot drive a raw desktop through
this path**, independent of the environment flag.

That is a legitimate Phase 0 outcome, not a failure. It also reshapes Phase 1:
Gemini is a *browser* computer-use provider. Supporting it means a browser
surface that can report a current URL, which is a different backend contract
from the desktop backends this bundle has — closer to the Playwright territory
explicitly scoped OUT of this bundle.

### Live verb names differ from the documented set
Captured: `move_to`, `hover_at`, `scroll_document`, `open_web_browser`.
Research/docs had `move`, `scroll`. Verb mapping must come from captures.

## THE HEADLINE — multi-provider is a TWO-provider problem

Phase 0 collapsed the field:

    Anthropic  desktop  WORKS   (shipping)
    OpenAI     desktop  WORKS   (proven this phase, live desktop)
    Gemini     BROWSER-ONLY - structurally cannot drive a raw desktop
    Qwen       harness-only - no wire tool type at all

The plan was written expecting three desktop providers with three coordinate
spaces, three result envelopes, and 2.5 safety protocols - and concluded "no
lossless common schema, three adapters." **That premise is now wrong.**

The two providers that can actually drive a desktop are the CLOSEST PAIR:

| | Anthropic | OpenAI |
|---|---|---|
| coordinates | absolute px, declared dims | absolute px, image's own space |
| proven mapping | exact | exact (`match=True`, zero drift) |
| safety on the wire | none | none (key absent on GA) |
| batching | 1 action | `actions[]`, N per call |
| config | `display_*_px` required | **zero** config accepted |

The genuinely hard conflicts the plan feared - normalized 0-999 coordinates,
a tri-state safety protocol with `blocked`, browser/mobile verbs - all belong
to **Gemini**, which cannot serve this bundle's purpose at all.

What actually remains hard between Anthropic and OpenAI:
 1. **Cardinality.** OpenAI batches N actions per `call_id` and expects ONE
    screenshot for the batch. Anthropic is 1:1. `ActionBatch` is still required.
 2. **Who owns the first frame.** OpenAI ignored a supplied `input_image` and
    called `screenshot` itself. Anthropic accepts a handed-in image.
 3. **Different request envelopes.** Responses API vs Messages API - the
    screenshot-return rewrite has no shared shape.
 4. **Field overloading.** Anthropic's `text` means "the string to type" AND
    "the modifier to hold", depending on action.

This is a materially smaller and better-understood problem than the plan
assumed. Phase 1 should be designed for TWO desktop providers, with Gemini
reframed as a possible future BROWSER surface (different backend contract) and
Qwen as a prompt-schema backend if ever wanted.

## Not done
- Gemini desktop task (screen-dependent, end to end)
- Unknowns 4 and 6
- Phase 1 design + council iteration
- Phase 2 plans

## ~~BLOCKER 4~~ — RETRACTED. I tested the wrong path.

**The claim below was wrong and is kept only because the mistake is instructive.**

I concluded the SDK could not parse GA computer-use responses by calling
`ResponseComputerToolCall.model_validate(payload)` directly. That is the
**strict** path. The SDK's actual client path uses **lenient**
`construct_type()`, because `client._strict_response_validation` defaults to
`False`. Verified by my own hand:

```
client._strict_response_validation: False

1) STRICT   R.model_validate()      REJECTED: pending_safety_checks
2) LENIENT  construct_type()        PARSES. pending_safety_checks = None
                                    actions = [{"type":"move","x":426,"y":87,"keys":null}]
3) UNPATCHED live call              SUCCEEDED. computer_call items: 1
```

**An unpatched live call works today.** The OpenAI arm was never blocked by this.

What IS real: the SDK's type declares `pending_safety_checks` required while GA
omits it. That gap only bites under `OPENAI_STRICT_RESPONSE_VALIDATION=true`, or
if a future SDK flips the default. So the raw-response path is defensive
hardening, not a crash fix — and PR #58 should say exactly that rather than
claiming it prevents a failure you would hit today.

**Why I got it wrong is the same mistake this project keeps making:** I tested a
layer that was not the real path, then reported the result as if it were. It is
the identical shape to the chain test that verified `type` survived while never
checking what else rode along — and to the 393 green tests sitting on a wire
nobody had exercised. A sub-agent caught it by driving the live API instead of
replaying a fixture.

## Original claim, retained for the record — the installed OpenAI SDK predates the GA wire shape

Verified by me against the resolved dependency, not inferred:

```
installed SDK: 2.8.1
ResponseComputerToolCall required fields:
    ['id', 'action', 'call_id', 'pending_safety_checks', 'status', 'type']
has actions[] (GA batched)?      False
has action   (preview singular)?  True
```

Every one of the three things Phase 0 captured live is incompatible with what
this SDK will parse:

| Live GA wire (captured) | SDK 2.8.1 expects |
|---|---|
| `actions[]`, batched | no such field — wants `action`, singular |
| `pending_safety_checks` absent | **required**, no default |
| — | rejects the GA payload during its own pydantic parse |

The provider calls `client.responses.create(...)` directly, so the SDK
deserializes the HTTP response **before any provider code runs**. A correct
provider change cannot rescue this: the failure happens upstream of it.

So the OpenAI arm has four blockers, not three, and the fourth is in a
dependency rather than in code we can PR:

1. `NATIVE_TOOL_TYPES` lacks `computer` — addressed by PR #58
2. native passthrough only fires for dicts, not `ToolSpec` — PR #58
3. `role=="tool"` content stringified — PR #58
4. **SDK 2.8.1 cannot parse a GA computer_call response** — needs an SDK bump,
   and that bump must be verified against live traffic, not release notes

This is the same shape as the original `parameters` bug: a layer nobody checked,
failing at the wire, invisible to every test above it. It is also exactly what
the council warned about — "a passing suite sitting on a wire nobody exercised."

**Consequence for sequencing:** PR #58 is necessary but NOT sufficient. Until
blocker 4 is settled with a live end-to-end call, the OpenAI arm stays
unreachable, and the design's §16 contingency (Anthropic-only ships, OpenAI arm
vanishes without invalidating it) is the live path, not the fallback.
