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
        elif req.op == "list_windows":
            result = {
                "windows": [
                    {"handle": "42", "title": "Notepad", "minimized": False},
                    {"handle": "7", "title": "Hidden", "minimized": True},
                ],
                "foreground": "42",
            }
        elif req.op == "get_clipboard":
            result = {"text": "clipboard contents"}
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


def test_mouse_down_and_up_round_trip_through_the_wire():
    backend, transport = _connected_backend()
    backend.mouse_down(10, 20, "left")
    backend.mouse_up(10, 20, "left")
    ops = [c.op for c in transport.calls]
    assert ops == ["mouse_down", "mouse_up"]
    assert transport.calls[0].args == {"x": 10, "y": 20, "button": "left"}


def test_drag_sends_start_and_end_in_one_call():
    backend, transport = _connected_backend()
    backend.drag((1, 2), (3, 4))
    assert len(transport.calls) == 1, "drag must stay one wire round trip (\u00a710.2)"
    assert transport.calls[0].op == "drag"
    assert transport.calls[0].args == {"start": [1, 2], "end": [3, 4]}


def test_drag_with_no_start_sends_null():
    backend, transport = _connected_backend()
    backend.drag(None, (3, 4))
    assert transport.calls[0].args["start"] is None


def test_scroll_round_trips_through_the_wire():
    backend, transport = _connected_backend()
    backend.scroll(5, 6, "down", 3)
    assert transport.calls[0].op == "scroll"
    assert transport.calls[0].args == {
        "x": 5,
        "y": 6,
        "direction": "down",
        "amount": 3,
    }


def test_hold_key_round_trips_through_the_wire():
    backend, transport = _connected_backend()
    backend.hold_key("ctrl+shift", 1.5)
    assert transport.calls[0].op == "hold_key"
    assert transport.calls[0].args == {"combo": "ctrl+shift", "duration": 1.5}


def test_list_windows_deserializes_into_window_info():
    backend, transport = _connected_backend()
    result = backend.list_windows()
    assert transport.calls[0].op == "list_windows"
    assert result.foreground == "42"
    assert len(result.windows) == 2
    assert result.windows[0].handle == "42"
    assert result.windows[0].title == "Notepad"
    assert result.windows[0].minimized is False
    assert result.windows[1].minimized is True


def test_focus_window_round_trips_through_the_wire():
    backend, transport = _connected_backend()
    backend.focus_window("42")
    assert transport.calls[0].op == "focus_window"
    assert transport.calls[0].args == {"handle": "42"}


def test_get_clipboard_returns_agent_text():
    backend, transport = _connected_backend()
    result = backend.get_clipboard()
    assert transport.calls[0].op == "get_clipboard"
    assert result == "clipboard contents"


def test_set_clipboard_round_trips_through_the_wire():
    backend, transport = _connected_backend()
    backend.set_clipboard("hello")
    assert transport.calls[0].op == "set_clipboard"
    assert transport.calls[0].args == {"text": "hello"}


def test_a_failed_mouse_down_is_not_retried():
    """mouse_down/mouse_up/drag/scroll/hold_key/focus_window/set_clipboard are
    all WRITE ops (\u00a76.3) - a failure must never be retried."""
    backend, transport = _connected_backend()
    transport.fail_next = True
    with pytest.raises(BackendError):
        backend.mouse_down(0, 0)
    assert len([c for c in transport.calls if c.op == "mouse_down"]) == 1
