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

from amplifier_core.message_models import ChatRequest, ToolSpec  # noqa: E402

import amplifier_module_hook_computer_use as hook_mod  # noqa: E402
import amplifier_module_tool_computer_use as tool_mod  # noqa: E402

PNG_1PX = base64.standard_b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


class FakeHooks:
    def __init__(self) -> None:
        self.handlers: dict[str, object] = {}

    def register(self, event, handler, priority=100, name=None):  # noqa: ANN001
        self.handlers[event] = handler


class FakeCoordinator:
    def __init__(self, tools: dict) -> None:
        self._tools = tools
        self.hooks = FakeHooks()

    def get(self, mount_point, name=None):  # noqa: ANN001
        if mount_point != "tools":
            return {"anthropic": FAKE_PROVIDER} if name is None else FAKE_PROVIDER
        return self._tools if name is None else self._tools.get(name)

    async def mount(self, mount_point, module, name=None):  # noqa: ANN001
        self._tools[name] = module


class anthropic_provider:  # noqa: N801 - module name must contain "anthropic" for detection
    def __init__(self) -> None:
        self.seen: ChatRequest | None = None
        self._beta_headers: list[str] = []
        self._default_headers: dict[str, str] = {}

    async def complete(self, request, **kwargs):  # noqa: ANN001
        self.seen = request
        return "ok"


FAKE_PROVIDER = anthropic_provider()


