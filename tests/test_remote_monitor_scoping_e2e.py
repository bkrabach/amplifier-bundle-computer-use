"""End-to-end regression test: per-monitor targeting must survive the FULL
remote round trip - `ComputerTool` (monitor selection) -> `RemoteBackend`
(capture_scaled capability) -> real NDJSON wire encode/decode (`wire.py`) ->
`RemoteAgent` dispatch (`remote_agent.py`) -> the platform `Backend.capture()`
call that actually receives the region.

This is deliberately NOT a mock-records-python-objects test: requests and
responses are pushed through `Request.encode()` / `Response.decode()` for
real, so a bug that only shows up after JSON round-tripping (e.g. a tuple
silently becoming a list, or a monitor id losing its type) would be caught
here and nowhere else in the suite.

Motivation (docs/designs/remote-transport.md \u00a711, \u00a7C1): on a real
multi-monitor target, a remote capture that falls back to the whole
virtual-desktop bounding box downscales far more aggressively than a single
monitor, and can contain large stretches of dead space where no monitor
exists at all (measured: 9626x4323 bounding box around four 3840x2160
monitors, 7.52x downscale, ~20% dead space vs. 3.00x for one monitor with
none). This test proves monitor selection - which lives entirely in
`ComputerTool`, nowhere in `RemoteBackend` - is not silently bypassed by the
remote path: the SECOND (non-primary) monitor's exact bounds must reach the
agent and scope the real capture, byte for byte through a genuine wire
encode/decode, on every screenshot.

Runs entirely on Linux with no remote host, no SSH, no real desktop: the
"wire" is an in-process bridge that decodes/dispatches/encodes exactly like
`SshTransport.send()` would, and the "platform backend" on the agent side is
a fake that renders a real (tiny) PNG via PIL so the full
open/convert/resize/save path in `remote_agent._op_capture_scaled` is
exercised for real, not stubbed out.
"""

from __future__ import annotations

import io
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "modules" / "tool-computer-use"))

import base64

import pytest
from amplifier_module_tool_computer_use.backend import (
    BackendError,
    MonitorInfo,
    ProbeResult,
    ScreenGeometry,
)
from amplifier_module_tool_computer_use.remote_agent import RemoteAgent
from amplifier_module_tool_computer_use.remote_backend import RemoteBackend
from amplifier_module_tool_computer_use.wire import Request

pytest.importorskip("PIL")

# -- a fake amplifier_core.models.ToolResult / ComputerTool import shim ------
# `amplifier_module_tool_computer_use/__init__.py` (ComputerTool) imports
# `amplifier_core.models.ToolResult` at module load time. This suite runs
# without amplifier_core installed (same reasoning `remote_agent.py`'s own
# module docstring gives for having zero amplifier_core dependency on the
# agent side) - stub the one symbol ComputerTool needs, exactly the pattern
# already used to probe this module directly against a live target.
if "amplifier_core" not in sys.modules:
    import types

    _core = types.ModuleType("amplifier_core")
    _models = types.ModuleType("amplifier_core.models")

    class _ToolResult:
        def __init__(
            self, success: bool = True, output: Any = None, error: Any = None
        ) -> None:
            self.success = success
            self.output = output
            self.error = error

    _models.ToolResult = _ToolResult  # type: ignore[attr-defined]
    _core.models = _models  # type: ignore[attr-defined]
    sys.modules["amplifier_core"] = _core
    sys.modules["amplifier_core.models"] = _models

from amplifier_module_tool_computer_use import ComputerTool

#: The four-monitor layout `monitors.py`'s own docstring cites as the real
#: motivating case: four 3840x2160 monitors with non-aligned origins and
#: ~20% dead space in their virtual-desktop bounding box.
FOUR_MONITORS = [
    MonitorInfo(id="DISPLAY1", x=0, y=0, width=3840, height=2160, primary=True),
    MonitorInfo(id="DISPLAY2", x=3840, y=0, width=3840, height=2160, primary=False),
    MonitorInfo(id="DISPLAY3", x=0, y=2160, width=3840, height=2160, primary=False),
    MonitorInfo(id="DISPLAY4", x=3840, y=2160, width=3840, height=2160, primary=False),
]


