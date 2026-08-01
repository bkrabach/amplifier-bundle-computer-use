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
    """Not exercised by these tests - `_wrap_provider` only needs it for
    `_promote_tools`, which never runs before the stream-detection guard fires."""

    def get(self, mount_point, name=None):
        return None


class _AnthropicProviderNoStream:
    """Today's shape: only `complete()`. Must wrap successfully (regression guard)."""

    __module__ = "amplifier_module_provider_anthropic"

    async def complete(self, request, **kwargs):
        return "ok"


class _AnthropicProviderWithStream:
    """Tomorrow's shape: `stream()` has been added. Must be refused, not silently
    wrapped."""

    __module__ = "amplifier_module_provider_anthropic"

    async def complete(self, request, **kwargs):
        return "ok"

    async def stream(self, request, **kwargs):
        yield "chunk"


class _OtherVendorProviderWithStream:
    """A provider that isn't Anthropic at all, and happens to have `stream()`. The
    guard must not fire here - this hook never wraps non-Anthropic providers in the
    first place, so there is nothing to be silently inert about.

    Note: neither this class name nor its `__module__` may contain the substring
    "anthropic" (case-insensitive) - `_is_anthropic()` does a plain substring match,
    so a test double named e.g. `_NonAnthropicProvider` would (ironically) match it.
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