async def main() -> int:
    failures: list[str] = []

    def check(label: str, ok: bool, detail: str = "") -> None:
        print(f"  {'PASS' if ok else 'FAIL'}  {label}{'  <- ' + detail if detail and not ok else ''}")
        if not ok:
            failures.append(label)

    # --- real tool, real hook, fake coordinator -------------------------------
    coord = FakeCoordinator({})
    await tool_mod.mount(coord, {"max_edge": 1280})
    await hook_mod.mount(coord, {"max_inline_screenshots": 2})
    tool = coord.get("tools", "computer")

    print("\n[1] tool advertises a native Anthropic tool definition")
    spec = tool.native_tool_spec
    check("type is a computer_* server tool", str(spec.get("type", "")).startswith("computer_"), str(spec))
    check("name is exactly 'computer'", spec.get("name") == "computer", str(spec))
    check("display dims present and even", spec["display_width_px"] % 2 == 0 and spec["display_height_px"] > 0, str(spec))
    print(f"       -> {spec}")

    # --- fire the hook so it wraps the provider -------------------------------
    print("\n[2] hook wraps the provider on provider:request")
    handler = coord.hooks.handlers.get("provider:request")
    check("handler registered on provider:request", handler is not None)
    assert handler is not None
    await handler("provider:request", {"provider": "anthropic"})
    check("provider marked as wrapped", getattr(FAKE_PROVIDER, "_amplifier_computer_use_wrapped", False))

    # --- build a request exactly as the orchestrator would --------------------
    shot = tool_mod.SHOT_DIR / "wire-test.png"
    shot.parent.mkdir(parents=True, exist_ok=True)
    shot.write_bytes(PNG_1PX)
    marker = json.dumps({tool_mod.MARKER: 1, "text": "screenshot captured", "images": [str(shot)]})

    request = ChatRequest(
        messages=[
            {"role": "user", "content": "what is on my screen"},
            {"role": "tool", "name": "computer", "tool_call_id": "t1", "content": marker},
        ],
        tools=[
            ToolSpec(name="computer", description="desktop", parameters=tool.input_schema),
            ToolSpec(name="read_file", description="read", parameters={"type": "object"}),
        ],
    )
    await FAKE_PROVIDER.complete(request)
    sent = FAKE_PROVIDER.seen
    assert sent is not None

    print("\n[3] tools array carries the NATIVE definition (not a function schema)")
    first = sent.tools[0]
    dumped = first.model_dump(exclude_none=True)
    check("native tool is first (avoids cache_control stamping)", dumped.get("name") == "computer", str(dumped))
    check("serialises to server tool type", str(dumped.get("type", "")).startswith("computer_"), str(dumped))
    check("no 'parameters' key on the wire (API rejects it)", "parameters" not in dumped, str(dumped))
    check("provider sees .type != function", getattr(first, "type", None) not in (None, "function"))
    check("other tools untouched", sent.tools[1].name == "read_file")
    print(f"       -> {dumped}")

    print("\n[4] beta header enabled on the provider")
    check(
        "anthropic-beta contains a computer-use flag",
        any("computer-use" in h for h in FAKE_PROVIDER._beta_headers),
        str(FAKE_PROVIDER._beta_headers),
    )
    print(f"       -> {FAKE_PROVIDER._beta_headers}")

    print("\n[5] screenshot marker became a REAL image content block")
    tool_msg = sent.messages[1]
    content = hook_mod._read_content(tool_msg)
    check("content is a block list, not a string", isinstance(content, list), type(content).__name__)
    if isinstance(content, list):
        kinds = [b.get("type") for b in content]
        check("contains an image block", "image" in kinds, str(kinds))
        img = next((b for b in content if b.get("type") == "image"), None)
        if img:
            src = img.get("source", {})
            check("base64 png source", src.get("type") == "base64" and src.get("media_type") == "image/png", str(src))
            check("payload matches the file on disk", base64.standard_b64decode(src["data"]) == PNG_1PX)
    check("original marker JSON is gone", tool_mod.MARKER not in json.dumps(content))

    print("\n[5b] what the PROVIDER actually serialises (model_dump) is API-clean")
    dumped_msg = tool_msg.model_dump() if hasattr(tool_msg, "model_dump") else tool_msg
    dc = dumped_msg.get("content")
    check("survives model_dump as a block list", isinstance(dc, list), str(type(dc)))
    if isinstance(dc, list):
        keys = sorted({k for b in dc for k in b})
        check("no 'visibility' key (API rejects it)", "visibility" not in keys, str(keys))
        img2 = next((b for b in dc if b.get("type") == "image"), None)
        check("image block intact after dump", bool(img2 and img2["source"]["data"]))
        print(f"       -> block keys: {keys}")

    print("\n[6] recency window drops stale screenshots")
    many = [{"role": "tool", "name": "computer", "tool_call_id": f"t{i}", "content": marker} for i in range(5)]
    out = hook_mod._expand_tool_results(many, max_inline=2)
    inlined = sum(1 for m in out if isinstance(hook_mod._read_content(m), list))
    check("only the newest 2 kept inline", inlined == 2, f"got {inlined}")
    check("oldest collapsed to text", isinstance(hook_mod._read_content(out[0]), str))

    print("\n[7] degrades safely when a screenshot file is gone")
    gone = json.dumps({tool_mod.MARKER: 1, "text": "screenshot captured", "images": ["/nonexistent/x.png"]})
    out = hook_mod._expand_tool_results([{"role": "tool", "content": gone}], max_inline=3)
    check("falls back to text, no crash", isinstance(hook_mod._read_content(out[0]), str))

    print("\n[8] unwraps the orchestrator's ToolResult envelope (the real shape on the wire)")
    envelope = json.dumps({"error": None, "output": marker})
    shot.write_bytes(PNG_1PX)
    out = hook_mod._expand_tool_results([{"role": "tool", "content": envelope}], max_inline=3)
    c = hook_mod._read_content(out[0])
    check("envelope-wrapped marker is detected", isinstance(c, list), str(c)[:100])
    if isinstance(c, list):
        check("image extracted from envelope", any(b.get("type") == "image" for b in c), str([b.get("type") for b in c]))

    shot.unlink(missing_ok=True)
    print("\n" + ("ALL WIRE-FORMAT CHECKS PASSED" if not failures else f"FAILURES: {failures}"))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
