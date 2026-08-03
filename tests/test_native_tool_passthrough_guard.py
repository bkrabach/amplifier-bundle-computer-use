"""Unit tests for the fail-closed guard against silently-downgraded native tools.

hook-computer-use used to promote `computer`'s `native_tool_spec` to the wire itself
and inject the matching `anthropic-beta` header ("job 1"). That is now redundant and
has been removed: amplifier-module-loop-streaming PR #36 (commit f8004e0) preserves a
tool's `native_tool_spec` through its own `ToolSpec` construction, and
amplifier-module-provider-anthropic PR #79 (commit 94a4354) derives the required beta
header itself.

If EITHER upstream module predates its fix, `computer`'s native definition silently
degrades to a plain function tool - the request is still valid, the tool still
appears, and the model just gets the weaker definition, with no error and no log
line. These tests prove the fix: `_provider_derives_native_tool_betas`,
`_orchestrator_preserves_native_tool_spec`, and
`_fail_if_native_tool_passthrough_unsupported` detect that condition by driving the
real, installed code with throwaway probes - not by trusting a version string - and
refuse loudly instead of degrading.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "modules" / "hook-computer-use"))

import amplifier_module_hook_computer_use as hook_mod
import pytest

# ---------------------------------------------------------------------------
# Provider-side fakes (amplifier-module-provider-anthropic PR #79)
# ---------------------------------------------------------------------------


class _ProviderWithWorkingBetaDerivation:
    """Today's shape: has a working `_derive_native_tool_betas()`."""

    __module__ = "amplifier_module_provider_anthropic"

    async def complete(self, request, **kwargs):
        return "ok"

    def _derive_native_tool_betas(self, tools):
        mapping = {"computer_20251124": "computer-use-2025-11-24"}
        return [mapping[t["type"]] for t in tools if t.get("type") in mapping]


class _ProviderPredatingPR79:
    """Pre-PR-#79 shape: no `_derive_native_tool_betas()` at all."""

    __module__ = "amplifier_module_provider_anthropic"

    async def complete(self, request, **kwargs):
        return "ok"


class _ProviderWithBrokenBetaDerivation:
    """Has the method, but it does not actually derive anything - a broken or
    downgraded implementation, not merely an absent one."""

    __module__ = "amplifier_module_provider_anthropic"

    async def complete(self, request, **kwargs):
        return "ok"

    def _derive_native_tool_betas(self, tools):
        return []


def test_provider_derives_native_tool_betas_true_for_working_provider():
    assert (
        hook_mod._provider_derives_native_tool_betas(
            _ProviderWithWorkingBetaDerivation()
        )
        is True
    )


def test_provider_derives_native_tool_betas_false_when_method_absent():
    """No middle ground here: this probe only ever runs on a provider already
    confirmed to be Anthropic (see `_is_anthropic`), so an absent method IS the
    pre-PR-#79 shape, not an ambiguous signal."""
    assert (
        hook_mod._provider_derives_native_tool_betas(_ProviderPredatingPR79()) is False
    )


def test_provider_derives_native_tool_betas_false_when_broken():
    assert (
        hook_mod._provider_derives_native_tool_betas(
            _ProviderWithBrokenBetaDerivation()
        )
        is False
    )


# ---------------------------------------------------------------------------
# Orchestrator-side fakes (amplifier-module-loop-streaming PR #36)
# ---------------------------------------------------------------------------


class _ToolSpecLike:
    """Minimal stand-in for `amplifier_core.message_models.ToolSpec` - just
    enough for `.model_dump(exclude_none=True)` to report back whatever fields
    a real `ToolSpec` (which is `extra="allow"`) would carry through."""

    def __init__(self, **fields: object) -> None:
        self._fields = fields

    def model_dump(self, exclude_none: bool = True) -> dict[str, object]:
        if exclude_none:
            return {k: v for k, v in self._fields.items() if v is not None}
        return dict(self._fields)


def _new_build_tool_spec(tool):
    """Mimics the FIXED `_build_tool_spec` (PR #36, commit f8004e0): preserves
    `native_tool_spec` fields as `ToolSpec` extras."""
    native = getattr(tool, "native_tool_spec", None)
    if isinstance(native, dict) and native.get("type"):
        return _ToolSpecLike(**native)
    return _ToolSpecLike(name=tool.name)


def _old_build_tool_spec(tool):
    """Mimics the PRE-PR-#36 behaviour: only name/description/parameters,
    silently dropping `native_tool_spec` entirely."""
    return _ToolSpecLike(name=tool.name)


def _register_fake_orchestrator_module(module_name: str, build_tool_spec=None):
    """Register a throwaway module in `sys.modules` under `module_name`, standing
    in for a real loop-streaming install, so `_orchestrator_preserves_native_tool_spec`
    (which looks the function up via `sys.modules[type(orchestrator).__module__]`)
    finds exactly the `_build_tool_spec` shape a given test wants to simulate."""
    module = types.ModuleType(module_name)
    if build_tool_spec is not None:
        module._build_tool_spec = build_tool_spec  # type: ignore[attr-defined]
    sys.modules[module_name] = module
    return module


