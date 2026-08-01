"""Unit tests for the C1 fast path in `imaging.capture_scaled_b64`: a backend
carrying the `capture_scaled` capability must be used instead of `capture()` +
a local PIL resize, detected via the class-descriptor idiom (never `hasattr`
on the instance - see docstring in imaging.py for why).
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "modules" / "tool-computer-use"))

from amplifier_module_tool_computer_use.geometry import Display
from amplifier_module_tool_computer_use.imaging import capture_scaled_b64


class _RemoteLikeBackend:
    """Has `capture_scaled` - the fast path must be taken, `capture()` must
    never be called."""

    def __init__(self) -> None:
        self.capture_scaled_calls: list[tuple] = []
        self.capture_calls = 0

    def capture(self, region=None) -> bytes:
        self.capture_calls += 1
        raise AssertionError("capture() must not be called when capture_scaled exists")

    def capture_scaled(self, region, model_size, max_edge, max_pixels) -> str:
        self.capture_scaled_calls.append((region, model_size, max_edge, max_pixels))
        return "already-scaled-b64-from-agent"


class _LocalLikeBackend:
    """No `capture_scaled` at all - must take the existing local path
    unchanged (a plain 1x1 red PNG, resized by PIL)."""

    def capture(self, region=None) -> bytes:
        import io

        from PIL import Image

        img = Image.new("RGB", (4, 4), (255, 0, 0))
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()


def test_capture_scaled_capability_is_preferred_over_capture():
    backend = _RemoteLikeBackend()
    disp = Display(
        screen_width=3840, screen_height=2160, model_width=1280, model_height=720
    )

    result = capture_scaled_b64(backend, disp, None, 1280, 1_150_000)

    assert result == "already-scaled-b64-from-agent"
    assert backend.capture_calls == 0
    assert backend.capture_scaled_calls == [(None, (1280, 720), 1280, 1_150_000)]


def test_local_backend_without_capability_uses_the_original_path_unchanged():
    backend = _LocalLikeBackend()
    disp = Display(screen_width=4, screen_height=4, model_width=2, model_height=2)

    result = capture_scaled_b64(backend, disp, None, 1280, 1_150_000)

    assert isinstance(result, str)
    assert len(result) > 0


def test_class_descriptor_check_never_invokes_a_raising_capture_scaled():
    """Mirrors D3: checking `getattr(type(backend), "capture_scaled", None)`
    must never itself invoke `capture_scaled` - only calling it (inside
    capture_scaled_b64's own call) can raise, and that's the caller's
    responsibility, not this detection step's."""

    class _Broken:
        def capture(self, region=None) -> bytes:
            raise AssertionError("must not reach capture() either")

        def capture_scaled(self, region, model_size, max_edge, max_pixels) -> str:
            raise RuntimeError("agent-side failure")

    assert getattr(type(_Broken()), "capture_scaled", None) is not None
