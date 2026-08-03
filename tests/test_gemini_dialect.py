"""Falsification test for `providers.Dialect`: can the two-vendor dispatch
table absorb a THIRD, genuinely divergent vendor without being restructured?

Gemini was chosen because it disagrees with both incumbents on every axis the
table was extracted from - no wire `type`, normalized coordinates instead of
pixels, verb-per-function instead of an `action`/`type` field. Everything
asserted here is transcribed from live traffic captured 2026-08-03 against
`gemini-2.5-computer-use-preview-10-2025` (`tests/fixtures/captures/gemini-*.json`).

Three groups of tests, and the third is the point:

  * WHAT FIT     - declaration, dispatch, verb translation: absorbed as one
                   `Dialect` record, no caller change.
  * WHAT STRAINED- normalized coordinates: needed a new `Dialect` field AND an
                   edit to `ComputerTool.execute`, because the conversion needs
                   the display and `read_actions` is not given it.
  * WHAT DID NOT FIT - `test_seam_gap_*`. These do NOT assert desired
                   behaviour. They pin the WRONG answers the surrounding code
                   gives a Gemini session today, so the gaps are measured facts
                   in the suite rather than prose in a report. Read their
                   docstrings before "fixing" any of them.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "modules" / "tool-computer-use"))
sys.path.insert(0, str(ROOT / "modules" / "hook-computer-use"))

import pytest
from amplifier_module_tool_computer_use import providers
from amplifier_module_tool_computer_use.geometry import Display
from amplifier_module_tool_computer_use.tool_versions import (
    KNOWN_MODEL_TOOL_VERSIONS,
    beta_header_for,
)

CAPTURES = ROOT / "tests" / "fixtures" / "captures"


def _capture(name: str) -> dict:
    return json.loads((CAPTURES / name).read_text(encoding="utf-8"))


# ===========================================================================
# WHAT FIT: absorbed as one `Dialect` record, nothing outside `providers.py`
# ===========================================================================


def test_declaration_has_no_type_key_at_all():
    """`{"type": "computer_use", ...}` -> 400 `Unknown name "type" at
    'tools[0]'`. The vendor key IS the discriminator. Both incumbent dialects
    put `type` at the top level; this one must not."""
    spec = providers.GEMINI.declare(
        "computer_use", width=1280, height=720, enable_zoom=True
    )
    assert spec == {"computer_use": {"environment": "ENVIRONMENT_DESKTOP"}}
    assert "type" not in spec


def test_gemini_owns_its_key_and_the_incumbents_are_untouched():
    assert providers.dialect_for_tool_type("computer_use") is providers.GEMINI
    assert providers.dialect_for_tool_type("computer") is providers.OPENAI
    assert providers.dialect_for_tool_type("computer_20251124") is providers.ANTHROPIC


def test_function_call_shape_is_read_as_gemini():
    """The verb is the FUNCTION NAME, not a field - detection is by shape, the
    same way `_read_openai_actions` sniffs its batch."""
    fn = _capture("gemini-response-1.json")["candidates"][0]["content"]["parts"][0][
        "functionCall"
    ]
    dialect, calls = providers.read_call(fn)
    assert dialect is providers.GEMINI
    assert list(calls) == [("mouse_move", {"coordinate": [311.0, 84.0]})]


def test_hover_at_is_also_a_move():
    dialect, calls = providers.read_call({"name": "hover_at", "args": {"x": 1, "y": 2}})
    assert dialect is providers.GEMINI
    assert list(calls) == [("mouse_move", {"coordinate": [1.0, 2.0]})]


@pytest.mark.parametrize(
    "payload",
    [
        {"action": "left_click", "coordinate": [1, 2]},  # anthropic
        {"actions": [{"type": "screenshot"}]},  # openai
        {"name": "move_to"},  # no args mapping
        {"args": {"x": 1, "y": 2}},  # no verb
    ],
)
def test_gemini_declines_every_shape_that_is_not_its_own(payload):
    """Adding a third dialect must not steal payloads from the first two."""
    assert providers._read_gemini_actions(payload) is None


def test_incumbent_dispatch_is_unchanged_by_the_third_dialect():
    anth, anth_calls = providers.read_call(
        {"action": "left_click", "coordinate": [1, 2]}
    )
    assert anth is providers.ANTHROPIC
    assert list(anth_calls) == [
        ("left_click", {"action": "left_click", "coordinate": [1, 2]})
    ]
    oai, oai_calls = providers.read_call({"actions": [{"type": "screenshot"}]})
    assert oai is providers.OPENAI
    assert list(oai_calls) == [("screenshot", {})]


def test_verified_model_prefix_resolves_to_gemini_key():
    """`gemini-response-1.json` reports modelVersion
    `gemini-2.5-computer-use-preview-10-2025`; the undated prefix must cover
    the dated id, per the Anthropic precedent."""
    assert KNOWN_MODEL_TOOL_VERSIONS["gemini-2.5-computer-use"] == "computer_use"
    from amplifier_module_tool_computer_use.tool_versions import required_for_model

    assert (
        required_for_model("gemini-2.5-computer-use-preview-10-2025") == "computer_use"
    )


# ===========================================================================
# WHAT STRAINED: normalized coordinates - a new field AND a caller edit
# ===========================================================================


def test_coordinates_are_normalized_not_pixels():
    """`gemini-unknown5.json`: probing far-right/bottom-right of a 1280x720
    image returned x=999 and y=999. y=999 is impossible in pixel space on a
    720px-tall image, so the space is a fixed 0..999 grid."""
    cap = _capture("gemini-unknown5.json")
    _, image_h = cap["image_space"]
    assert cap["normalized_cap"] == providers._GEMINI_COORD_MAX == 999
    assert max(p[3] for p in cap["probes"]) == 999
    assert image_h - 1 < 999  # 719 < 999: cannot be a pixel coordinate


def test_normalized_conversion_reproduces_the_measured_cursor_exactly():
    """THE GROUND-TRUTH TEST. `gemini-coordproof.json` recorded where the real
    cursor actually landed for a known raw pair. Full pipeline - `read_call`
    -> `to_model_space` -> `Display.to_screen` - must reproduce it exactly."""
    cap = _capture("gemini-coordproof.json")
    mw, mh = cap["image_space"]
    sw, sh = cap["monitor"]
    disp = Display(screen_width=sw, screen_height=sh, model_width=mw, model_height=mh)

    dialect, calls = providers.read_call(cap["function_call"])
    ((action, params),) = list(calls)
    params = dialect.to_model_space(action, params, disp.model_width, disp.model_height)
    x, y = params["coordinate"]

    assert list(disp.to_screen(x, y)) == cap["real_cursor_after_normalized_move"]
    assert list(disp.to_screen(x, y)) == [1194, 181]


def test_the_pixel_interpretation_is_wrong_by_hundreds_of_pixels():
    """What the tool did BEFORE `to_model_space` existed, and what it would
    silently keep doing if a Gemini payload were fed through unconverted:
    (933, 252) instead of (1194, 181). 261px off in x, 71px in y - a click on
    the wrong thing, with no error anywhere."""
    cap = _capture("gemini-coordproof.json")
    mw, mh = cap["image_space"]
    sw, sh = cap["monitor"]
    disp = Display(screen_width=sw, screen_height=sh, model_width=mw, model_height=mh)
    raw = cap["raw"]

    unconverted = disp.to_screen(float(raw["x"]), float(raw["y"]))
    assert list(unconverted) == cap["as_pixels"] == [933, 252]
    assert list(unconverted) != cap["real_cursor_after_normalized_move"]


def test_the_grid_divisor_is_1000_not_999():
    """A 0..999 grid over N pixels is N/1000 per cell. Dividing by 999 lands
    999 one past the edge and misses the captured cursor by a pixel."""
    cap = _capture("gemini-coordproof.json")
    mw, mh = cap["image_space"]
    sw, sh = cap["monitor"]
    disp = Display(screen_width=sw, screen_height=sh, model_width=mw, model_height=mh)
    raw = cap["raw"]

    by_999 = disp.to_screen(raw["x"] / 999 * mw, raw["y"] / 999 * mh)
    assert list(by_999) != cap["real_cursor_after_normalized_move"]
    assert providers._GEMINI_COORD_SPAN == 1000


def test_model_space_stays_float_because_rounding_early_loses_a_pixel():
    """`_gemini_to_model_space` must NOT round. y=84 is 60.48 model px, which
    scales to 181 screen px (measured truth); a pre-rounded 60 scales to 180."""
    cap = _capture("gemini-coordproof.json")
    mw, mh = cap["image_space"]
    sw, sh = cap["monitor"]
    disp = Display(screen_width=sw, screen_height=sh, model_width=mw, model_height=mh)

    params = providers.GEMINI.to_model_space(
        "mouse_move", {"coordinate": [311.0, 84.0]}, mw, mh
    )
    x, y = params["coordinate"]
    assert isinstance(x, float) and isinstance(y, float)
    assert disp.to_screen(x, y)[1] == 181
    assert disp.to_screen(round(x), round(y))[1] == 180  # what rounding early costs


def test_to_model_space_is_identity_for_both_incumbents():
    """The new field must not perturb the two live paths. Same object back."""
    params = {"coordinate": [426, 87], "scroll_amount": 3}
    for dialect in (providers.ANTHROPIC, providers.OPENAI):
        assert dialect.to_model_space("scroll", params, 1280, 720) is params


# ===========================================================================
# WHAT DID NOT FIT
# ===========================================================================


def test_scroll_magnitude_cannot_be_absorbed_and_says_so():
    """`gemini-unknown6.json`: `magnitude: 999` against a 720px-tall image -
    a NORMALIZED DISTANCE. This tool's `scroll_amount` (and `Backend.scroll`'s
    `amount`) is a WHEEL NOTCH COUNT. `Call = tuple[str, dict]` carries no
    units, and no capture pins a pixels-per-notch value, so there is neither a
    conversion nor anywhere to defer one to. Fails loud rather than guessing."""
    cap = _capture("gemini-unknown6.json")
    call = cap["calls"][0]
    assert call["args"]["magnitude"] == 999
    assert call["args"]["magnitude"] > cap["image_h"]  # 999 > 720: not pixels

    dialect, calls = providers.read_call(call)
    assert dialect is providers.GEMINI
    with pytest.raises(ValueError, match="wheel-notch count"):
        list(calls)


def test_open_web_browser_has_no_desktop_equivalent_and_says_so():
    """`gemini-task-turn0.json`: shown a Windows desktop, the model called
    `open_web_browser`. There is no such action in this tool's vocabulary."""
    fn = _capture("gemini-task-turn0.json")["candidates"][0]["content"]["parts"][0][
        "functionCall"
    ]
    assert fn["name"] == "open_web_browser"
    _, calls = providers.read_call(fn)
    with pytest.raises(ValueError, match="no desktop equivalent"):
        list(calls)


