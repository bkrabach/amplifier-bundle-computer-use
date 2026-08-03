"""Unit tests for the fail-closed guard against silently-downgraded native tools.

hook-computer-use used to promote `computer`'s `native_tool_spec` to the wire itself
and inject the matching `anthropic-beta` header ("job 1"). That is now redundant and
has been removed: amplifier-module-loop-streaming PR #36 (commit f8004e0) preserves a
tool's `native_tool_spec` through its own `ToolSpec` construction, and each supported
provider carries the native form the rest of the way in its own idiom -
amplifier-module-provider-anthropic PR #79 (commit 94a4354) derives the required beta
header itself; amplifier-module-provider-openai PR #58 (commit 3af4ce1) recognises the
tool's bare `computer` type and emits it verbatim.

If the mounted provider or orchestrator predates its fix, `computer`'s native
definition silently degrades to a plain function tool - the request is still valid,
the tool still appears, and the model just gets the weaker definition, with no error
and no log line. These tests prove the fix: `_provider_derives_native_tool_betas`,
`_provider_recognizes_bare_computer_tool`, `_provider_supports_native_computer_tool`,
`_orchestrator_preserves_native_tool_spec`, and
`_fail_if_orchestrator_native_tool_spec_unsupported` detect that condition by driving
the real, installed code with throwaway probes - not by trusting a class name or
module path.

Honest, deliberate scope note (see `_provider_supports_native_computer_tool`'s
docstring): a pure capability probe cannot distinguish "this provider was never meant
to support computer-use at all" from "this IS a supported vendor, but the installed
build predates the exact fix being probed for" - both look identical from the
outside. The old `_is_anthropic()` module-name check COULD tell those apart by
trusting a claimed identity; removing it (the point of this change) means that
specific distinction is gone too. What remains loud, provably, is: a provider that
DOES demonstrate a working integration point but computes the wrong answer for it
(`_ProviderWithBrokenBetaDerivation`, `_ProviderWithBrokenBareComputerConversion`
below), and the orchestrator-side check (unaffected by any of this - the orchestrator
identity check was never in scope here).
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
# Provider-side fakes: Anthropic's dated-type convention
# (amplifier-module-provider-anthropic PR #79)
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
    """Pre-PR-#79 shape: no `_derive_native_tool_betas()` at all, and no
    OpenAI-style `_convert_tools_from_request()` either - indistinguishable,
    to a pure capability probe, from a provider that never supported
    computer-use in the first place. See the module docstring."""

    __module__ = "amplifier_module_provider_anthropic"

    async def complete(self, request, **kwargs):
        return "ok"


class _ProviderWithBrokenBetaDerivation:
    """Has the method, but it does not actually derive anything - a broken or
    downgraded implementation, not merely an absent one. This IS distinguishable
    from "wrong vendor": the integration point exists and answers wrong."""

    __module__ = "amplifier_module_provider_anthropic"

    async def complete(self, request, **kwargs):
        return "ok"

    def _derive_native_tool_betas(self, tools):
        return []


# ---------------------------------------------------------------------------
# Provider-side fakes: OpenAI's bare-type convention
# (amplifier-module-provider-openai PR #58)
# ---------------------------------------------------------------------------


class _ProviderWithWorkingBareComputerConversion:
    """Today's shape: `_convert_tools_from_request` emits `computer` bare."""

    __module__ = "amplifier_module_provider_openai"

    async def complete(self, request, **kwargs):
        return "ok"

    def _convert_tools_from_request(self, tools, model_name=None):
        out = []
        for tool in tools:
            if getattr(tool, "type", None) == "computer":
                out.append({"type": "computer"})
                continue
            out.append({"type": "function", "name": getattr(tool, "name", "?")})
        return out


class _ProviderPredatingPR58:
    """Pre-PR-#58 shape: no `_convert_tools_from_request()` at all - same
    "indistinguishable from wrong vendor" honesty note as `_ProviderPredatingPR79`."""

    __module__ = "amplifier_module_provider_openai"

    async def complete(self, request, **kwargs):
        return "ok"


class _ProviderWithBrokenBareComputerConversion:
    """Has `_convert_tools_from_request`, but it degrades `computer` into a
    function tool instead of emitting it bare - a real, observable bug."""

    __module__ = "amplifier_module_provider_openai"

    async def complete(self, request, **kwargs):
        return "ok"

    def _convert_tools_from_request(self, tools, model_name=None):
        return [
            {"type": "function", "name": getattr(t, "name", "?"), "parameters": {}}
            for t in tools
        ]


