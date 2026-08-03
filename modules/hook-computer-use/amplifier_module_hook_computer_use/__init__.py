"""Amplifier hook module: make Anthropic's NATIVE computer-use tool work end to end.

One thing used to stand between a mounted `computer` tool and real computer use,
and it lived in the orchestrator, which we did not want to fork: tool results are
collapsed to ``str`` before they reach the provider, so a screenshot can never
travel back as an image content block.

That is fixed at a single seam: the provider's ``complete()`` call. This hook wraps
it and, on the way through:

* expands screenshot markers in tool results into real base64 image blocks,
* keeps only the most recent screenshots inline, so long sessions stay affordable.

Nothing is forked, nothing is patched on disk, and removing the hook degrades the
tool cleanly back to an ordinary function tool.

Tool-spec promotion (rewriting ``computer`` into Anthropic's native wire form and
injecting the ``anthropic-beta`` header) used to live here too, but is now handled
upstream: `amplifier-module-loop-streaming` preserves a tool's ``native_tool_spec``
through its own `ToolSpec` construction, and `amplifier-module-provider-anthropic`
derives the required beta header itself from the native tool types present on the
request. This hook now only *verifies* that support is present
(`_fail_if_native_tool_passthrough_unsupported`) rather than doing the work itself -
see that function's docstring for why a silent version mismatch is not acceptable
here.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import sys
import warnings
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from amplifier_core import HookResult

try:  # event name is a plain constant, but tolerate kernels that move it
    from amplifier_core.events import PROVIDER_REQUEST  # type: ignore[attr-defined]
except ImportError:  # pragma: no cover
    PROVIDER_REQUEST = "provider:request"

try:
    from amplifier_core.events import TOOL_PRE  # type: ignore[attr-defined]
except ImportError:  # pragma: no cover
    TOOL_PRE = "tool:pre"

try:
    from amplifier_core.events import TOOL_POST  # type: ignore[attr-defined]
except ImportError:  # pragma: no cover
    TOOL_POST = "tool:post"

logger = logging.getLogger(__name__)

__version__ = "0.1.0"

MARKER = "__amplifier_computer_use__"
_WRAPPED_FLAG = "_amplifier_computer_use_wrapped"
_DEFAULT_MAX_INLINE_IMAGES = 3

#: Set AMPLIFIER_COMPUTER_USE_TRACE=<path> to record what this hook did and when.
#: Invaluable when "the model says it cannot see the screen" and you need to know
#: whether the hook mounted, fired, wrapped, and rewrote - without reading events.jsonl.
_TRACE_PATH = os.environ.get("AMPLIFIER_COMPUTER_USE_TRACE")


def _trace(msg: str) -> None:
    if not _TRACE_PATH:
        return
    try:
        with open(_TRACE_PATH, "a", encoding="utf-8") as fh:
            fh.write(
                f"{datetime.now(tz=timezone.utc).isoformat(timespec='seconds')} [{os.getpid()}] {msg}\n"
            )
    except OSError:
        pass


def _is_anthropic(provider: Any) -> bool:
    return (
        "anthropic" in f"{type(provider).__module__}.{type(provider).__name__}".lower()
    )


class ComputerUseHookIncompatibleProviderError(RuntimeError):
    """Raised when the provider we are about to wrap would make this hook a silent no-op.

    This hook only patches `Provider.complete()`. The bundled orchestrator
    (loop-streaming, `amplifier_module_loop_streaming/__init__.py:827`) branches on
    `hasattr(provider, "stream")`: whenever a provider exposes `stream()`, the
    orchestrator calls THAT method instead of `complete()` - our wrap never runs.
    Today this "works" only because `provider-anthropic` happens not to define
    `stream()`. There is no exception, no log line, and no event the day that changes:
    the hook still reports "wrapped provider ... for native computer use", mount()
    still succeeds, and computer-use silently goes blind while the session otherwise
    looks completely healthy.

    A computer-use bundle that silently stops driving the computer is worse than one
    that refuses to load. So: refuse. Loudly, unmissably, the first time a provider
    with `stream()` is seen - not a warning that scrolls past in a log stream.
    """


def _fail_if_stream_incompatible(provider: Any) -> None:
    if hasattr(provider, "stream"):
        raise ComputerUseHookIncompatibleProviderError(
            f"computer-use: provider {type(provider).__name__} "
            f"({type(provider).__module__}) now exposes stream() - hook-computer-use "
            "only wraps complete(), and the orchestrator prefers stream() whenever it "
            "is present. Wrapping this provider would silently do nothing: no "
            "screenshot inlining, no error, no log line. Refusing to operate rather "
            "than degrade invisibly. Fix: either wrap complete() AND stream() in this "
            "hook, or route computer-use through an orchestrator that does not prefer "
            "stream()."
        )


class ComputerUseNativeToolPassthroughUnsupportedError(RuntimeError):
    """Raised when the mounted orchestrator/provider cannot carry `computer`'s
    native tool form to the wire without this hook doing it for them.

    hook-computer-use used to promote `computer`'s `native_tool_spec` to the wire
    itself and inject the matching `anthropic-beta` header (see CHANGELOG /
    module docstring - "job 1"). That is now redundant and has been removed:

    * `amplifier-module-loop-streaming` (commit f8004e0, PR #36 - "feat: preserve
      model-native tool form through ToolSpec construction") makes its
      `_build_tool_spec()` preserve a tool's `native_tool_spec` through `ToolSpec`
      construction - `ToolSpec` is `extra="allow"`, so the native keys ride along
      as extras and reach the provider intact.
    * `amplifier-module-provider-anthropic` (commit 94a4354, PR #79 - "fix:
      cache_control targets last function tool; derive betas from native tool
      types") makes the provider derive the required `anthropic-beta` header
      itself from the native tool types present on the request, and stop
      stamping `cache_control` onto them.

    If EITHER upstream module predates its fix, `computer`'s native definition
    silently degrades to a plain function tool: the request is still valid, the
    tool still appears, and the model just gets the weaker definition - measurably
    worse targeting, with no error and no log line. That is the exact
    silent-downgrade failure mode this bundle exists to prevent (see
    `ComputerUseHookIncompatibleProviderError` for the sibling guard against the
    same class of failure on the streaming path). So: refuse to mount rather than
    let it happen invisibly.

    Detection deliberately does not trust a version string - manifests can lie,
    and a shallow git clone may not even have one. Instead it drives the actual
    installed code with a throwaway probe tool/tool-list and checks the real
    output, exactly the way `_fail_if_stream_incompatible` checks for a real
    `stream` attribute rather than a claimed version.
    """


def _provider_derives_native_tool_betas(provider: Any) -> bool:
    """Probe whether `provider` self-derives `anthropic-beta` headers for native
    tool types (amplifier-module-provider-anthropic PR #79).

    Only called after `_is_anthropic(provider)` has already confirmed this IS
    the provider brand this hook depends on - unlike the orchestrator probe
    below, there is no "unknown vendor, cannot judge" case to consider here.
    A real provider-anthropic unconditionally has a working
    `_derive_native_tool_betas()` from PR #79 onward, so an absent or broken
    one is exactly the pre-PR-#79 shape, not an ambiguous signal.
    """
    derive = getattr(provider, "_derive_native_tool_betas", None)
    if not callable(derive):
        return False
    try:
        betas = derive([{"type": "computer_20251124"}])
    except Exception:
        logger.exception(
            "computer-use: _derive_native_tool_betas probe raised on %s",
            type(provider).__name__,
        )
        return False
    return isinstance(betas, list) and any("computer-use" in str(b) for b in betas)


def _is_loop_streaming(orchestrator: Any) -> bool:
    """Same module-name heuristic `_is_anthropic()` uses for providers, applied
    to the orchestrator. Needed because - unlike the provider, already
    confirmed Anthropic before `_provider_derives_native_tool_betas` runs - the
    mounted orchestrator could be anything, including one this hook has no
    opinion about at all."""
    identity = f"{type(orchestrator).__module__}.{type(orchestrator).__name__}"
    return "loop_streaming" in identity.lower().replace("-", "_")


def _orchestrator_preserves_native_tool_spec(orchestrator: Any) -> bool | None:
    """Probe whether the mounted orchestrator's tool-spec construction preserves
    a tool's `native_tool_spec` (amplifier-module-loop-streaming PR #36).

    Exercises the orchestrator module's own `_build_tool_spec()` against a
    throwaway stub tool exposing `native_tool_spec`, and checks whether the
    native `type` actually survives into the emitted `ToolSpec`. This is a real
    behavioural probe of the installed code, not a version string.

    Returns:
        True  - identified as loop-streaming, and it preserves the native type.
        False - identified as loop-streaming, but it does not: either
                `_build_tool_spec()` is missing entirely, or it drops the
                native type (the pre-PR-#36 behaviour).
        None  - NOT identified as loop-streaming at all (see
                `_is_loop_streaming`) - some other orchestrator is mounted,
                which is simply not this check's concern. Callers must not
                treat this as "confirmed compatible".
    """
    if not _is_loop_streaming(orchestrator):
        return None
    module = sys.modules.get(type(orchestrator).__module__)
    build: Any = getattr(module, "_build_tool_spec", None) if module else None
    if not callable(build):
        return False

    class _NativeToolSpecProbe:
        name = "__computer_use_native_tool_spec_probe__"
        description = "probe"
        input_schema: dict[str, Any] = {}

        @property
        def native_tool_spec(self) -> dict[str, Any]:
            return {
                "type": "computer_20251124",
                "name": "__computer_use_native_tool_spec_probe__",
            }

    try:
        spec: Any = build(_NativeToolSpecProbe())
        dumped = spec.model_dump(exclude_none=True)
    except Exception:
        logger.exception(
            "computer-use: native_tool_spec passthrough probe raised on orchestrator %s",
            type(orchestrator).__name__,
        )
        return False
    return dumped.get("type") == "computer_20251124"


def _fail_if_native_tool_passthrough_unsupported(
    coordinator: Any, provider: Any
) -> None:
    """Refuse to mount if `computer`'s native tool form cannot reach the wire
    without this hook promoting it itself. See
    `ComputerUseNativeToolPassthroughUnsupportedError` for the full rationale.

    The orchestrator probe returns `None` when it cannot be run at all (some
    orchestrator other than loop-streaming is mounted) - that is "not this
    check's concern", not "confirmed compatible", and is intentionally NOT
    treated as a failure: we only refuse to mount when the probe positively
    identified loop-streaming AND it came back negative. The provider probe
    has no such middle state - see its docstring.
    """
    if not _provider_derives_native_tool_betas(provider):
        raise ComputerUseNativeToolPassthroughUnsupportedError(
            f"computer-use: provider {type(provider).__name__} "
            f"({type(provider).__module__}) does not derive anthropic-beta headers "
            "from native tool types (no working _derive_native_tool_betas()). "
            "hook-computer-use no longer injects this header itself - upgrade "
            "amplifier-module-provider-anthropic to at least commit 94a4354 (PR #79, "
            "'fix: cache_control targets last function tool; derive betas from "
            "native tool types'), or the `computer` tool's native definition will "
            "silently degrade to a plain function tool."
        )

    orchestrator = None
    try:
        orchestrator = coordinator.get("orchestrator")
    except Exception:
        logger.debug("computer-use: orchestrator lookup failed", exc_info=True)
    if orchestrator is not None and (
        _orchestrator_preserves_native_tool_spec(orchestrator) is False
    ):
        raise ComputerUseNativeToolPassthroughUnsupportedError(
            f"computer-use: orchestrator {type(orchestrator).__name__} "
            f"({type(orchestrator).__module__}) does not preserve a tool's "
            "native_tool_spec through its ToolSpec construction. "
            "hook-computer-use no longer promotes tool specs itself - upgrade "
            "amplifier-module-loop-streaming to at least commit f8004e0 (PR #36, "
            "'feat: preserve model-native tool form through ToolSpec "
            "construction'), or the `computer` tool's native definition will "
            "silently degrade to a plain function tool."
        )


#: Reused decoder for `_loads_leading` - `json.JSONDecoder` is stateless/reentrant
#: across `raw_decode` calls, so one module-level instance is safe to share.
_JSON_DECODER = json.JSONDecoder()


def _loads_leading(text: str) -> Any:
    """Parse the JSON value at the *start* of `text`, ignoring anything after it.

    Tool-result content is not necessarily JSON-and-only-JSON by the time this hook
    sees it: the kernel's `HookResult.append_to_last_tool_result` mechanism (see
    HOOKS_API.md - "Injection placement control") lets OTHER hooks glue their own
    text onto the tail of the *same* last-tool-result content string ours occupies.
    Session-start reminders (`hooks-status-context`, `hooks-todo-reminder`,
    `mode-status`, `hooks-skills-visibility`, ...) do exactly this on the very
    first tool call of a session - the common case a screenshot is taken in.

    That mechanism is legitimate, general kernel policy we neither own nor control
    the timing of (per KERNEL_PHILOSOPHY.md, "policy lives at the edges" - other
    hooks are free to append). What we own is not assuming we're the only thing
    that will ever write to that string. `json.loads` requires the *entire* input
    to be consumed and raises `JSONDecodeError: Extra data` the moment anything
    trails the closing brace - so a session-start reminder appended after our
    marker silently made every first-screenshot expansion fail, having nothing to
    do with local vs. remote. `raw_decode` parses one JSON value from the start of
    the string and simply reports where it stopped, so trailing text - ours or
    anyone else's - no longer breaks marker detection.
    """
    return _JSON_DECODER.raw_decode(text.lstrip())[0]


def _parse_marker(content: Any) -> dict[str, Any] | None:
    """Return the computer-use payload if this tool content carries one.

    The orchestrator does not hand our ``ToolResult`` straight to the provider - it
    serialises it into an envelope, so the real payload arrives as a JSON *string*
    nested under ``output``::

        {"error": null, "output": "{\\"__amplifier_computer_use__\\": 1, ...}"}

    Unwrap whatever shape shows up rather than assuming one: envelope, bare payload,
    or already-decoded dict. The content string may also carry trailing text
    appended by another hook (see `_loads_leading`) - tolerate that too.
    """
    if isinstance(content, dict):
        return content if MARKER in content else _parse_marker(content.get("output"))
    if not isinstance(content, str) or MARKER not in content:
        return None
    try:
        data = _loads_leading(content)
    except (json.JSONDecodeError, TypeError, ValueError):
        return None
    if isinstance(data, dict):
        if MARKER in data:
            return data
        return _parse_marker(data.get("output"))
    return None


def _image_block(path: str) -> dict[str, Any] | None:
    try:
        raw = Path(path).read_bytes()
    except OSError:
        logger.warning("computer-use: screenshot %s is gone; sending text only", path)
        return None
    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": "image/png",
            "data": base64.standard_b64encode(raw).decode(),
        },
    }


def _read_content(msg: Any) -> Any:
    """Messages reach the provider as pydantic ``Message`` objects, but plain dicts
    show up in tests and in other orchestrators. Support both."""
    if isinstance(msg, dict):
        return msg.get("content")
    return getattr(msg, "content", None)


def _with_content(msg: Any, content: Any) -> Any:
    """Return a copy of `msg` carrying new content.

    Content is deliberately written as *plain dicts*, not ``TextBlock``/``ImageBlock``
    instances: those serialise an extra ``visibility: null`` key that the Anthropic API
    rejects inside a ``tool_result``. Pydantic emits a cosmetic serializer warning for
    the raw dicts (suppressed at mount) but produces exactly the bytes the API wants.
    """
    if isinstance(msg, dict):
        return {**msg, "content": content}
    try:
        clone = msg.model_copy()
        clone.content = content
        return clone
    except Exception:  # noqa: BLE001 - not a pydantic model; fall back to mutating
        try:
            msg.content = content
        except Exception:  # noqa: BLE001 - immutable message; report and move on
            logger.warning(
                "computer-use: could not rewrite message content on %r",
                type(msg).__name__,
            )
        return msg


def _expand_tool_results(messages: list[Any], max_inline: int) -> list[Any]:
    """Turn screenshot markers into image blocks, newest `max_inline` kept inline."""
    rewritten: list[Any] = []
    budget = max_inline
    for msg in reversed(messages):
        payload = _parse_marker(_read_content(msg))
        if payload is None:
            rewritten.append(msg)
            continue

        text = str(payload.get("text") or "screenshot")
        images = [p for p in payload.get("images", []) if isinstance(p, str)]
        blocks: list[dict[str, Any]] = []
        # Distinguish "budget exhausted before we ever tried this file" (superseded)
        # from "we tried to read it and it was gone" (missing) - these used to share
        # one message ("superseded by a newer screenshot"), which was actively
        # misleading for the missing-file case: the model would be told its
        # screenshot was dropped for recency reasons when the real reason was the
        # file being pruned/unreadable.
        missing = False
        if budget > 0 and images:
            for path in images:
                block = _image_block(path)
                if block is not None:
                    blocks.append(block)
                else:
                    missing = True
            if blocks:
                budget -= 1
        if blocks:
            rewritten.append(
                _with_content(msg, [{"type": "text", "text": text}, *blocks])
            )
        else:
            if not images:
                note = ""
            elif missing:
                note = " [image dropped: screenshot file no longer available]"
            else:
                note = " [image dropped: superseded by a newer screenshot]"
            rewritten.append(_with_content(msg, f"{text}{note}"))
    rewritten.reverse()
    return rewritten


def _wrap_provider(provider: Any, coordinator: Any, max_inline: int) -> bool:
    if getattr(provider, _WRAPPED_FLAG, False):
        return False
    if not _is_anthropic(provider):
        logger.info(
            "computer-use: provider %s is not Anthropic; native tool not enabled",
            type(provider).__name__,
        )
        return False
    # Fail loud (see ComputerUseHookIncompatibleProviderError) BEFORE wrapping, not
    # after: wrapping a stream()-capable provider would "succeed" and log
    # "wrapped provider ... for native computer use" while the wrap is never
    # actually exercised on the request hot path.
    _fail_if_stream_incompatible(provider)
    # Same reasoning, different failure mode: if the mounted orchestrator/provider
    # cannot carry `computer`'s native tool form to the wire on their own, wrapping
    # would still "succeed" while the tool silently degrades to a plain function
    # tool. See ComputerUseNativeToolPassthroughUnsupportedError.
    _fail_if_native_tool_passthrough_unsupported(coordinator, provider)
    if not hasattr(provider, "complete"):
        return False

    original = provider.complete

    async def complete(request: Any, **kwargs: Any):
        try:
            messages = getattr(request, "messages", None)
            if isinstance(messages, list):
                if _TRACE_PATH:
                    for m in messages[-6:]:
                        c = _read_content(m)
                        role = (
                            m.get("role")
                            if isinstance(m, dict)
                            else getattr(m, "role", "?")
                        )
                        _trace(
                            f"  msg role={role} content_type={type(c).__name__} preview={str(c)[:160]!r}"
                        )
                before = sum(1 for m in messages if _parse_marker(_read_content(m)))
                request.messages = _expand_tool_results(messages, max_inline)
                inlined = sum(
                    1 for m in request.messages if isinstance(_read_content(m), list)
                )
                if before:
                    _trace(f"complete: markers={before} messages_with_blocks={inlined}")
        except Exception:
            logger.exception(
                "computer-use: request rewrite failed; sending request unchanged"
            )
        return await original(request, **kwargs)

    provider.complete = complete
    setattr(provider, _WRAPPED_FLAG, True)
    _trace(
        f"WRAPPED provider={type(provider).__name__} module={type(provider).__module__}"
    )
    logger.info(
        "computer-use: wrapped provider %s for screenshot inlining",
        type(provider).__name__,
    )
    return True


#: `computer` action names that mutate the target, mirrored from
#: `tool-computer-use`'s own `MUTATING` set so this hook does not need to
#: import that module (kept small and duplicated deliberately - see
#: `_make_gate_handler`'s docstring for why a local copy is safer here than a
#: cross-module import).
_GATE_MUTATING_ACTIONS = {
    "mouse_move",
    "left_click",
    "right_click",
    "middle_click",
    "double_click",
    "triple_click",
    "left_mouse_down",
    "left_mouse_up",
    "left_click_drag",
    "scroll",
    "key",
    "hold_key",
    "type",
    "focus_window",
    "set_clipboard",
}


def _make_gate_handler(coordinator: Any):
    """Build the `tool:pre` handler implementing the write-confirmation gate
    (`docs/designs/remote-transport.md` \u00a710.4): "Gate every WRITE, or gate
    none - anything finer is guesswork wearing a confidence costume."

    This hook is pure POLICY sitting on top of a MECHANISM `ComputerTool`
    already computes and exposes (`gate_writes`, `is_remote` - see
    tool-computer-use's `__init__.py`): whether to gate is the tool's own,
    already-resolved decision (defaults to on for a remote target with
    `read_only=False`, off otherwise - see that module for the exact rule).
    This function only turns "gate_writes is True and this action mutates"
    into the kernel's own `ask_user` mechanism (`HOOKS_API.md`) - it invents
    no new confirmation system of its own, per the design doc's explicit
    guidance not to.
    """

    async def handler(event: str, data: dict[str, Any]) -> HookResult:
        tool_name = (data or {}).get("tool_name")
        if tool_name not in ("computer", "desktop"):
            return HookResult(action="continue")
        try:
            tool = coordinator.get("tools", tool_name)
        except Exception:  # noqa: BLE001 - a lookup failure degrades to "no gate", never a crash
            return HookResult(action="continue")
        computer = tool if tool_name == "computer" else getattr(tool, "_computer", None)
        if computer is None or not getattr(computer, "_gate_writes", False):
            return HookResult(action="continue")
        action = (data.get("tool_input") or {}).get("action")
        if action not in _GATE_MUTATING_ACTIONS:
            return HookResult(action="continue")
        backend_name = getattr(getattr(computer, "_backend", None), "name", "?")
        return HookResult(
            action="ask_user",
            approval_prompt=(
                f"Remote computer-use ({backend_name}): allow "
                f"{tool_name}.{action!r} on the target desktop?"
            ),
            approval_options=["Allow", "Deny"],
            approval_default="deny",
            reason="gate_writes is enabled for this remote target (\u00a710.4)",
        )

    return handler


def _make_halt_notice_handler(coordinator: Any):
    """Build the `tool:post` handler that closes defect 1
    (`docs/designs/coexistence.md` \u00a76.0): a halted session must not be able
    to reach a final response without the interruption in front of the
    model.

    The `HaltedError` message already reaches the model as this tool call's
    own error result - and a real evaluation run proved that is not enough:
    the model saw five halts spread across a session and still reported
    "Task completed successfully" with zero mention of any interruption
    (`.amplifier/evaluation/computer-use/20260802T113341Z/s2-interrupt-halt/`).
    An isolated tool-error deep in a long transcript is easy for a model to
    fail to surface in a summary written many turns later - especially once
    other, successful actions follow it.

    The fix used here is the kernel's own mechanism (`inject_context`,
    `HOOKS_API.md`), not a second one built on top of it - exactly what
    `coexistence.md` \u00a78.3 already prescribes for pause and asks not be
    reinvented for halt: "Use the kernel's existing mechanism; do not build
    a second one." `ComputerTool.execute()` records every `HaltedError` it
    sees into `computer.halt_notices` (`tool-computer-use/__init__.py`);
    this handler fires on every `tool:post` for `computer`/`desktop` and, as
    long as that list is non-empty, injects a fresh system-role reminder
    that the model cannot avoid seeing on its very next turn - repeated on
    every subsequent tool call for the rest of the session, not just once,
    so it is still there no matter how many more actions happen before the
    model writes its final response. `ephemeral=True` mirrors the same
    reminder pattern the kernel already uses for the task-list nudge every
    turn: a fact that must be fresh on every turn, not one more permanent
    message bloating history.
    """

    async def handler(event: str, data: dict[str, Any]) -> HookResult:
        tool_name = (data or {}).get("tool_name")
        if tool_name not in ("computer", "desktop"):
            return HookResult(action="continue")
        try:
            tool = coordinator.get("tools", tool_name)
        except Exception:  # noqa: BLE001 - a lookup failure must not break the turn
            return HookResult(action="continue")
        computer = tool if tool_name == "computer" else getattr(tool, "_computer", None)
        notices = getattr(computer, "halt_notices", None)
        if not notices:
            return HookResult(action="continue")
        latest = notices[-1]
        return HookResult(
            action="inject_context",
            context_injection=(
                "SAFETY NOTICE (computer-use human/agent coexistence guard): "
                f"{len(notices)} human-detected interruption(s) occurred during "
                "this driving session - a person at the machine produced input "
                "the agent did not generate, and writes were halted before the "
                f"next one (docs/designs/coexistence.md \u00a76.0). Most recent: "
                f"{latest['message']} You MUST explicitly acknowledge this "
                "interruption in any summary, report, or completion claim you "
                "give the user - never report unqualified success or that the "
                "task completed cleanly without mentioning it."
            ),
            context_injection_role="system",
            ephemeral=True,
        )

    return handler


async def mount(
    coordinator: Any, config: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Register the provider-request hook that enables native computer use."""
    cfg = config or {}
    max_inline = int(cfg.get("max_inline_screenshots", _DEFAULT_MAX_INLINE_IMAGES))
    # Image blocks are written as plain dicts so no `visibility: null` reaches the API.
    # Pydantic notices the field type mismatch and warns; the serialised bytes are correct.
    warnings.filterwarnings(
        "ignore", message=".*PydanticSerializationUnexpectedValue.*"
    )
    priority = int(cfg.get("priority", 50))

    async def handler(event: str, data: dict[str, Any]) -> HookResult:
        # Providers are guaranteed mounted by the time the loop asks one to run.
        name = (data or {}).get("provider")
        provider = None
        try:
            provider = coordinator.get("providers", name) if name else None
            if provider is None:
                mounted = coordinator.get("providers")
                if isinstance(mounted, dict) and mounted:
                    provider = next(iter(mounted.values()))
        except Exception:
            # This determines whether native computer-use ever engages for the
            # entire session. Previously logged at DEBUG only - invisible at any
            # normal log level, so a persistently failing lookup meant computer-use
            # silently never wrapped anything, with nothing in the logs to show it.
            logger.warning("computer-use: provider lookup failed", exc_info=True)
        if provider is None:
            _trace(f"handler: NO PROVIDER FOUND (name={name!r})")
            # Same reasoning: without this, "no provider found" was visible ONLY
            # via AMPLIFIER_COMPUTER_USE_TRACE, which is off by default. A session
            # that never finds a provider to wrap looks identical - in the normal
            # logs - to one that is working correctly.
            logger.warning(
                "computer-use: no provider found to wrap (requested=%r); "
                "screenshot inlining will not happen this turn",
                name,
            )
        else:
            _wrap_provider(provider, coordinator, max_inline)
        return HookResult(action="continue")

    coordinator.hooks.register(
        PROVIDER_REQUEST, handler, priority=priority, name="hook-computer-use"
    )
    coordinator.hooks.register(
        TOOL_PRE,
        _make_gate_handler(coordinator),
        priority=priority,
        name="hook-computer-use-gate",
    )
    coordinator.hooks.register(
        TOOL_POST,
        _make_halt_notice_handler(coordinator),
        priority=priority,
        name="hook-computer-use-halt-notice",
    )
    _trace(f"MOUNTED max_inline={max_inline}")
    logger.info("hook-computer-use mounted (max_inline_screenshots=%d)", max_inline)
    return {
        "name": "hook-computer-use",
        "version": __version__,
        "provides": ["native-computer-use-wire-format"],
        "description": (
            "Verifies native tool-spec passthrough is supported upstream and "
            "returns screenshots as image blocks"
        ),
    }