def test_uncaptured_verbs_fail_loud_rather_than_being_inferred_from_docs():
    """Google's published list names actions this traffic never produced
    (`click_at`, `type_text_at`, ...). Encoding them would be inference, which
    is exactly what the `models` tables forbid."""
    _, calls = providers.read_call({"name": "click_at", "args": {"x": 1, "y": 2}})
    with pytest.raises(ValueError, match="unsupported Gemini computer-use action"):
        list(calls)


def test_seam_gap_beta_header_for_gemini_returns_anthropics_header():
    """NOT desired behaviour - a MEASURED GAP, pinned so it cannot hide.

    `GEMINI.beta_headers` is empty, but `tool_versions.beta_header_for` falls
    back to `BETA_HEADER_FOR_VERSION[FALLBACK_TOOL_VERSION]` for any type it
    does not know, so a Gemini session is handed Anthropic's
    `computer-use-2025-11-24`. The `Dialect` record cannot express "this vendor
    has no such concept" distinctly from "unknown type" - `beta_headers={}`
    and an absent entry are the same thing to that lookup. Closing this means
    editing `tool_versions.py` (and its test), outside `providers.py`."""
    assert beta_header_for("computer_use") == "computer-use-2025-11-24"


def test_seam_gap_hook_resolves_the_wrong_tool_type_for_gemini():
    """NOT desired behaviour - a MEASURED GAP, pinned so it cannot hide.

    `hook-computer-use._resolve_native_tool_type` reads the mounted tool's
    `native_tool_spec` and takes `native.get("type")`. Gemini's declaration has
    no `type`, so the hook silently falls back to `_DEFAULT_PROBE_TOOL_TYPE`
    and probes the mounted provider for ANTHROPIC's wire convention. Closing
    this means editing `hook-computer-use`, which declares `dependencies = []`
    and so cannot import this table at all."""
    import amplifier_module_hook_computer_use as hook_mod

    class _GeminiToolStub:
        @property
        def native_tool_spec(self) -> dict:
            return providers.GEMINI.declare(
                "computer_use", width=1280, height=720, enable_zoom=False
            )

    class _Coordinator:
        def get(self, kind, name=None):
            return _GeminiToolStub() if (kind, name) == ("tools", "computer") else None

    resolved = hook_mod._resolve_native_tool_type(_Coordinator())
    assert resolved == "computer_20251124"  # Anthropic's, for a Gemini session
    assert resolved != "computer_use"