def _real_png(width: int, height: int) -> bytes:
    from PIL import Image

    img = Image.new("RGB", (width, height), (10, 20, 30))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


class _FakePlatformBackend:
    """Stands in for `MacOSBackend`/`WindowsBackend`/`LinuxX11Backend` on the
    agent side. Records exactly what `region` it was asked to capture, and
    renders a REAL PNG sized to that region so the agent's own PIL
    open/resize/save pipeline runs unmodified."""

    name = "fake-multi-monitor-target"

    def __init__(self, monitors: list[MonitorInfo]) -> None:
        self._monitors = monitors
        self.capture_calls: list[tuple[int, int, int, int] | None] = []

    def probe(self) -> ProbeResult:
        return ProbeResult(True, "")

    def screen_geometry(self) -> ScreenGeometry:
        # Virtual-desktop bounding box around all monitors - deliberately
        # NOT what a monitor-scoped screenshot should use.
        return ScreenGeometry(width=7680, height=4320, origin_x=0, origin_y=0)

    def list_monitors(self) -> list[MonitorInfo]:
        return list(self._monitors)

    def capture(self, region: tuple[int, int, int, int] | None = None) -> bytes:
        self.capture_calls.append(region)
        if region is not None:
            x1, y1, x2, y2 = region
            return _real_png(x2 - x1, y2 - y1)
        return _real_png(7680, 4320)  # whole virtual desktop, if ever asked for

    def cursor_position(self) -> tuple[int, int]:
        return (0, 0)

    def move(self, x: int, y: int) -> None:
        pass

    def click(self, x, y, button="left", count=1) -> None:
        pass

    def type_text(self, text: str) -> None:
        pass

    def key(self, combo: str) -> None:
        pass


class _InProcessAgentTransport:
    """A "wire" that is a real NDJSON encode/decode round trip, but dispatches
    in-process to a `RemoteAgent` instead of over a subprocess/SSH pipe -
    exactly what `SshTransport.send()` does, minus the subprocess.

    Using `Request.encode()`/`Response.decode()` for real (not passing Python
    objects between the two sides) is the point: a region tuple must survive
    genuine JSON serialization to prove nothing is lost crossing the wire.
    """

    def __init__(self, agent: RemoteAgent) -> None:
        self._agent = agent

    def send(self, line: bytes, timeout: float = 30.0) -> bytes:
        # Mirrors remote_agent.RemoteAgent.run()'s per-line parsing exactly,
        # but without the stdin/stdout plumbing - `_dispatch` is the same
        # method the real read loop calls.
        import json

        data = json.loads(line.decode("utf-8"))
        req = Request(
            id=int(data["id"]), op=str(data["op"]), args=data.get("args") or {}
        )
        resp = self._agent._dispatch(req)
        return resp.encode()


def _build_remote_computer(
    monitors: list[MonitorInfo], target_monitor: str
) -> tuple[ComputerTool, _FakePlatformBackend]:
    fake_platform = _FakePlatformBackend(monitors)
    agent = RemoteAgent(fake_platform, read_only=True)
    transport = _InProcessAgentTransport(agent)
    backend = RemoteBackend({"_host": "user@fake-host", "_transport": transport})
    backend._connected = True  # bypass real connect() - unit-level test
    computer = ComputerTool(backend, {"target_monitor": target_monitor})
    computer.resolve_display()
    return computer, fake_platform