def test_provider_derives_native_tool_betas_true_for_working_provider():
    assert (
        hook_mod._provider_derives_native_tool_betas(
            _ProviderWithWorkingBetaDerivation()
        )
        is True
    )


def test_provider_derives_native_tool_betas_false_when_method_absent():
    """A pure capability probe cannot tell "predates the fix" apart from "wrong
    vendor entirely" - both simply lack the integration point. See module
    docstring for why that is an accepted, honest trade-off of removing the
    module-name check."""
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


def test_provider_recognizes_bare_computer_tool_true_for_working_provider():
    assert (
        hook_mod._provider_recognizes_bare_computer_tool(
            _ProviderWithWorkingBareComputerConversion()
        )
        is True
    )


def test_provider_recognizes_bare_computer_tool_false_when_method_absent():
    assert (
        hook_mod._provider_recognizes_bare_computer_tool(_ProviderPredatingPR58())
        is False
    )


def test_provider_recognizes_bare_computer_tool_false_when_broken():
    assert (
        hook_mod._provider_recognizes_bare_computer_tool(
            _ProviderWithBrokenBareComputerConversion()
        )
        is False
    )


# ---------------------------------------------------------------------------
# _provider_supports_native_computer_tool - the _is_anthropic() replacement
# ---------------------------------------------------------------------------


def test_provider_supports_native_computer_tool_true_for_anthropic_shape():
    assert (
        hook_mod._provider_supports_native_computer_tool(
            _ProviderWithWorkingBetaDerivation(), "computer_20251124"
        )
        is True
    )


def test_provider_supports_native_computer_tool_true_for_openai_shape():
    assert (
        hook_mod._provider_supports_native_computer_tool(
            _ProviderWithWorkingBareComputerConversion(), "computer"
        )
        is True
    )


def test_provider_supports_native_computer_tool_false_for_neither_shape():
    class _TotallyUnrelatedProvider:
        __module__ = "some_other_vendor.provider"

        async def complete(self, request, **kwargs):
            return "ok"

    assert (
        hook_mod._provider_supports_native_computer_tool(
            _TotallyUnrelatedProvider(), "computer_20251124"
        )
        is False
    )


def test_provider_supports_native_computer_tool_false_when_type_mismatched():
    """A real Anthropic-shaped provider asked about OpenAI's bare type (or vice
    versa) correctly reports no support - the probe is honest about which
    exact type it verified, never conflating the two conventions."""
    assert (
        hook_mod._provider_supports_native_computer_tool(
            _ProviderWithWorkingBetaDerivation(), "computer"
        )
        is False
    )
    assert (
        hook_mod._provider_supports_native_computer_tool(
            _ProviderWithWorkingBareComputerConversion(), "computer_20251124"
        )
        is False
    )


# ---------------------------------------------------------------------------
# Orchestrator-side fakes (amplifier-module-loop-streaming PR #36) - unchanged,
# `_is_loop_streaming` staying a module-name check is deliberate (out of scope
# here - see `_is_loop_streaming`'s docstring).
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
# End-to-end: _fail_if_orchestrator_native_tool_spec_unsupported / _wrap_provider
# ---------------------------------------------------------------------------


class _FakeComputerTool:
    """Minimal stand-in exposing only what `_resolve_native_tool_type` reads:
    a `native_tool_spec` dict carrying the `type` this session's `computer`
    tool is actually configured to declare (Anthropic-versioned, or OpenAI's
    bare `"computer"`)."""

    def __init__(self, tool_type: str) -> None:
        self._tool_type = tool_type

    @property
    def native_tool_spec(self) -> dict[str, object]:
        return {"type": self._tool_type, "name": "computer"}


class _FakeCoordinatorWithOrchestrator:
    def __init__(self, orchestrator, tool_type: str | None = None) -> None:
        self._orchestrator = orchestrator
        self._computer_tool = _FakeComputerTool(tool_type) if tool_type else None

    def get(self, mount_point, name=None):
        if mount_point == "orchestrator":
            return self._orchestrator
        if mount_point == "tools" and name == "computer":
            return self._computer_tool
        return None


