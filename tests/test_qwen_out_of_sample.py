"""OUT-OF-SAMPLE extensibility probe: Qwen (Alibaba DashScope).

`docs/designs/multi-provider-design.md` Sec 18 states the extensibility claim and
then disowns the measurement behind it: Gemini was added at a cost of 0 files
outside `providers.py`, but the base had been improved KNOWING Gemini's shape.
Sec 18's own words - "That is fitting the base to the test case... It is not a
measurement" - and it names the missing test: a provider whose shape informed
nothing in the table.

Qwen is that provider. It was investigated and dropped in Phase 0 BEFORE
`providers.py` existed (`tests/fixtures/captures/qwen-verdict.md`), and it
diverges on an axis none of the three incumbents touch: **it has no wire tool
type at all**. Its declaration is a `computer_use` JSON schema pasted into the
SYSTEM PROMPT as text; its action arrives as `<tool_call>...</tool_call>` inside
`message.content` and must be regexed out.

This file is the instrument, not a feature. `providers.QWEN` is deliberately
NOT in `DIALECTS` - the tests below monkeypatch it in where they need it, the
same way `test_provider_dialects.py` already does with synthetic dialects, and
`test_qwen_is_not_in_the_shipped_table` pins that real dispatch is untouched.

VERDICT RECORDED HERE: FALSIFIED. Two of the seven `Dialect` fields cannot hold
Qwen's facts, and one of the two failures is not in the table at all - it is in
the CALL PATH that reaches the table. See the two `test_break_*` tests.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "modules" / "tool-computer-use"))

import pytest
from amplifier_module_tool_computer_use import providers
from amplifier_module_tool_computer_use.geometry import ImageSpace

#: The documented Qwen response body, transcribed from Alibaba's GUI-Plus doc
#: as summarised in `qwen-verdict.md`: the action is a `<tool_call>` block
#: embedded in assistant TEXT, and its coordinate is in the model's internal
#: resized-image space (the doc's own example returns [2530, 314] against a
#: 3008x1758 image while the prompt claims a 1000x1000 space).
QWEN_ASSISTANT_CONTENT = (
    "I'll click the search box.\n"
    '<tool_call>{"name": "computer_use", "arguments": '
    '{"action": "left_click", "coordinate": [2530, 314]}}</tool_call>'
)
QWEN_SOURCE_IMAGE = ImageSpace(3008, 1758)


# ---------------------------------------------------------------------------
# Guard: the instrument must not perturb what it measures
# ---------------------------------------------------------------------------


def test_qwen_is_not_in_the_shipped_table():
    """`QWEN` exists as a measuring instrument and must never reach real
    dispatch. If it were in `DIALECTS` it would claim every payload carrying a
    `content` string, and it would contribute a fictional tool type to
    `KNOWN_MODEL_TOOL_VERSIONS`."""
    assert providers.QWEN not in providers.DIALECTS
    assert providers.DIALECTS == (
        providers.ANTHROPIC,
        providers.OPENAI,
        providers.GEMINI,
    )


def test_the_three_shipped_dialects_are_bit_identical_with_the_probe_present():
    """Invariance, asserted over the aggregate views rather than by eye: the
    probe adds nothing to either table the rest of the module is assembled
    from, and steals no wire type from the dialects that drive a real
    desktop."""
    assert providers.model_tool_types() == {
        "claude-sonnet-4-5": "computer_20250124",
        "claude-sonnet-5": "computer_20251124",
        "claude-opus-5": "computer_20251124",
        "gpt-5.5": "computer",
        "gemini-2.5-computer-use": "computer_use",
    }
    assert providers.beta_headers() == {
        "computer_20251124": "computer-use-2025-11-24",
        "computer_20250124": "computer-use-2025-01-24",
        "computer_20241022": "computer-use-2024-10-22",
    }
    assert providers.dialect_for_tool_type("computer_20251124") is providers.ANTHROPIC
    assert providers.dialect_for_tool_type("computer") is providers.OPENAI
    assert providers.dialect_for_tool_type("computer_use") is providers.GEMINI
    assert providers.dialect_for_tool_type("qwen_computer_use") is providers.ANTHROPIC


# ---------------------------------------------------------------------------
# BREAK 1 - `Dialect.declare` has no codomain that can express Qwen
# ---------------------------------------------------------------------------


def test_break_1_declare_cannot_express_a_provider_with_no_tools_entry():
    """`declare` must return a dict, and every consumer puts that dict in the
    request's `tools[]` array. Qwen puts NOTHING in `tools[]` - DashScope
    documents `type` as "Currently, only function is supported" - and its real
    declaration is prose in the system prompt.

    There is no `Dialect` field for system-prompt text and no consumer that
    would carry one there, so the honest implementation raises. This asserts
    the raise, which IS the falsification: the seam holds this vendor's name
    but not its declaration."""
    with pytest.raises(providers.QwenNotDeclarableError, match="SYSTEM PROMPT"):
        providers.QWEN.declare(
            "qwen_computer_use", width=1280, height=720, enable_zoom=False
        )


def test_break_1_is_structural_not_an_unimplemented_stub():
    """The three incumbents all answer `declare` with a dict destined for
    `tools[]`; that destination is a property of the FIELD. Pinned here so the
    break above cannot be read as "someone just did not write it yet"."""
    for dialect in providers.DIALECTS:
        spec = dialect.declare(
            dialect.tool_types[0], width=1280, height=720, enable_zoom=False
        )
        assert isinstance(spec, dict) and spec, dialect.name


def test_break_1_dialect_has_no_field_that_could_carry_a_system_prompt():
    """Enumerated rather than asserted in prose. Expressing Qwen's declaration
    needs a NEW field here plus a NEW consumer reaching the system prompt -
    `ComputerTool` (modules/tool-computer-use/__init__.py) and the hook
    (modules/hook-computer-use/__init__.py), both OUTSIDE providers.py."""
    import dataclasses

    fields = {f.name for f in dataclasses.fields(providers.Dialect)}
    assert fields == {
        "name",
        "tool_types",
        "declare",
        "read_actions",
        "result_must_carry_screenshot",
        "models",
        "beta_headers",
    }


# ---------------------------------------------------------------------------
# BREAK 2 - `read_actions` works; nothing can ever call it
# ---------------------------------------------------------------------------


def test_what_fits_the_reader_translates_the_documented_payload(monkeypatch):
    """The half that DOES fit, proven end to end through `read_call` rather
    than by calling the reader directly.

    Two things work here, and both are the seam earning its keep: detection by
    payload SHAPE (never by provider identity), and the `image_space` second
    argument - added for Gemini's normalized grid - carrying enough to place a
    coordinate from a THIRD, unrelated coordinate space (`smart_resize`)."""
    monkeypatch.setattr(
        providers,
        "DIALECTS",
        (providers.OPENAI, providers.GEMINI, providers.QWEN, providers.ANTHROPIC),
        raising=True,
    )
    dialect, calls = providers.read_call(
        {"content": QWEN_ASSISTANT_CONTENT}, QWEN_SOURCE_IMAGE
    )
    assert dialect is providers.QWEN
    [(action, params)] = list(calls)
    assert action == "left_click"
    x, y = params["coordinate"]
    # Deliberately NOT asserting an exact target: the smart_resize constants
    # are transcribed, not verified against a live call (region-locked, no
    # key), and pinning a number here would dress inference up as evidence.
    # What IS assertable is the structural property - the mapped coordinate
    # lands inside the image the model was shown.
    assert 0 <= x <= QWEN_SOURCE_IMAGE.width
    assert 0 <= y <= QWEN_SOURCE_IMAGE.height


def test_what_fits_the_reader_refuses_to_guess_the_image_size(monkeypatch):
    """Same discipline as `_normalize_gemini_action`: handed no image space, a
    dialect whose coordinates are relative to one must name the missing fact,
    not invent a size. Lazy, so `execute()` turns it into an ordinary tool
    error rather than letting it escape `read_call`."""
    monkeypatch.setattr(
        providers, "DIALECTS", (providers.QWEN, providers.ANTHROPIC), raising=True
    )
    _, calls = providers.read_call({"content": QWEN_ASSISTANT_CONTENT})
    with pytest.raises(ValueError, match="smart_resize"):
        list(calls)


def test_break_2_the_reader_is_unreachable_because_qwen_emits_no_tool_call():
    """THE SECOND BREAK, and the one that cannot be fixed inside this file.

    `read_actions`' only reachable caller is `read_call`, whose only caller is
    `ComputerTool.execute`, which the orchestrator invokes ONLY for a parsed
    tool call:

        tool_calls = provider.parse_tool_calls(response)
        if not tool_calls:            # -> stream as final text, no dispatch
            ...
        (amplifier_module_loop_streaming/__init__.py:1785-1787)

    and `parse_tool_calls` returns `response.tool_calls`
    (amplifier_module_provider_openai/__init__.py:1425-1427). Qwen's action is
    text inside `message.content`, so `response.tool_calls` is empty and the
    tool is never invoked at all.

    That is asserted here the only way this repo honestly can: the payload the
    tool WOULD have to receive is not a tool-call payload, it is a shim shape
    (`{"content": <str>}`) with no producer anywhere in the request path.
    Something upstream must regex the text and synthesize a tool call, and that
    something is a provider module or the hook - both OUTSIDE providers.py, one
    of them outside this repo entirely.

    What this test CAN pin mechanically is the consequence in the shipped
    table: with `QWEN` absent (production), a Qwen-shaped payload is claimed by
    the ANTHROPIC catch-all and read as an empty action name."""
    dialect, calls = providers.read_call(
        {"content": QWEN_ASSISTANT_CONTENT}, QWEN_SOURCE_IMAGE
    )
    assert dialect is providers.ANTHROPIC
    [(action, _params)] = list(calls)
    assert action == ""


def test_break_2_the_catch_all_misread_at_least_fails_loud():
    """The empty action name above is not a silent success: `ACTIONS`
    membership rejects it, so a Qwen payload reaching the shipped table today
    produces a tool error rather than a wrong click. Worth pinning - the
    failure mode of the falsified case is 'loud and confusing', not
    'quiet and wrong'."""
    from amplifier_module_tool_computer_use import ACTIONS

    assert "" not in ACTIONS