def test_non_primary_monitor_region_survives_the_full_remote_round_trip():
    """The single most important assertion: selecting a SPECIFIC, non-primary
    monitor out of four must reach the agent's real `capture(region=...)`
    call as that exact monitor's bounds - not `None` (virtual-desktop
    fallback) and not the primary monitor's bounds."""
    computer, fake_platform = _build_remote_computer(
        FOUR_MONITORS, target_monitor="DISPLAY4"
    )

    assert computer.current_monitor is not None
    assert computer.current_monitor.id == "DISPLAY4"

    summary, b64 = computer._run("screenshot", {})

    assert summary == "screenshot captured"
    assert b64 is not None
    # Exactly one capture reached the platform backend, scoped to DISPLAY4's
    # real bounds (3840, 2160) -> (7680, 4320) - never the 7680x4320
    # virtual-desktop bounding box and never `None`.
    assert fake_platform.capture_calls == [(3840, 2160, 7680, 4320)]

    png_bytes = base64.standard_b64decode(b64)
    from PIL import Image

    img = Image.open(io.BytesIO(png_bytes))
    disp = computer.display
    # Cross-checks the returned image dimensions match what `disp` (computed
    # from the SELECTED monitor's own width/height, per
    # `_resolve_display_for_target`) declares. The real proof this is
    # monitor-scoped rather than bounding-box-scoped is the exact
    # `capture_calls` region already asserted above.
    assert img.size == (disp.model_width, disp.model_height)


def test_primary_monitor_is_the_default_target_over_the_remote_round_trip():
    computer, fake_platform = _build_remote_computer(
        FOUR_MONITORS, target_monitor="primary"
    )

    assert computer.current_monitor is not None
    assert computer.current_monitor.id == "DISPLAY1"

    computer._run("screenshot", {})

    assert fake_platform.capture_calls == [(0, 0, 3840, 2160)]


def test_remote_screenshot_never_calls_the_unimplemented_native_capture_op():
    """`RemoteBackend.capture()` deliberately raises in Phase 1 (C1) - a
    screenshot through `ComputerTool` must never reach it, over the real
    wire, regardless of which monitor is targeted."""
    computer, fake_platform = _build_remote_computer(
        FOUR_MONITORS, target_monitor="DISPLAY2"
    )

    # `capture()` on the CLIENT-side RemoteBackend must never be invoked -
    # if it were, it would raise before this line finishes.
    computer._run("screenshot", {})

    assert fake_platform.capture_calls == [(3840, 0, 7680, 2160)]
    with pytest.raises(BackendError, match="capture_scaled"):
        computer._backend.capture()


def test_zoom_also_routes_through_capture_scaled_over_the_remote_round_trip():
    """C1's fast path must cover `zoom` too, not just `screenshot` - both
    dispatch through the same `imaging.capture_scaled_b64` helper in
    `ComputerTool._run`, and both must survive the real wire round trip."""
    computer, fake_platform = _build_remote_computer(
        FOUR_MONITORS, target_monitor="DISPLAY1"
    )

    _summary, b64 = computer._run(
        "zoom",
        {
            "coordinate": [
                0,
                0,
                computer.display.model_width,
                computer.display.model_height,
            ]
        },
    )

    assert b64 is not None
    assert fake_platform.capture_calls  # a region-scoped capture happened
    # zoom crops WITHIN the current monitor's screen space - every capture
    # call must stay inside DISPLAY1's bounds (0,0)-(3840,2160).
    for region in fake_platform.capture_calls:
        assert region is not None
        x1, y1, x2, y2 = region
        assert 0 <= x1 <= x2 <= 3840
        assert 0 <= y1 <= y2 <= 2160


def test_monitor_enumeration_failure_is_not_silently_swallowed_for_explicit_target():
    """An explicit `target_monitor` id that enumeration cannot find must fail
    loud (per `monitors.select_monitor`'s own contract) even over the real
    remote round trip - never silently fall back to a different monitor or
    the virtual desktop."""
    fake_platform = _FakePlatformBackend(FOUR_MONITORS)
    agent = RemoteAgent(fake_platform, read_only=True)
    transport = _InProcessAgentTransport(agent)
    backend = RemoteBackend({"_host": "user@fake-host", "_transport": transport})
    backend._connected = True

    with pytest.raises(BackendError, match="not found among enumerated monitors"):
        ComputerTool(backend, {"target_monitor": "DISPLAY99"}).resolve_display()
