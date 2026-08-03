"""Unit tests for `providers.py` - the one place that knows how each vendor
declares the native `computer` tool and how each one shapes an action.

These lock the four things that genuinely differ between the two dialects, as
captured from live traffic (`tests/fixtures/captures/`), plus the two
invariants that keep the seam from becoming a place a silent downgrade can
hide:

  * a dialect's declaration is exactly what that wire accepts - Anthropic's
    REQUIRES the display size, OpenAI's rejects every field but `type`;
  * dispatch never depends on where a dialect happens to sit in `DIALECTS`.

`_normalize_openai_action` itself has its own suite
(`test_openai_action_normalization.py`).
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "modules" / "tool-computer-use"))

import pytest
from amplifier_module_tool_computer_use import providers
from amplifier_module_tool_computer_use.tool_versions import (
    BETA_HEADER_FOR_VERSION,
    KNOWN_MODEL_TOOL_VERSIONS,
)

# -- dialect selection by wire type -----------------------------------------


@pytest.mark.parametrize(
    "tool_type",
    ["computer_20251124", "computer_20250124", "computer_20241022"],
)
def test_dated_types_belong_to_anthropic(tool_type):
    assert providers.dialect_for_tool_type(tool_type) is providers.ANTHROPIC


def test_bare_type_belongs_to_openai():
    assert providers.dialect_for_tool_type("computer") is providers.OPENAI


def test_unknown_type_falls_back_to_the_versioned_family():
    """A brand-new, explicitly configured Anthropic type this build predates
    must still get the display size Anthropic requires - guessing OpenAI's
    bare form for it would strip a required field and 400 every request."""
    assert providers.dialect_for_tool_type("computer_29999999") is providers.ANTHROPIC
    assert providers.DEFAULT_DIALECT is providers.ANTHROPIC


# -- declaration: the two shapes that actually reach the wire ---------------


def test_anthropic_declaration_carries_the_required_display_size():
    spec = providers.ANTHROPIC.declare(
        "computer_20251124", width=1280, height=720, enable_zoom=True
    )
    assert spec == {
        "type": "computer_20251124",
        "name": "computer",
        "display_width_px": 1280,
        "display_height_px": 720,
        "enable_zoom": True,
    }


def test_anthropic_zoom_only_on_generations_that_have_it():
    spec = providers.ANTHROPIC.declare(
        "computer_20250124", width=1280, height=720, enable_zoom=True
    )
    assert "enable_zoom" not in spec


def test_anthropic_zoom_off_when_disabled():
    spec = providers.ANTHROPIC.declare(
        "computer_20251124", width=1280, height=720, enable_zoom=False
    )
    assert "enable_zoom" not in spec


def test_openai_declaration_is_bare():
    """Live Responses API traffic: `{"type": "computer"}` -> 200; any other
    declaration field (`display_width_px`, `display_height_px`,
    `display_width`, `environment`) -> 400 "Unknown parameter". The size and
    zoom flag are accepted by this call and discarded by construction."""
    assert providers.OPENAI.declare(
        "computer", width=1280, height=720, enable_zoom=True
    ) == {"type": "computer"}


# -- reading a call: shape decides, never provider identity -----------------


def test_anthropic_call_is_one_action_carrying_its_own_params():
    dialect, calls = providers.read_call(
        {
            "action": "scroll",
            "coordinate": [640, 360],
            "scroll_direction": "down",
            "scroll_amount": 3,
        }
    )
    assert dialect is providers.ANTHROPIC
    [(action, params)] = list(calls)
    assert action == "scroll"
    assert params["coordinate"] == [640, 360]
    assert params["scroll_amount"] == 3


def test_openai_call_is_a_batch_under_actions():
    dialect, calls = providers.read_call(
        {"actions": [{"type": "move", "keys": None, "x": 426, "y": 87}]}
    )
    assert dialect is providers.OPENAI
    assert list(calls) == [("mouse_move", {"coordinate": [426.0, 87.0]})]


def test_openai_batch_of_many_stays_one_call():
    _, calls = providers.read_call({"actions": [{"type": "wait"}] * 15})
    assert list(calls) == [("wait", {})] * 15


def test_openai_empty_batch_reads_as_no_actions():
    dialect, calls = providers.read_call({"actions": []})
    assert dialect is providers.OPENAI
    assert list(calls) == []


def test_actions_that_is_not_a_list_is_not_an_openai_call():
    """The `actions` LIST is the wire signature, not the key name."""
    dialect, calls = providers.read_call({"actions": "nope", "action": "screenshot"})
    assert dialect is providers.ANTHROPIC
    assert list(calls)[0][0] == "screenshot"


def test_openai_batch_is_read_lazily_so_partial_execution_is_preserved():
    """A malformed entry must not be discovered until the good entries before
    it have already been pulled (and, in `ComputerTool.execute`, run).
    Normalizing eagerly would silently make batches atomic - a real behaviour
    change dressed up as a refactor."""
    _, calls = providers.read_call(
        {
            "actions": [
                {"type": "click", "button": "left", "x": 3, "y": 4},
                {"type": "some_future_action"},
            ]
        }
    )
    it = iter(calls)
    assert next(it) == ("left_click", {"coordinate": [3.0, 4.0]})
    with pytest.raises(ValueError, match="unsupported OpenAI computer-use action type"):
        next(it)


def test_openai_batch_rejects_a_non_action_entry():
    _, calls = providers.read_call({"actions": [{"type": "wait"}, "not-an-action"]})
    it = iter(calls)
    assert next(it) == ("wait", {})
    with pytest.raises(ValueError, match="unsupported action entry"):
        next(it)


# -- the invariants that keep the seam honest -------------------------------


def test_dispatch_does_not_depend_on_position_in_the_table(monkeypatch):
    """`DIALECTS` order is observable in `KNOWN_MODEL_TOOL_VERSIONS`, so it is
    free to change for reasons that have nothing to do with dispatch. Reading
    a call must survive that: the catch-all is tried explicitly last, never
    "last in the tuple"."""
    monkeypatch.setattr(
        providers, "DIALECTS", (providers.OPENAI, providers.ANTHROPIC), raising=True
    )
    dialect, _ = providers.read_call({"actions": [{"type": "wait"}]})
    assert dialect is providers.OPENAI
    dialect, _ = providers.read_call({"action": "screenshot"})
    assert dialect is providers.ANTHROPIC

    monkeypatch.setattr(
        providers, "DIALECTS", (providers.ANTHROPIC, providers.OPENAI), raising=True
    )
    dialect, _ = providers.read_call({"actions": [{"type": "wait"}]})
    assert dialect is providers.OPENAI


def test_only_openai_requires_a_screenshot_in_every_result():
    """OpenAI's `computer_call_output` is invalid without an image; Anthropic
    is happy with a text-only result. This is the ONLY thing a batch needs
    beyond "a list, sometimes of length one" - which is why there is no named
    ActionBatch type."""
    assert providers.OPENAI.result_must_carry_screenshot is True
    assert providers.ANTHROPIC.result_must_carry_screenshot is False


def test_every_dialects_tool_types_are_disjoint():
    """Two dialects claiming one wire type would make `dialect_for_tool_type`
    order-dependent, and a declaration could silently come from the wrong
    vendor - the exact silent-downgrade shape this seam exists to prevent."""
    seen: set[str] = set()
    for dialect in providers.DIALECTS:
        assert not (seen & set(dialect.tool_types)), dialect.name
        seen |= set(dialect.tool_types)


def test_every_verified_model_maps_to_a_type_its_own_dialect_owns():
    """A row in a dialect's `models` table that pointed at another dialect's
    wire type would declare the wrong shape for that model on every request."""
    for dialect in providers.DIALECTS:
        for model, tool_type in dialect.models.items():
            assert tool_type in dialect.tool_types, (dialect.name, model)


def test_tool_versions_tables_are_assembled_from_the_dialects():
    """`tool_versions` holds resolution POLICY; the vendor rows live with the
    vendor. If these drift apart, one of them is a second source of truth."""
    assert providers.model_tool_types() == KNOWN_MODEL_TOOL_VERSIONS
    assert providers.beta_headers() == BETA_HEADER_FOR_VERSION
    assert KNOWN_MODEL_TOOL_VERSIONS["gpt-5.5"] == "computer"
    assert KNOWN_MODEL_TOOL_VERSIONS["claude-opus-5"] == "computer_20251124"
    # OpenAI has no beta-header opt-in for this tool, and must not acquire one
    # by inheriting Anthropic's.
    assert "computer" not in BETA_HEADER_FOR_VERSION