def test_fail_if_orchestrator_native_tool_spec_unsupported_raises_for_old_orchestrator():
    _register_fake_orchestrator_module(
        "_fake_loop_streaming_old_e2e", build_tool_spec=_old_build_tool_spec
    )

    class _OldOrchestrator:
        __module__ = "_fake_loop_streaming_old_e2e"

    coord = _FakeCoordinatorWithOrchestrator(orchestrator=_OldOrchestrator())
    with pytest.raises(
        hook_mod.ComputerUseNativeToolPassthroughUnsupportedError
    ) as excinfo:
        hook_mod._fail_if_orchestrator_native_tool_spec_unsupported(coord)
    message = str(excinfo.value)
    assert "f8004e0" in message
    assert "loop-streaming" in message.lower()


def test_fail_if_orchestrator_native_tool_spec_unsupported_is_a_noop_when_compatible():
    _register_fake_orchestrator_module(
        "_fake_loop_streaming_fixed_e2e", build_tool_spec=_new_build_tool_spec
    )

    class _FixedOrchestrator:
        __module__ = "_fake_loop_streaming_fixed_e2e"

    coord = _FakeCoordinatorWithOrchestrator(orchestrator=_FixedOrchestrator())
    # Must not raise.
    hook_mod._fail_if_orchestrator_native_tool_spec_unsupported(coord)


def test_fail_if_orchestrator_native_tool_spec_unsupported_is_a_noop_with_no_orchestrator():
    """No orchestrator mounted (e.g. still starting up) must not be confused with
    an incompatible one - nothing to probe means nothing to fail on."""
    coord = _FakeCoordinatorWithOrchestrator(orchestrator=None)
    # Must not raise.
    hook_mod._fail_if_orchestrator_native_tool_spec_unsupported(coord)


def test_wrap_provider_skips_quietly_for_provider_predating_pr79():
    """Full integration through `_wrap_provider`, the actual mount-time seam.

    Behavior change from the old `_is_anthropic()`-gated design, deliberate and
    documented (see module docstring and `_provider_supports_native_computer_tool`):
    a provider indistinguishable from "wrong vendor" is skipped quietly, logged,
    not raised. Loud failure is reserved for a provider that demonstrates a real,
    working integration point but computes the wrong answer, or an orchestrator
    that positively fails its own probe (see the other tests in this file)."""
    coord = _FakeCoordinatorWithOrchestrator(orchestrator=None)
    provider = _ProviderPredatingPR79()

    wrapped = hook_mod._wrap_provider(provider, coord, max_inline=3)

    assert wrapped is False
    assert not getattr(provider, hook_mod._WRAPPED_FLAG, False)


def test_wrap_provider_skips_quietly_for_provider_predating_pr58():
    coord = _FakeCoordinatorWithOrchestrator(orchestrator=None)
    provider = _ProviderPredatingPR58()

    wrapped = hook_mod._wrap_provider(provider, coord, max_inline=3)

    assert wrapped is False
    assert not getattr(provider, hook_mod._WRAPPED_FLAG, False)


def test_capability_probe_rejection_is_operator_actionable(caplog):
    """The capability-probe rejection (`_provider_supports_native_computer_tool`
    returning False in `_wrap_provider`) is a real, silent capability gap for
    this session - 'computer' just quietly runs as a plain function tool - and
    it used to be logged at INFO, a level routinely filtered out of default
    log verbosity. This is fail-loud-to-the-system without being fail-loud-to-
    the-human: refusing to promote the tool is the right call, but nothing
    told an operator it happened, why, or what to do about it.

    Proves three things about the log line this path now produces: it is
    loud enough to see by default (WARNING, not INFO), it is honest about the
    ambiguity `_provider_supports_native_computer_tool`'s own docstring
    describes (a negative result cannot distinguish "wrong vendor" from "right
    vendor, old build"), and it tells a human what to do about each case.
    """
    coord = _FakeCoordinatorWithOrchestrator(orchestrator=None)
    provider = _ProviderPredatingPR79()

    with caplog.at_level("WARNING", logger=hook_mod.__name__):
        wrapped = hook_mod._wrap_provider(provider, coord, max_inline=3)

    assert wrapped is False
    records = [r for r in caplog.records if r.levelname == "WARNING"]
    assert records, "capability-probe rejection must be visible at WARNING, not INFO"
    message = records[0].getMessage()
    assert "does not carry native tool type" not in message  # old, INFO-only wording
    assert "NOT enabled" in message
    assert "what to do" in message.lower()


