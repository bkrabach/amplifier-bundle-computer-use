"""Reproduces the actual defect, using the REAL amplifier_core loader/validator
code - not a hand-rolled stand-in for it.

`amplifier_core`'s loader calls every tool module's `mount()` TWICE for every
real session:

  CALL 1 - a throwaway protocol-compliance probe, run from
  `_session_init.py` -> `loader.load()` -> `loader._validate_module()` ->
  `ToolValidator.validate()` -> `ToolValidator._check_protocol_compliance()`
  (`amplifier_core/validation/tool.py`). This constructs a fresh
  `MockCoordinator()`, calls `await mount_fn(coordinator, actual_config)`,
  inspects what got mounted, and tears the result down in a `finally` block
  a few lines later. Its own module docstring says so: "Check 4: Protocol
  compliance (requires calling mount)".

  CALL 2 - the real mount, run moments later from `loader.load()`'s
  `mount_with_config_ep` closure (`return await fn(coordinator, config or {})`),
  against the orchestrator's real coordinator.

Before this fix, `mount()` itself built the session-start disclosure
(`_build_announcement`) - so CALL 1 alone showed a real dialog to a real
human, for a `ComputerTool`/`CoexistenceGuard` pair that was thrown away a
moment later, and CALL 2 could show a SECOND dialog for the actual session
(or reuse/misapply the first's decision - see docs/designs/coexistence.md
and the 6594b63 commit this defect survived). The fix moves the disclosure
to `ComputerTool._ensure_announced`, called from `execute()` - a validation
probe never calls `execute()`, only `mount()`.

These tests use the REAL `ToolValidator` from the installed `amplifier_core`
dependency for CALL 1 (not a substitute), and this module's REAL `mount()`
for both calls - only `_build_announcement` itself is replaced with a spy, so
the test can count invocations without needing a real display, subprocess,
or human.
"""

from __future__ import annotations

import asyncio
import base64
import io
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "modules" / "tool-computer-use"))

import amplifier_module_tool_computer_use as cu
from amplifier_core.testing import MockCoordinator
from amplifier_core.validation.base import ValidationResult
from amplifier_core.validation.tool import ToolValidator
from amplifier_module_tool_computer_use.backend import BackendError, ScreenGeometry
from amplifier_module_tool_computer_use.coexistence_guard import CoexistenceGuard
from amplifier_module_tool_computer_use.presence import PresenceMonitor


def _guard(platform: str = "linux-x11", idle_ms: float = 999_999.0) -> CoexistenceGuard:
    presence = PresenceMonitor(idle_source=lambda: idle_ms, platform=platform)
    return CoexistenceGuard(presence=presence, release_all=lambda reason: [])


def _tiny_png_b64() -> str:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (2, 2)).save(buf, format="PNG")
    return base64.standard_b64encode(buf.getvalue()).decode()


_TINY_PNG_B64 = _tiny_png_b64()


class _FakeLocalBackend:
    """A local (non-remote) backend shaped just enough for `mount()` and a
    real `screenshot` action to run end to end without a real display."""

    is_remote = False
    name = "fake-local"

    def __init__(self) -> None:
        self.closed = False

    def type_text(self, text: str) -> None:
        """`ComputerTool.__init__` inspects this signature."""

    def screen_geometry(self) -> ScreenGeometry:
        return ScreenGeometry(width=1920, height=1080)

    def list_monitors(self):
        raise BackendError("no monitor enumeration on this fake")

    def capture_scaled(self, region, size, max_edge, max_pixels) -> str:
        return _TINY_PNG_B64

    def get_clipboard(self) -> str:
        return ""

    def close(self) -> None:
        self.closed = True


class _RecordingCoordinator(MockCoordinator):
    """The REAL `MockCoordinator` (same class `_check_protocol_compliance`
    itself constructs), subclassed only to make the mounted tool instances
    easy to grab back out for the "first real use" step below."""

    def __init__(self) -> None:
        super().__init__()
        self.tools_by_name: dict[str, object] = {}

    async def mount(self, mount_point, module, name=None):
        await super().mount(mount_point, module, name=name)
        if mount_point == "tools":
            self.tools_by_name[name or module.name] = module


