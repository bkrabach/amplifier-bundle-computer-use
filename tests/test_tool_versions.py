"""Unit tests for the model <-> tool_version coupling fix (`tool_versions.py`).

Pure functions, no backend, no network, no coordinator - these run anywhere,
including CI with no live Anthropic API access. Every case below traces
directly to the verified evidence table in the module docstring.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "modules" / "tool-computer-use"))

import pytest
from amplifier_module_tool_computer_use.tool_versions import (
    FALLBACK_TOOL_VERSION,
    ToolVersionError,
    beta_header_for,
    require_static_pairing,
    required_for_model,
    resolve_tool_version,
)

SONNET_4_5 = "claude-sonnet-4-5-20250929"
SONNET_5 = "claude-sonnet-5"
OPUS_5 = "claude-opus-5"


# -- required_for_model (the verified table) ---------------------------------


def test_required_for_model_exact_matches():
    assert required_for_model(SONNET_4_5) == "computer_20250124"
    assert required_for_model(SONNET_5) == "computer_20251124"
    assert required_for_model(OPUS_5) == "computer_20251124"


def test_required_for_model_prefix_matches_a_dated_release():
    # A hypothetical dated release of the sonnet-5/opus-5 generation should
    # still resolve via prefix match, without adding a new table entry.
    assert required_for_model("claude-sonnet-5-20260315") == "computer_20251124"
    assert required_for_model("claude-opus-5-20260401") == "computer_20251124"


def test_required_for_model_unknown_returns_none():
    assert required_for_model("claude-haiku-4-5") is None
    assert required_for_model("") is None
    assert required_for_model(None) is None  # type: ignore[arg-type]


# -- require_static_pairing (mount time - may raise) -------------------------


def test_mount_time_known_model_no_configured_auto_resolves():
    assert require_static_pairing(SONNET_4_5, None) == "computer_20250124"
    assert require_static_pairing(OPUS_5, None) == "computer_20251124"


def test_mount_time_known_model_matching_configured_is_fine():
    assert (
        require_static_pairing(SONNET_4_5, "computer_20250124") == "computer_20250124"
    )


def test_mount_time_known_model_conflicting_configured_raises_actionable_error():
    with pytest.raises(ToolVersionError) as excinfo:
        require_static_pairing(SONNET_4_5, "computer_20251124")
    msg = str(excinfo.value)
    assert "computer_20251124" in msg
    assert "computer_20250124" in msg
    assert SONNET_4_5 in msg


def test_mount_time_unknown_model_with_configured_trusts_it():
    # Cannot verify - refusing to operate on every unrecognised model would be
    # worse than trusting an explicit override.
    assert require_static_pairing("claude-haiku-9", "computer_20251124") == (
        "computer_20251124"
    )


def test_mount_time_unknown_model_with_no_configured_raises():
    with pytest.raises(ToolVersionError) as excinfo:
        require_static_pairing("claude-haiku-9", None)
    assert "claude-haiku-9" in str(excinfo.value)


def test_mount_time_nothing_configured_falls_back_to_default():
    # This is the "existing bundle config with neither tool_version nor model
    # set" case - must keep working exactly as before this fix (test_wire_format
    # and other pre-existing callers rely on this).
    assert require_static_pairing(None, None) == FALLBACK_TOOL_VERSION


def test_mount_time_only_configured_no_model_hint_trusts_configured():
    assert require_static_pairing(None, "computer_20241022") == "computer_20241022"


# -- resolve_tool_version (request time - never raises) ----------------------


def test_request_time_known_model_overrides_stale_configured():
    """The core reachable defect: a fallback model must correct a stale value,
    not keep emitting a pairing that will 400 forever."""
    resolved, corrected = resolve_tool_version(
        SONNET_4_5, "computer_20251124", previous="computer_20251124"
    )
    assert resolved == "computer_20250124"
    assert corrected is True


def test_request_time_known_model_matching_previous_is_not_flagged_corrected():
    resolved, corrected = resolve_tool_version(
        OPUS_5, None, previous="computer_20251124"
    )
    assert resolved == "computer_20251124"
    assert corrected is False


def test_request_time_unknown_model_keeps_previous():
    resolved, corrected = resolve_tool_version(
        "claude-haiku-9", None, previous="computer_20250124"
    )
    assert resolved == "computer_20250124"
    assert corrected is False


def test_request_time_unknown_model_with_configured_trusts_configured():
    resolved, corrected = resolve_tool_version("claude-haiku-9", "computer_20241022")
    assert resolved == "computer_20241022"
    assert corrected is False


def test_request_time_nothing_known_falls_back_to_default():
    resolved, corrected = resolve_tool_version(None, None)
    assert resolved == FALLBACK_TOOL_VERSION
    assert corrected is False


def test_request_time_never_raises_even_for_a_totally_unknown_model():
    # A raise here would take down the whole provider request mid-session -
    # the exact class of bug D3 already fixed once for native_tool_spec.
    resolved, _ = resolve_tool_version("some-brand-new-model-nobody-has-seen", None)
    assert resolved == FALLBACK_TOOL_VERSION


def test_request_time_simulates_the_reachable_fallback_scenario_end_to_end():
    """Session starts on opus-5 (-> 20251124), provider-anthropic falls back to
    sonnet-4-5 (-> 20250124) mid-session - tool_version must follow, every time,
    not just once."""
    version = require_static_pairing(OPUS_5, None)
    assert version == "computer_20251124"

    # Turn 2: provider fell back.
    version, corrected = resolve_tool_version(SONNET_4_5, None, previous=version)
    assert version == "computer_20250124"
    assert corrected is True

    # Turn 3: provider recovers to the original model.
    version, corrected = resolve_tool_version(OPUS_5, None, previous=version)
    assert version == "computer_20251124"
    assert corrected is True


# -- beta_header_for ---------------------------------------------------------


def test_beta_header_for_known_versions():
    assert beta_header_for("computer_20251124") == "computer-use-2025-11-24"
    assert beta_header_for("computer_20250124") == "computer-use-2025-01-24"
    assert beta_header_for("computer_20241022") == "computer-use-2024-10-22"


def test_beta_header_for_unknown_version_falls_back_safely():
    assert beta_header_for("computer_99999999") == beta_header_for(
        FALLBACK_TOOL_VERSION
    )