def test_orchestrator_preserves_native_tool_spec_true_for_fixed_loop_streaming():
    _register_fake_orchestrator_module(
        "_fake_loop_streaming_fixed", build_tool_spec=_new_build_tool_spec
    )

    class _FixedOrchestrator:
        __module__ = "_fake_loop_streaming_fixed"

    assert (
        hook_mod._orchestrator_preserves_native_tool_spec(_FixedOrchestrator()) is True
    )


def test_orchestrator_preserves_native_tool_spec_false_for_old_loop_streaming():
    _register_fake_orchestrator_module(
        "_fake_loop_streaming_old", build_tool_spec=_old_build_tool_spec
    )

    class _OldOrchestrator:
        __module__ = "_fake_loop_streaming_old"

    assert (
        hook_mod._orchestrator_preserves_native_tool_spec(_OldOrchestrator()) is False
    )


def test_orchestrator_preserves_native_tool_spec_false_when_build_tool_spec_missing():
    """Identified as loop-streaming (module name matches) but doesn't even have
    `_build_tool_spec` - an even older shape than PR #36 anticipated. Still a
    definite "no", not an unknown."""
    _register_fake_orchestrator_module("_fake_loop_streaming_ancient")

    class _AncientOrchestrator:
        __module__ = "_fake_loop_streaming_ancient"

    assert (
        hook_mod._orchestrator_preserves_native_tool_spec(_AncientOrchestrator())
        is False
    )


def test_orchestrator_preserves_native_tool_spec_none_for_unrelated_orchestrator():
    """A totally different, unrelated orchestrator is simply not this check's
    concern - it must not be treated as either confirmed-compatible or
    confirmed-incompatible."""

    class _SomeOtherOrchestrator:
        __module__ = "amplifier_module_loop_basic"

    assert (
        hook_mod._orchestrator_preserves_native_tool_spec(_SomeOtherOrchestrator())
        is None
    )


# ---------------------------------------------------------------------------
# End-to-end: _fail_if_native_tool_passthrough_unsupported / _wrap_provider
# ---------------------------------------------------------------------------


class _FakeCoordinatorWithOrchestrator:
    def __init__(self, orchestrator) -> None:
        self._orchestrator = orchestrator

    def get(self, mount_point, name=None):
        if mount_point == "orchestrator":
            return self._orchestrator
        return None


def test_fail_if_native_tool_passthrough_unsupported_raises_for_old_provider():
    coord = _FakeCoordinatorWithOrchestrator(orchestrator=None)
    with pytest.raises(
        hook_mod.ComputerUseNativeToolPassthroughUnsupportedError
    ) as excinfo:
        hook_mod._fail_if_native_tool_passthrough_unsupported(
            coord, _ProviderPredatingPR79()
        )
    message = str(excinfo.value)
    assert "94a4354" in message
    assert "provider-anthropic" in message.lower()


def test_fail_if_native_tool_passthrough_unsupported_raises_for_old_orchestrator():
    _register_fake_orchestrator_module(
        "_fake_loop_streaming_old_e2e", build_tool_spec=_old_build_tool_spec
    )

    class _OldOrchestrator:
        __module__ = "_fake_loop_streaming_old_e2e"

    coord = _FakeCoordinatorWithOrchestrator(orchestrator=_OldOrchestrator())
    with pytest.raises(
        hook_mod.ComputerUseNativeToolPassthroughUnsupportedError
    ) as excinfo:
        hook_mod._fail_if_native_tool_passthrough_unsupported(
            coord, _ProviderWithWorkingBetaDerivation()
        )
    message = str(excinfo.value)
    assert "f8004e0" in message
    assert "loop-streaming" in message.lower()


def test_fail_if_native_tool_passthrough_unsupported_is_a_noop_when_compatible():
    _register_fake_orchestrator_module(
        "_fake_loop_streaming_fixed_e2e", build_tool_spec=_new_build_tool_spec
    )

    class _FixedOrchestrator:
        __module__ = "_fake_loop_streaming_fixed_e2e"

    coord = _FakeCoordinatorWithOrchestrator(orchestrator=_FixedOrchestrator())
    # Must not raise.
    hook_mod._fail_if_native_tool_passthrough_unsupported(
        coord, _ProviderWithWorkingBetaDerivation()
    )


def test_fail_if_native_tool_passthrough_unsupported_is_a_noop_with_no_orchestrator():
    """No orchestrator mounted (e.g. still starting up) must not be confused with
    an incompatible one - nothing to probe means nothing to fail on."""
    coord = _FakeCoordinatorWithOrchestrator(orchestrator=None)
    # Must not raise.
    hook_mod._fail_if_native_tool_passthrough_unsupported(
        coord, _ProviderWithWorkingBetaDerivation()
    )


def test_wrap_provider_raises_for_provider_predating_pr79():
    """Full integration through `_wrap_provider`, the actual mount-time seam."""
    coord = _FakeCoordinatorWithOrchestrator(orchestrator=None)
    provider = _ProviderPredatingPR79()

    with pytest.raises(hook_mod.ComputerUseNativeToolPassthroughUnsupportedError):
        hook_mod._wrap_provider(provider, coord, max_inline=3)

    # And it must NOT have been mounted as wrapped - a partial wrap is just as
    # dangerous as a fully silent one.
    assert not getattr(provider, hook_mod._WRAPPED_FLAG, False)
