"""Amplifier hook module: make Anthropic's NATIVE computer-use tool work end to end.

Two things stand between a mounted `computer` tool and real computer use, and both
live in the orchestrator, which we do not want to fork:

1. The orchestrator builds every `ToolSpec` as ``name``/``description``/``parameters``,
   so a tool cannot declare itself as a *server-side* Anthropic tool type.
2. Tool results are collapsed to ``str`` before they reach the provider, so a
   screenshot can never travel back as an image content block.

Both are fixed at a single seam: the provider's ``complete()`` call. This hook wraps it
and, on the way through:

* promotes any mounted tool exposing ``native_tool_spec`` to its native wire form,
* adds the required ``anthropic-beta`` header,
* expands screenshot markers in tool results into real base64 image blocks,
* keeps only the most recent screenshots inline, so long sessions stay affordable.

Nothing is forked, nothing is patched on disk, and removing the hook degrades the
tool cleanly back to an ordinary function tool.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import warnings
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from amplifier_core import HookResult
from amplifier_core.message_models import ToolSpec
from pydantic import Field

try:  # event name is a plain constant, but tolerate kernels that move it
    from amplifier_core.events import PROVIDER_REQUEST  # type: ignore[attr-defined]
except ImportError:  # pragma: no cover
    PROVIDER_REQUEST = "provider:request"

try:
    from amplifier_core.events import TOOL_PRE  # type: ignore[attr-defined]
except ImportError:  # pragma: no cover
    TOOL_PRE = "tool:pre"

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


class NativeToolSpec(ToolSpec):
    """A ToolSpec that serialises to a provider-native tool definition.

    The Anthropic provider passes any tool whose ``.type`` is not ``"function"``
    straight through via ``model_dump(exclude_none=True)``. Overriding ``model_dump``
    lets us emit exactly the server-side shape (no ``parameters`` key, which the API
    would reject) while remaining a genuine ``ToolSpec`` for anything upstream.
    """

    native_payload: dict[str, Any] = Field(default_factory=dict)
    type: str | None = None

    def model_dump(self, **_kwargs: Any) -> dict[str, Any]:  # type: ignore[override]
        return dict(self.native_payload)


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
            "is present. Wrapping this provider would silently do nothing: no native "
            "tool promotion, no screenshot inlining, no error, no log line. Refusing "
            "to operate rather than degrade invisibly. Fix: either wrap complete() AND "
            "stream() in this hook, or route computer-use through an orchestrator that "
            "does not prefer stream()."
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


def _promote_tools(
    coordinator: Any, tools: list[Any], model: str | None = None
) -> tuple[list[Any], list[str]]:
    """Swap function-shaped specs for native ones where the tool asks for it.

    `model` is the model actually about to receive THIS request
    (`ChatRequest.model`, when the caller can see it) - passed through to any
    tool exposing `note_model()` so its declared `native_tool_spec`/
    `native_beta_header` stay correct even when the model changes mid-session
    (e.g. `provider-anthropic`'s own model-fallback). See
    `tool_versions.resolve_tool_version` in tool-computer-use for why this
    fixes a real, reachable 400-every-turn defect rather than a hypothetical
    one.
    """
    if not tools:
        return tools, []
    native: list[Any] = []
    normal: list[Any] = []
    betas: list[str] = []
    for spec in tools:
        payload = None
        tool = None
        try:
            tool = coordinator.get("tools", getattr(spec, "name", ""))
        except Exception:
            # Best-effort: this loop runs over EVERY tool on the request, not just
            # `computer`, so a lookup failure for an unrelated tool is expected and
            # must not break promotion of the others. Logged (not silent) so a
            # persistent failure to find `computer` specifically is still
            # diagnosable instead of invisibly meaning "computer-use never
            # promotes" with zero trace.
            logger.debug(
                "computer-use: tool lookup failed for %r", spec.name, exc_info=True
            )
            tool = None
        # D3 fix: `native_tool_spec` is a property, and on some tools it can raise
        # (e.g. a backend I/O failure). `hasattr(tool, "native_tool_spec")` used to
        # gate this block - but Python 3's `hasattr` swallows *only* `AttributeError`;
        # any other exception raised by the property escapes `hasattr` itself, before
        # the `try/except` below even starts, and takes down the whole provider
        # request. Checking the *class* for the descriptor (never invokes the
        # getter - a property accessed via the class returns the descriptor object
        # itself) tells us whether the attribute exists at all, without ever calling
        # into a tool's code. Reading the actual value happens only inside the
        # `try/except` that already handles a broken tool.
        if (
            tool is not None
            and getattr(type(tool), "native_tool_spec", None) is not None
        ):
            # Model/tool_version coupling fix: `note_model` is a plain method
            # (not a property), so simply reading it via getattr can never
            # trigger the D3 hazard - only *calling* it can raise, and that is
            # already inside this function's broader try/except below. A tool
            # with no `note_model` (or any tool predating this fix) is
            # unaffected - `getattr(..., None)` degrades to a no-op.
            note_model = getattr(tool, "note_model", None)
            if callable(note_model):
                try:
                    note_model(model)
                except Exception:
                    logger.exception(
                        "computer-use: note_model(%r) failed for %r; "
                        "native_tool_spec may be stale this turn",
                        model,
                        spec.name,
                    )
            try:
                payload = dict(tool.native_tool_spec)
            except Exception:  # a broken tool must not break the request
                logger.exception(
                    "computer-use: could not read native_tool_spec from %r", spec.name
                )
                payload = None
        if payload:
            native.append(
                NativeToolSpec(
                    name=payload.get("name", spec.name),
                    parameters={},
                    description=getattr(spec, "description", None),
                    native_payload=payload,
                    type=payload.get("type"),
                )
            )
            header = getattr(tool, "native_beta_header", None)
            if header:
                betas.append(str(header))
        else:
            normal.append(spec)
    # Native tools go first: the provider stamps cache_control onto the LAST tool,
    # and server-side tool definitions must not carry it.
    return native + normal, betas


def _ensure_beta_headers(provider: Any, headers: list[str]) -> None:
    """Enable the given `anthropic-beta` flags on the already-mounted provider.

    Only `_beta_headers` is written here. `_default_headers` is deliberately NOT
    touched (it was, until 2026-07-31, and the write was dead code): that dict is
    baked into the Anthropic SDK client at provider *construction* time
    (`amplifier_module_provider_anthropic/__init__.py:686`,
    `AsyncAnthropic(..., default_headers=self._default_headers, ...)`), which runs
    long before this hook ever gets a chance to mutate the provider instance -
    `mount()` order guarantees the provider already exists by the time we see it.
    Writing to `_default_headers` after that point mutates a dict the already-built
    httpx client copied at init and never rereads; it has zero effect and was
    silently doing nothing on every single request.

    `_beta_headers` works for the opposite reason: it is RE-READ per request
    (`amplifier_module_provider_anthropic/__init__.py:1262-1273`,
    `_build_request_beta_headers`), not baked into a client at construction time.

    KNOWN PRIVATE-ATTRIBUTE DEPENDENCY (dated 2026-07-31): `_beta_headers` is not
    part of the `Provider` protocol - there is no public API for a hook to request
    additional beta headers for a request it did not originate. The correct
    long-term fix lives upstream, on amplifier-module-provider-anthropic: derive the
    required `anthropic-beta` values from the native tool *types* actually present
    on `request.tools` (the provider already inspects `request.tools` to build the
    wire payload), so no external hook needs to reach into a private attribute at
    all. Until that lands, this dependency is deliberate and documented, not an
    oversight - if `provider-anthropic` ever renames or removes `_beta_headers`,
    the `isinstance` guard below degrades this to a no-op again, which is why it is
    logged rather than left to fail silently a second time.
    """
    existing = getattr(provider, "_beta_headers", None)
    if existing is None or not isinstance(existing, list):
        logger.warning(
            "computer-use: provider %s has no writable _beta_headers list; "
            "cannot enable %s. Native computer-use tool calls will likely be "
            "rejected by the Anthropic API for missing the required beta opt-in.",
            type(provider).__name__,
            headers,
        )
        return
    for header in headers:
        if header not in existing:
            existing.append(header)
            logger.info("computer-use: enabled anthropic-beta %s", header)


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
    if not hasattr(provider, "complete"):
        return False

    original = provider.complete

    async def complete(request: Any, **kwargs: Any):
        try:
            tools = list(getattr(request, "tools", None) or [])
            if tools:
                promoted, betas = _promote_tools(
                    coordinator, tools, getattr(request, "model", None)
                )
                _trace(
                    f"complete: tools={[getattr(t, 'name', '?') for t in tools]} promoted_betas={betas}"
                )
                if betas:
                    request.tools = promoted
                    _ensure_beta_headers(provider, betas)
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
        "computer-use: wrapped provider %s for native computer-use",
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
                "native computer-use tool promotion will not happen this turn",
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
    _trace(f"MOUNTED max_inline={max_inline}")
    logger.info("hook-computer-use mounted (max_inline_screenshots=%d)", max_inline)
    return {
        "name": "hook-computer-use",
        "version": __version__,
        "provides": ["native-computer-use-wire-format"],
        "description": "Promotes native tool specs and returns screenshots as image blocks",
    }
