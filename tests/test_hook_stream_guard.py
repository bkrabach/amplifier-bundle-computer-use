"""Unit tests for the fail-closed guard against silently-inert provider wrapping.

The orchestrator (loop-streaming) calls `provider.stream()` instead of
`provider.complete()` whenever `hasattr(provider, "stream")` is true. This hook only
wraps `complete()`. If a provider ever gains a `stream()` method, wrapping it would
"succeed" (log "wrapped provider ... for native computer use") while being completely
inert on the actual request hot path - no exception, no error event, session looks
healthy while computer-use silently never engages.

These tests prove the fix: `_wrap_provider` (and its `hasattr(provider, "stream")`
detector, `_fail_if_stream_incompatible`) refuses loudly instead of degrading.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "modules" / "tool-computer-use"))
sys.path.insert(0, str(ROOT / "modules" / "hook-computer-use"))

import amplifier_module_hook_computer_use as hook_mod
import pytest


class _FakeCoordinator:
    """`_wrap_provider` also needs this for `_fail_if_native_tool_passthrough_unsupported`
    (the orchestrator lookup). Returning `None` for every mount point - including
    `"orchestrator"` - means that check finds no orchestrator to probe and skips
    it, so it never interferes with what these tests are actually about: the
    stream-detection guard."""

    def get(self, mount_point, name=None):
        return None


class _AnthropicProviderNoStream:
    """Today's shape: `complete()` plus a working `_derive_native_tool_betas()`
    (amplifier-module-provider-anthropic PR #79). Must wrap successfully
    (regression guard) - the native-tool-passthrough compatibility check (see
    `_fail_if_native_tool_passthrough_unsupported`) must not block a provider
    these tests intend to be fully compatible."""

    __module__ = "amplifier_module_provider_anthropic"

    async def complete(self, request, **kwargs):
        return "ok"

    def _derive_native_tool_betas(self, tools):
        return ["computer-use-2025-11-24"] if tools else []


class _AnthropicProviderWithStream:
    """Tomorrow's shape: still has a working `_derive_native_tool_betas()` (so the
    new capability gate, `_provider_supports_native_computer_tool`, confirms it),
    but `stream()` has been added. Must be refused, not silently wrapped."""

    __module__ = "amplifier_module_provider_anthropic"

    async def complete(self, request, **kwargs):
        return "ok"

    async def stream(self, request, **kwargs):
        yield "chunk"

    def _derive_native_tool_betas(self, tools):
        return ["computer-use-2025-11-24"] if tools else []


class _OtherVendorProviderWithStream:
    """A provider with no native-computer-tool capability at all (neither
    `_derive_native_tool_betas` nor `_convert_tools_from_request`), that happens to
    have `stream()`. The guard must not fire here - `_provider_supports_native_computer_tool`
    already short-circuits `_wrap_provider` before the stream check ever runs, so
    there is nothing to be silently inert about.
    """

    __module__ = "some_other_vendor.provider"

    async def complete(self, request, **kwargs):
        return "ok"

    async def stream(self, request, **kwargs):
        yield "chunk"


def test_fail_if_stream_incompatible_raises_when_stream_present():
    with pytest.raises(hook_mod.ComputerUseHookIncompatibleProviderError):
        hook_mod._fail_if_stream_incompatible(_AnthropicProviderWithStream())


def test_fail_if_stream_incompatible_is_a_noop_without_stream():
    # Must not raise.
    hook_mod._fail_if_stream_incompatible(_AnthropicProviderNoStream())


def test_wrap_provider_raises_for_anthropic_provider_with_stream():
    coord = _FakeCoordinator()
    provider = _AnthropicProviderWithStream()

    with pytest.raises(hook_mod.ComputerUseHookIncompatibleProviderError) as excinfo:
        hook_mod._wrap_provider(provider, coord, max_inline=3)

    # Message must name what happened and why - not a generic error.
    message = str(excinfo.value)
    assert "stream" in message.lower()
    assert type(provider).__name__ in message

    # And it must NOT have been mounted as wrapped - a partial/silent wrap would be
    # just as dangerous as a fully silent one.
    assert not getattr(provider, hook_mod._WRAPPED_FLAG, False)
    assert provider.complete.__name__ != "complete" or not hasattr(
        provider, hook_mod._WRAPPED_FLAG
    )


def test_wrap_provider_still_wraps_anthropic_provider_without_stream():
    """Regression guard: today's provider-anthropic (no stream()) must keep working
    exactly as before."""
    coord = _FakeCoordinator()
    provider = _AnthropicProviderNoStream()

    wrapped = hook_mod._wrap_provider(provider, coord, max_inline=3)

    assert wrapped is True
    assert getattr(provider, hook_mod._WRAPPED_FLAG, False) is True


def test_wrap_provider_does_not_raise_for_non_anthropic_provider_with_stream():
    """The guard is specifically about providers THIS HOOK wraps. A non-Anthropic
    provider with `stream()` is simply not our concern - `_is_anthropic()` already
    short-circuits before the stream check runs."""
    coord = _FakeCoordinator()
    provider = _OtherVendorProviderWithStream()

    wrapped = hook_mod._wrap_provider(provider, coord, max_inline=3)

    assert wrapped is False
    assert not getattr(provider, hook_mod._WRAPPED_FLAG, False)


def test_wrap_provider_is_idempotent_and_does_not_recheck_after_first_wrap():
    """Once wrapped, `_wrap_provider` returns False immediately (already-wrapped
    guard) without re-evaluating the stream check - matching the existing
    idempotency contract for repeated hook firings across a session."""
    coord = _FakeCoordinator()
    provider = _AnthropicProviderNoStream()

    assert hook_mod._wrap_provider(provider, coord, max_inline=3) is True
    # Second call: already wrapped, returns False, no re-raise even though nothing
    # about the provider changed.
    assert hook_mod._wrap_provider(provider, coord, max_inline=3) is False
