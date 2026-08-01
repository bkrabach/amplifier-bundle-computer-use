"""Unit tests for `RemoteBackend` against a `_FakeTransport` - no real SSH, no
real subprocess. Proves request/response correlation, the capture_scaled
capability shape, and (critically) that a failed WRITE op is never retried.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "modules" / "tool-computer-use"))

import pytest
from amplifier_module_tool_computer_use.backend import BackendError
from amplifier_module_tool_computer_use.remote_backend import RemoteBackend
from amplifier_module_tool_computer_use.wire import Request, Response


class _FakeTransport:
    """Stands in for `SshTransport.send` - decodes the request, hands back a
    scripted response, and counts calls per op so tests can assert a WRITE
    was attempted exactly once even after a simulated failure."""

    def __init__(self) -> None:
        self.calls: list[Request] = []
        self.fail_next = False

    def send(self, line: bytes, timeout: float = 30.0) -> bytes:
        import json

        data = json.loads(line)
        req = Request(id=data["id"], op=data["op"], args=data.get("args") or {})
        self.calls.append(req)
        if self.fail_next:
            from amplifier_module_tool_computer_use.ssh_transport import SshConnectError

            raise SshConnectError("simulated connection loss")
        if req.op == "screen_geometry":
            result = {"width": 1920, "height": 1080, "origin_x": 0, "origin_y": 0}
        elif req.op == "capture_scaled":
            result = {
                "enc": "b64",
                "png": "aGVsbG8=",
                "w": 640,
                "h": 400,
                "native_w": 3840,
                "native_h": 2160,
                "scaled_on": "agent",
            }
        elif req.op == "click":
            result = None
        else:
            result = {}
        return Response(id=req.id, ok=True, result=result).encode()


def _connected_backend() -> tuple[RemoteBackend, _FakeTransport]:
    transport = _FakeTransport()
    backend = RemoteBackend({"_host": "user@host", "_transport": transport})
    backend._connected = True  # bypass real connect() for unit-level testing
    return backend, transport


def test_is_remote_flag_is_true():
    backend, _ = _connected_backend()
    assert backend.is_remote is True


def test_screen_geometry_round_trips_through_the_wire():
    backend, transport = _connected_backend()
    geo = backend.screen_geometry()
    assert (geo.width, geo.height) == (1920, 1080)
    assert transport.calls[0].op == "screen_geometry"


def test_capture_scaled_returns_the_agent_scaled_png_b64():
    backend, transport = _connected_backend()
    png_b64 = backend.capture_scaled(None, (640, 400), 1280, 1_150_000)
    assert png_b64 == "aGVsbG8="
    assert transport.calls[0].op == "capture_scaled"
    assert transport.calls[0].args["model_w"] == 640


def test_native_capture_is_refused_in_phase_1():
    """C1: the raw, native-resolution capture op is deliberately not wired -
    only capture_scaled is, so nothing can accidentally drag a multi-MB frame
    across the wire."""
    backend, _ = _connected_backend()
    with pytest.raises(BackendError, match="capture_scaled"):
        backend.capture()


def test_a_failed_write_is_not_retried():
    """\u00a76.3's most important guarantee, proven at the RemoteBackend layer:
    exactly one attempt reaches the transport for a WRITE op, even though it
    fails."""
    backend, transport = _connected_backend()
    transport.fail_next = True

    with pytest.raises(BackendError):
        backend.click(10, 10)

    write_calls = [c for c in transport.calls if c.op == "click"]
    assert len(write_calls) == 1, "a WRITE op must never be retried after failure"


def test_not_connected_raises_backend_error_not_a_crash():
    backend = RemoteBackend({"_host": "user@host", "_transport": _FakeTransport()})
    with pytest.raises(BackendError, match="not connected"):
        backend.screen_geometry()


def test_phase_2_ops_fail_loud_rather_than_silently_no_op():
    backend, _ = _connected_backend()
    with pytest.raises(BackendError, match="Phase 2"):
        backend.mouse_down(0, 0)
    with pytest.raises(BackendError, match="Phase 2"):
        backend.drag((0, 0), (10, 10))
