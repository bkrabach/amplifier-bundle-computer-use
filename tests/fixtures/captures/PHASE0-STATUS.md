# Phase 0 — status

## Providers

| Provider | Verdict | Evidence |
|---|---|---|
| Anthropic | control, already shipping | live session drives real desktop |
| OpenAI | **PROVEN** | live API + live desktop, coordinate proof, 3 unknowns closed |
| Gemini | **PARTIAL** | live API + coordinate space PROVEN; desktop task not yet run |
| Qwen | **HARNESS ONLY - dropped** | verified by own fetch of DashScope reference |

## Unknowns

| # | Question | Status | Answer |
|---|---|---|---|
| 1 | OpenAI GA `computer` config fields? | **CLOSED** | **ZERO.** bare 200; `display_width`/`environment`/`display_width_px` all 400 `Unknown parameter` |
| 2 | OpenAI action object schema | **CLOSED** | `actions[]` (batched key). Real action: `{"type":"move","keys":null,"x":426,"y":87}` - `keys` slot always present, explicitly null |
| 3 | OpenAI `pending_safety_checks` still emitted? | **CLOSED** | **Key absent entirely** - not null, not `[]`. Preview-era vestigial, as suspected |
| 4 | Anthropic `scroll` / modifier field names | OPEN | not yet probed |
| 5 | Gemini coord range 0-999 vs 0-1000 | **CLOSED** | **NORMALIZED 0-999.** Decisive: `y=999` returned on a 720px-tall image - impossible in pixel space. Edge targets pin to exactly 999 |
| 6 | Gemini `scroll.magnitude_in_pixels` px or normalized? | OPEN | not yet probed |

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

## Not done
- Gemini desktop task (screen-dependent, end to end)
- Unknowns 4 and 6
- Phase 1 design + council iteration
- Phase 2 plans
