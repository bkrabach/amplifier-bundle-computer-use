# Qwen — Phase 0 determination: HARNESS ONLY (verified)

Verdict reached from primary sources and confirmed by my own fetch of
`https://www.alibabacloud.com/help/en/model-studio/qwen-api-via-dashscope`:

    "type": "function"   x6 occurrences
    computer-use tool types (computer_2025*, computer_use, computer-use): 0

Alibaba's four inference surfaces all say function-only:
 - DashScope native: `type` -> "Currently, only function is supported."
 - OpenAI-compat Responses: built-in types enumerated, no `computer`
 - Anthropic-compat Messages: tools have only name/description/input_schema,
   NO `type` field at all
 - OpenAI-compat Chat Completions: GUI quickstart calls it with no `tools` param

The decisive artifact is Alibaba's own GUI-Plus doc: it instructs you to paste a
`computer_use` JSON schema INTO THE SYSTEM PROMPT as text, then
`re.findall(r'<tool_call>(.*?)</tool_call>')` the action out of
`message.content`. There is no server-side tool contract, so there is nothing
for the API to mis-declare or reject. That is a harness by definition.

Additional blockers even if we wanted it:
 - `gui-plus` is China (Beijing) region ONLY, "Chinese mainland deployment scope"
 - API keys are region-scoped; an international key cannot reach it
 - Coordinates land in the model's internal resized-image space (doc's own
   example returns [2530, 314] against a 3008x1758 image despite the prompt
   claiming 1000x1000) - requires a `smart_resize` helper to map back

DECISION: dropped from Phase 0/1/2 per the goal's instruction. If Qwen is ever
added it slots in as another prompt-schema backend behind the existing seam,
not as a second native path alongside Anthropic.

## Unverified (stated plainly)
- No live API call - no DashScope key, and it is region-locked regardless.
- Whether the Anthropic-compat shim SILENTLY DROPS an unknown tool `type` vs
  400s on it. This matters: silent-drop would make a naive "point the bundle at
  Qwen" fail quietly - the same silent-degradation shape as the provider
  stream() risk. Only a live Beijing-region call settles it.
