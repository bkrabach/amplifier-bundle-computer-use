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


def _parse_marker(content: Any) -> dict[str, Any] | None:
    """Return the computer-use payload if this tool content carries one.

    The orchestrator does not hand our ``ToolResult`` straight to the provider - it
    serialises it into an envelope, so the real payload arrives as a JSON *string*
    nested under ``output``::

        {"error": null, "output": "{\\"__amplifier_computer_use__\\": 1, ...}"}

    Unwrap whatever shape shows up rather than assuming one: envelope, bare payload,
    or already-decoded dict.
    """
    if isinstance(content, dict):
        return content if MARKER in content else _parse_marker(content.get("output"))
    if not isinstance(content, str) or MARKER not in content:
        return None
    try:
        data = json.loads(content)
    except (json.JSONDecodeError, TypeError):
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
        if budget > 0:
            for path in images:
                block = _image_block(path)
                if block is not None:
                    blocks.append(block)
            if blocks:
                budget -= 1
        if blocks:
            rewritten.append(
                _with_content(msg, [{"type": "text", "text": text}, *blocks])
            )
        else:
            note = (
                " [image dropped: superseded by a newer screenshot]" if images else ""
            )
            rewritten.append(_with_content(msg, f"{text}{note}"))
    rewritten.reverse()
    return rewritten


def _promote_tools(coordinator: Any, tools: list[Any]) -> tuple[list[Any], list[str]]:
    """Swap function-shaped specs for native ones where the tool asks for it."""
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
        except Exception:  # noqa: BLE001 - tool lookup is best-effort
            tool = None
        if tool is not None and hasattr(tool, "native_tool_spec"):
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
    existing = getattr(provider, "_beta_headers", None)
    if existing is None or not isinstance(existing, list):
        return
    for header in headers:
        if header not in existing:
            existing.append(header)
            logger.info("computer-use: enabled anthropic-beta %s", header)
    default = getattr(provider, "_default_headers", None)
    if isinstance(default, dict) and existing:
        default["anthropic-beta"] = ",".join(existing)


def _wrap_provider(provider: Any, coordinator: Any, max_inline: int) -> bool:
    if getattr(provider, _WRAPPED_FLAG, False):
        return False
    if not _is_anthropic(provider):
        logger.info(
            "computer-use: provider %s is not Anthropic; native tool not enabled",
            type(provider).__name__,
        )
        return False
    if not hasattr(provider, "complete"):
        return False

    original = provider.complete

    async def complete(request: Any, **kwargs: Any):
        try:
            tools = list(getattr(request, "tools", None) or [])
            if tools:
                promoted, betas = _promote_tools(coordinator, tools)
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
            logger.debug("computer-use: provider lookup failed", exc_info=True)
        if provider is None:
            _trace(f"handler: NO PROVIDER FOUND (name={name!r})")
        else:
            _wrap_provider(provider, coordinator, max_inline)
        return HookResult(action="continue")

    coordinator.hooks.register(
        PROVIDER_REQUEST, handler, priority=priority, name="hook-computer-use"
    )
    _trace(f"MOUNTED max_inline={max_inline}")
    logger.info("hook-computer-use mounted (max_inline_screenshots=%d)", max_inline)
    return {
        "name": "hook-computer-use",
        "version": __version__,
        "provides": ["native-computer-use-wire-format"],
        "description": "Promotes native tool specs and returns screenshots as image blocks",
    }
