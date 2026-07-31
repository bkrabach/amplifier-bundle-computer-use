"""Unit tests for backend selection/probe order (`registry.select_backend`).

D1 depends entirely on this working correctly: a backend that cannot serve this
machine must never be selected, a backend whose `probe()` itself misbehaves must
never take down selection, and the first *available* backend in priority order must
win. None of this touches a real desktop - every backend here is a fake.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "modules" / "tool-computer-use"))

import pytest
from amplifier_module_tool_computer_use.backend import ProbeResult
from amplifier_module_tool_computer_use.registry import (
    NoBackendAvailable,
    select_backend,
)


class _FakeBackend:
    """Minimal stand-in satisfying just enough of the Backend surface for probing."""

    name = "fake"
    available = True
    raises = False

    def __init__(self, config=None) -> None:
        self.config = config

    def probe(self) -> ProbeResult:
        if self.raises:
            raise RuntimeError("probe blew up")
        return ProbeResult(self.available, "" if self.available else "fake unavailable")


def _make(
    name: str, available: bool = True, raises: bool = False
) -> type[_FakeBackend]:
    return type(
        name,
        (_FakeBackend,),
        {"name": name, "available": available, "raises": raises},
    )


def test_select_backend_picks_first_available_in_priority_order():
    unavailable = _make("unavailable-one", available=False)
    available = _make("available-two", available=True)
    never_reached = _make("never-reached", available=True)

    backend = select_backend({}, factories=(unavailable, available, never_reached))
    assert backend.name == "available-two"


def test_select_backend_tries_windows_before_linux_by_default():
    from amplifier_module_tool_computer_use.linux_x11 import LinuxX11Backend
    from amplifier_module_tool_computer_use.registry import BACKEND_FACTORIES
    from amplifier_module_tool_computer_use.windows import WindowsBackend

    assert BACKEND_FACTORIES.index(WindowsBackend) < BACKEND_FACTORIES.index(
        LinuxX11Backend
    )


def test_select_backend_raises_with_every_reason_when_none_available():
    a = _make("a", available=False)
    b = _make("b", available=False)

    with pytest.raises(NoBackendAvailable) as excinfo:
        select_backend({}, factories=(a, b))

    message = str(excinfo.value)
    assert "a" in message
    assert "b" in message
    assert "fake unavailable" in message
    assert excinfo.value.attempts == [
        ("a", "fake unavailable"),
        ("b", "fake unavailable"),
    ]


def test_select_backend_survives_a_probe_that_raises():
    """D1's whole point: a broken backend must not break selection of the *next*
    candidate, and must never propagate an exception out of mount()."""
    broken = _make("broken", raises=True)
    good = _make("good", available=True)

    backend = select_backend({}, factories=(broken, good))
    assert backend.name == "good"


def test_select_backend_raises_when_every_candidate_probe_raises():
    broken_one = _make("broken-one", raises=True)
    broken_two = _make("broken-two", raises=True)

    with pytest.raises(NoBackendAvailable) as excinfo:
        select_backend({}, factories=(broken_one, broken_two))
    assert "probe raised" in str(excinfo.value)


def test_select_backend_passes_config_through_to_constructor():
    seen = {}

    class _ConfigCapturingBackend(_FakeBackend):
        name = "config-capturing"

        def __init__(self, config=None) -> None:
            super().__init__(config)
            seen["config"] = config

    cfg = {"some_key": "some_value"}
    select_backend(cfg, factories=(_ConfigCapturingBackend,))
    assert seen["config"] == cfg
