"""Decisive test: does the hook actually put a NATIVE tool and a REAL image on the wire?

No mocks of our own code - only the coordinator and the provider are stand-ins, because
those are the two things we do not own. Everything under test is the real module.
"""

from __future__ import annotations

import asyncio
import base64
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "modules" / "tool-computer-use"))
sys.path.insert(0, str(ROOT / "modules" / "hook-computer-use"))

import amplifier_module_hook_computer_use as hook_mod
import amplifier_module_tool_computer_use as tool_mod
from amplifier_core.message_models import ChatRequest, ToolSpec

PNG_1PX = base64.standard_b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


class FakeHooks:
    def __init__(self) -> None:
        self.handlers: dict[str, object] = {}

    def register(self, event, handler, priority=100, name=None):
        self.handlers[event] = handler


class FakeCoordinator:
    def __init__(self, tools: dict) -> None:
        self._tools = tools
        self.hooks = FakeHooks()

    def get(self, mount_point, name=None):
        if mount_point == "orchestrator":
            # No orchestrator mounted in this harness - the hook must treat
            # that as "cannot verify", not "confirmed incompatible" (see
            # `_fail_if_native_tool_passthrough_unsupported`).
            return None
        if mount_point != "tools":
            return {"anthropic": FAKE_PROVIDER} if name is None else FAKE_PROVIDER
        return self._tools if name is None else self._tools.get(name)

    async def mount(self, mount_point, module, name=None):
        self._tools[name] = module


class anthropic_provider:
    """Stand-in for the real provider-anthropic instance.

    Includes a real, working `_derive_native_tool_betas` (amplifier-module-
    provider-anthropic PR #79 shape) so this fake passes
    `_fail_if_native_tool_passthrough_unsupported`'s compatibility probe - the
    hook itself no longer promotes tools or injects beta headers, so a fake
    lacking this method would (correctly) make the hook refuse to wrap it.
    """

    _NATIVE_TOOL_BETA_HEADERS = {
        "computer_20241022": "computer-use-2024-10-22",
        "computer_20250124": "computer-use-2025-01-24",
        "computer_20251124": "computer-use-2025-11-24",
    }

    def __init__(self) -> None:
        self.seen: ChatRequest | None = None
        self._beta_headers: list[str] = []
        self._default_headers: dict[str, str] = {}

    def _derive_native_tool_betas(self, tools):
        betas: list[str] = []
        for tool in tools or []:
            if not isinstance(tool, dict):
                continue
            beta = self._NATIVE_TOOL_BETA_HEADERS.get(tool.get("type") or "")
            if beta and beta not in betas:
                betas.append(beta)
        return betas

    async def complete(self, request, **kwargs):
        self.seen = request
        return "ok"


FAKE_PROVIDER = anthropic_provider()