def test_real_loader_double_mount_never_triggers_the_announcement(monkeypatch):
    """FAILS WITHOUT THE FIX: before the disclosure moved out of `mount()`,
    CALL 1 alone (the throwaway protocol-compliance probe) reached
    `_build_announcement` - a real dialog for a session that was never
    going to drive anything."""
    calls: list[str] = []

    def _spy_announce(*_a, **_k):
        calls.append("announced")
        return None

    monkeypatch.setattr(cu, "_build_announcement", _spy_announce)
    monkeypatch.setattr(cu, "select_backend", lambda cfg: _FakeLocalBackend())
    monkeypatch.setattr(
        cu, "_build_coexistence_guard", lambda backend, cfg: _guard("linux-x11")
    )

    cfg = {"read_only": True}

    # -- CALL 1: the REAL amplifier_core protocol-compliance probe ----------
    result = ValidationResult(module_type="tool", module_path="fake")
    asyncio.get_event_loop().run_until_complete(
        ToolValidator()._check_protocol_compliance(result, cu.mount, config=cfg)
    )
    assert result.passed, (
        f"the probe itself must still pass validation: {result.summary()}"
    )
    assert calls == [], (
        "CALL 1 (the throwaway protocol-compliance probe) must NEVER reach "
        "_build_announcement - this is the exact defect: a discarded mount() "
        "showing a real dialog to a real human"
    )

    # -- CALL 2: the real mount, exactly what loader.mount_with_config_ep ----
    # does (`return await fn(coordinator, config or {})`) - same raw `cu.mount`
    # function, same config, a second time.
    coordinator = _RecordingCoordinator()
    asyncio.get_event_loop().run_until_complete(cu.mount(coordinator, cfg))
    assert calls == [], (
        "CALL 2 (the real mount()) must also never reach _build_announcement "
        "directly - mount() no longer builds the disclosure at all"
    )
    assert set(coordinator.tools_by_name) == {"computer", "desktop"}

    # -- First real use: THIS is the one and only place the disclosure may
    # fire - and it must actually fire here.
    computer_tool = coordinator.tools_by_name["computer"]
    exec_result = asyncio.get_event_loop().run_until_complete(
        computer_tool.execute({"action": "screenshot"})
    )
    assert exec_result.success is True
    assert calls == ["announced"], (
        "the session's first real execute() must trigger the disclosure "
        "exactly once - neither mount() call did, and this call must"
    )

    # -- A second real action must reuse the decision, not ask again --------
    asyncio.get_event_loop().run_until_complete(
        computer_tool.execute({"action": "screenshot"})
    )
    assert calls == ["announced"], (
        "a second action on the same session must not re-trigger the disclosure"
    )


def test_desktop_tool_first_use_also_gated_not_only_computer(monkeypatch):
    """The `desktop` tool shares `ComputerTool._ensure_announced` with
    `computer` (see `DesktopTool.execute`) - proves the gate covers a
    `desktop`-only session too, including an action (`get_clipboard`) that
    never reaches `ComputerTool._run()` at all - it calls
    `backend.get_clipboard()` directly (see `DesktopTool.execute`'s
    `get_clipboard` branch). A clipboard read is exactly the kind of
    sensitive access this module's own docs single out (a just-copied
    password, an unseen paste buffer) - it must not be reachable without
    disclosure just because it happens to skip `_run()`."""
    calls: list[str] = []
    monkeypatch.setattr(
        cu, "_build_announcement", lambda *_a, **_k: calls.append("x") or None
    )
    monkeypatch.setattr(cu, "select_backend", lambda cfg: _FakeLocalBackend())
    monkeypatch.setattr(
        cu, "_build_coexistence_guard", lambda backend, cfg: _guard("linux-x11")
    )

    coordinator = _RecordingCoordinator()
    asyncio.get_event_loop().run_until_complete(
        cu.mount(coordinator, {"clipboard_read_policy": "allow"})
    )
    assert calls == [], "mount() must not announce for the desktop tool either"

    desktop_tool = coordinator.tools_by_name["desktop"]
    result = asyncio.get_event_loop().run_until_complete(
        desktop_tool.execute({"action": "get_clipboard"})
    )
    assert result.success is True
    assert calls == ["x"], (
        "desktop's first action (get_clipboard, which never touches "
        "ComputerTool._run()) must still trigger the disclosure"
    )