def test_wrap_provider_wraps_openai_shaped_provider():
    """Regression guard for the whole point of this change: an OpenAI-shaped
    provider with working bare-computer-tool passthrough gets wrapped, exactly
    like an Anthropic-shaped one already does elsewhere in this suite.

    Registers a fake `computer` tool declaring the bare `"computer"` type -
    `_resolve_native_tool_type` reads that real, mounted type rather than
    falling back to the Anthropic-shaped default, so the probe checks
    against the type this session actually declares."""
    coord = _FakeCoordinatorWithOrchestrator(orchestrator=None, tool_type="computer")
    provider = _ProviderWithWorkingBareComputerConversion()

    wrapped = hook_mod._wrap_provider(provider, coord, max_inline=3)

    assert wrapped is True
    assert getattr(provider, hook_mod._WRAPPED_FLAG, False) is True


# ---------------------------------------------------------------------------
# _resolve_native_tool_type: read the fact, do not infer it from the artifact
# ---------------------------------------------------------------------------


class _CoordinatorWithTool:
    def __init__(self, tool) -> None:
        self._tool = tool

    def get(self, mount_point, name=None):
        return self._tool if (mount_point, name) == ("tools", "computer") else None


def test_resolve_native_tool_type_prefers_the_tools_own_statement():
    """`native_tool_type` is the tool STATING which type it is declaring.
    `native_tool_spec["type"]` is that same fact INFERRED from a vendor-shaped
    wire dict. When both exist the stated one wins - it is the authority, and
    the wire dict is only ever a projection of it."""

    class _StatesAndDeclares:
        @property
        def native_tool_type(self) -> str:
            return "computer_20250124"

        @property
        def native_tool_spec(self) -> dict:
            return {"type": "computer_20250124", "name": "computer"}

    assert (
        hook_mod._resolve_native_tool_type(_CoordinatorWithTool(_StatesAndDeclares()))
        == "computer_20250124"
    )


def test_resolve_native_tool_type_falls_back_to_the_wire_type_when_not_stated():
    """Unchanged behaviour for anything mounted under `computer` that predates
    `native_tool_type` - including this file's own `_FakeComputerTool`."""
    coord = _CoordinatorWithTool(_FakeComputerTool("computer"))
    assert hook_mod._resolve_native_tool_type(coord) == "computer"


def test_resolve_native_tool_type_answers_for_a_declaration_with_no_wire_type():
    """THE GAP THIS CLOSES. A vendor whose declaration is discriminated by its
    own key has no top-level `type`, so the old inference returned `None` and
    this function fell through to `_DEFAULT_PROBE_TOOL_TYPE` - a DIFFERENT
    vendor's type - and then probed the mounted provider for the wrong wire
    convention, silently. Reading the stated fact answers correctly without
    this module knowing any wire format, and without importing anything (it
    declares `dependencies = []`)."""

    class _NoWireType:
        @property
        def native_tool_type(self) -> str:
            return "some_vendor_tool"

        @property
        def native_tool_spec(self) -> dict:
            return {"some_vendor_tool": {"environment": "DESKTOP"}}

    resolved = hook_mod._resolve_native_tool_type(_CoordinatorWithTool(_NoWireType()))
    assert resolved == "some_vendor_tool"
    assert resolved != hook_mod._DEFAULT_PROBE_TOOL_TYPE


def test_resolve_native_tool_type_survives_a_raising_stated_type():
    """Both sources are properties, and a property that raises must not take
    down the request path (the D3 class of bug). A raising `native_tool_type`
    logs and falls through to the wire type rather than propagating."""

    class _RaisesThenDeclares:
        @property
        def native_tool_type(self) -> str:
            raise RuntimeError("boom")

        @property
        def native_tool_spec(self) -> dict:
            return {"type": "computer_20241022"}

    coord = _CoordinatorWithTool(_RaisesThenDeclares())
    assert hook_mod._resolve_native_tool_type(coord) == "computer_20241022"


def test_resolve_native_tool_type_falls_back_when_nothing_is_mounted():
    assert (
        hook_mod._resolve_native_tool_type(_CoordinatorWithTool(None))
        == hook_mod._DEFAULT_PROBE_TOOL_TYPE
    )