async def main() -> int:
    failures: list[str] = []

    def check(label: str, ok: bool, detail: str = "") -> None:
        print(
            f"  {'PASS' if ok else 'FAIL'}  {label}{'  <- ' + detail if detail and not ok else ''}"
        )
        if not ok:
            failures.append(label)

    # --- real tool, real hook, fake coordinator -------------------------------
    coord = FakeCoordinator({})
    await tool_mod.mount(coord, {"max_edge": 1280})
    await hook_mod.mount(coord, {"max_inline_screenshots": 2})
    tool = coord.get("tools", "computer")

    print("\n[1] tool advertises a native Anthropic tool definition")
    spec = tool.native_tool_spec
    check(
        "type is a computer_* server tool",
        str(spec.get("type", "")).startswith("computer_"),
        str(spec),
    )
    check("name is exactly 'computer'", spec.get("name") == "computer", str(spec))
    check(
        "display dims present and even",
        spec["display_width_px"] % 2 == 0 and spec["display_height_px"] > 0,
        str(spec),
    )
    print(f"       -> {spec}")

    # --- fire the hook so it wraps the provider -------------------------------
    print("\n[2] hook wraps the provider on provider:request")
    handler = coord.hooks.handlers.get("provider:request")
    check("handler registered on provider:request", handler is not None)
    assert handler is not None
    await handler("provider:request", {"provider": "anthropic"})
    check(
        "provider marked as wrapped",
        getattr(FAKE_PROVIDER, "_amplifier_computer_use_wrapped", False),
    )

    # --- build a request exactly as the orchestrator would --------------------
    shot = tool_mod.SHOT_DIR / "wire-test.png"
    shot.parent.mkdir(parents=True, exist_ok=True)
    shot.write_bytes(PNG_1PX)
    marker = json.dumps(
        {tool_mod.MARKER: 1, "text": "screenshot captured", "images": [str(shot)]}
    )

    request = ChatRequest(
        messages=[
            {"role": "user", "content": "what is on my screen"},
            {
                "role": "tool",
                "name": "computer",
                "tool_call_id": "t1",
                "content": marker,
            },
        ],
        tools=[
            ToolSpec(
                name="computer", description="desktop", parameters=tool.input_schema
            ),
            ToolSpec(
                name="read_file", description="read", parameters={"type": "object"}
            ),
        ],
    )
    await FAKE_PROVIDER.complete(request)
    sent = FAKE_PROVIDER.seen
    assert sent is not None

    print(
        "\n[3] hook no longer touches request.tools (native promotion now happens "
        "upstream, in the orchestrator's ToolSpec construction - see "
        "amplifier-module-loop-streaming PR #36)"
    )
    check("tools list is the exact same object", sent.tools is request.tools)
    check("tool count unchanged", len(sent.tools) == 2, str(sent.tools))
    check("computer tool untouched", sent.tools[0].name == "computer")
    check(
        "still a plain function tool (this test builds the request directly, "
        "bypassing the orchestrator's own promotion)",
        getattr(sent.tools[0], "type", None) in (None, "function"),
    )
    check("other tools untouched", sent.tools[1].name == "read_file")

    print(
        "\n[4] hook no longer injects anthropic-beta headers (provider derives them "
        "itself - see amplifier-module-provider-anthropic PR #79)"
    )
    check(
        "_beta_headers left exactly as the provider set it up (empty)",
        FAKE_PROVIDER._beta_headers == [],
        str(FAKE_PROVIDER._beta_headers),
    )

    print("\n[5] screenshot marker became a REAL image content block")
    tool_msg = sent.messages[1]
    content = hook_mod._read_content(tool_msg)
    check(
        "content is a block list, not a string",
        isinstance(content, list),
        type(content).__name__,
    )
    if isinstance(content, list):
        kinds = [b.get("type") for b in content]
        check("contains an image block", "image" in kinds, str(kinds))
        img = next((b for b in content if b.get("type") == "image"), None)
        if img:
            src = img.get("source", {})
            check(
                "base64 png source",
                src.get("type") == "base64" and src.get("media_type") == "image/png",
                str(src),
            )
            check(
                "payload matches the file on disk",
                base64.standard_b64decode(src["data"]) == PNG_1PX,
            )
    check("original marker JSON is gone", tool_mod.MARKER not in json.dumps(content))

    print("\n[5b] what the PROVIDER actually serialises (model_dump) is API-clean")
    dumped_msg = tool_msg.model_dump() if hasattr(tool_msg, "model_dump") else tool_msg
    dc = dumped_msg.get("content")
    check("survives model_dump as a block list", isinstance(dc, list), str(type(dc)))
    if isinstance(dc, list):
        keys = sorted({k for b in dc for k in b})
        check(
            "no 'visibility' key (API rejects it)", "visibility" not in keys, str(keys)
        )
        img2 = next((b for b in dc if b.get("type") == "image"), None)
        check("image block intact after dump", bool(img2 and img2["source"]["data"]))
        print(f"       -> block keys: {keys}")

    print("\n[6] recency window drops stale screenshots")
    many = [
        {"role": "tool", "name": "computer", "tool_call_id": f"t{i}", "content": marker}
        for i in range(5)
    ]
    out = hook_mod._expand_tool_results(many, max_inline=2)
    inlined = sum(1 for m in out if isinstance(hook_mod._read_content(m), list))
    check("only the newest 2 kept inline", inlined == 2, f"got {inlined}")
    check("oldest collapsed to text", isinstance(hook_mod._read_content(out[0]), str))

    print("\n[7] degrades safely when a screenshot file is gone")
    gone = json.dumps(
        {
            tool_mod.MARKER: 1,
            "text": "screenshot captured",
            "images": ["/nonexistent/x.png"],
        }
    )
    out = hook_mod._expand_tool_results(
        [{"role": "tool", "content": gone}], max_inline=3
    )
    check(
        "falls back to text, no crash", isinstance(hook_mod._read_content(out[0]), str)
    )

    print(
        "\n[8] unwraps the orchestrator's ToolResult envelope (the real shape on the wire)"
    )
    envelope = json.dumps({"error": None, "output": marker})
    shot.write_bytes(PNG_1PX)
    out = hook_mod._expand_tool_results(
        [{"role": "tool", "content": envelope}], max_inline=3
    )
    c = hook_mod._read_content(out[0])
    check("envelope-wrapped marker is detected", isinstance(c, list), str(c)[:100])
    if isinstance(c, list):
        check(
            "image extracted from envelope",
            any(b.get("type") == "image" for b in c),
            str([b.get("type") for b in c]),
        )

    shot.unlink(missing_ok=True)
    print(
        "\n"
        + ("ALL WIRE-FORMAT CHECKS PASSED" if not failures else f"FAILURES: {failures}")
    )
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
