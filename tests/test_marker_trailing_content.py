"""Regression test for the root cause behind "screenshot marker never expands
into an image block" - reproduced on Aug 1 2026 against a live session.

Root cause (proven with a live trace, not inferred): the kernel's
`HookResult.append_to_last_tool_result` mechanism (see HOOKS_API.md -
"Injection placement control") lets OTHER hooks - `hooks-status-context`,
`hooks-todo-reminder`, `mode-status`, `hooks-skills-visibility`, etc. - append
their own text directly onto the tail of the SAME tool-result content string
`hook-computer-use` is trying to parse as JSON. This happens routinely on the
very first tool call of a session (exactly when a screenshot is most often
taken), turning the tool result content into:

    '{"error": null, "output": "{...marker...}", "success": true}\\n\\n<system-reminder ...>'

`json.loads` requires the ENTIRE string to parse as one JSON value and raises
`JSONDecodeError: Extra data` the moment anything trails the closing brace.
`_parse_marker` caught that error and returned `None` - correctly avoiding a
crash, but silently and indistinguishably from "this message has no marker at
all". The screenshot capture always succeeded; only the hook's expansion
silently no-op'd, on ANY backend (local or remote) - this was never a
backend-specific bug, only observed more often on whichever backend happened
to have a screenshot land on session-start reminder timing.

Fix: `_parse_marker` now uses `_loads_leading`, which parses the JSON value at
the START of the string via `json.JSONDecoder().raw_decode()` and ignores
anything appended after it - trailing text from any other hook no longer
breaks marker detection.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "modules" / "tool-computer-use"))
sys.path.insert(0, str(ROOT / "modules" / "hook-computer-use"))

import amplifier_module_hook_computer_use as hook_mod
import amplifier_module_tool_computer_use as tool_mod

#: The exact real-world envelope shape captured from a live session's
#: transcript.jsonl, byte-for-byte (only the shot path/dimensions vary).
_MARKER_JSON = json.dumps(
    {
        tool_mod.MARKER: 1,
        "text": "screenshot captured (1280x826)",
        "images": ["/home/bkrabach/.amplifier/computer-use/shots/deadbeef.png"],
    }
)
_ENVELOPE = json.dumps({"error": None, "output": _MARKER_JSON, "success": True})

#: A representative sample of what `append_to_last_tool_result` actually glues
#: on - trimmed but structurally identical to the real reminder text observed.
_TRAILING_REMINDER = (
    '\n\n<system-reminder source="hooks-status-context">\n'
    "Here is useful information about the environment you are running in:\n"
    "<env>\nWorking directory: /home/bkrabach/dev/computer-use-improvements\n"
    "</env>\n</system-reminder>"
)


def test_parse_marker_tolerates_trailing_appended_text():
    """The exact production shape: envelope JSON immediately followed by a
    reminder some OTHER hook appended to the same tool-result content string."""
    content = _ENVELOPE + _TRAILING_REMINDER

    result = hook_mod._parse_marker(content)

    assert result is not None, (
        "marker must still be detected when another hook appends trailing "
        "text to the same tool-result content string"
    )
    assert result[tool_mod.MARKER] == 1
    assert result["images"] == [
        "/home/bkrabach/.amplifier/computer-use/shots/deadbeef.png"
    ]


def test_parse_marker_tolerates_trailing_text_on_bare_marker():
    """Trailing text appended directly after the bare (non-enveloped) marker
    JSON - the shape used when no orchestrator envelope wraps the output."""
    content = _MARKER_JSON + _TRAILING_REMINDER

    result = hook_mod._parse_marker(content)

    assert result is not None
    assert result[tool_mod.MARKER] == 1


def test_parse_marker_still_returns_none_for_non_marker_trailing_text():
    """Regression guard: unrelated JSON with trailing text and NO marker must
    still return None - this fix must not make marker detection over-eager."""
    content = json.dumps({"foo": "bar"}) + _TRAILING_REMINDER

    assert hook_mod._parse_marker(content) is None


def test_parse_marker_still_rejects_genuinely_malformed_json():
    """Regression guard: content that merely CONTAINS the marker substring
    (e.g. inside a longer non-JSON string) but isn't valid JSON at all must
    still safely return None, not raise."""
    content = "not json at all but mentions __amplifier_computer_use__ anyway"

    assert hook_mod._parse_marker(content) is None


def test_expand_tool_results_inlines_image_despite_trailing_reminder(tmp_path):
    """End-to-end: `_expand_tool_results` must still produce a real image block
    when the tool-result message carries a marker with trailing appended text -
    exactly the message shape that reached the provider in the live repro."""
    shot = tool_mod.SHOT_DIR / "trailing-content-test.png"
    shot.parent.mkdir(parents=True, exist_ok=True)
    png_1px = bytes.fromhex(
        "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
        "0000000d4944415478da6360000002000155020ff31af0e00000000049454e44ae426082"
    )
    shot.write_bytes(png_1px)
    try:
        marker = json.dumps(
            {tool_mod.MARKER: 1, "text": "screenshot captured", "images": [str(shot)]}
        )
        envelope = json.dumps({"error": None, "output": marker, "success": True})
        content = envelope + _TRAILING_REMINDER

        out = hook_mod._expand_tool_results(
            [{"role": "tool", "content": content}], max_inline=3
        )

        result_content = hook_mod._read_content(out[0])
        assert isinstance(result_content, list), (
            f"expected a real image content block list, got: {result_content!r}"
        )
        assert any(b.get("type") == "image" for b in result_content)
    finally:
        shot.unlink(missing_ok=True)
