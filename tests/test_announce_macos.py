"""Unit tests for the macOS announcement (`announce_macos.py`) -
`docs/coexistence.md` \u00a77.3: `osascript display dialog`, disclosed
timeout, and the rule that `gave up:true` is never consent.

`subprocess.run` is monkeypatched throughout - **no real `osascript` is ever
invoked by this test file**, per the hard safety rule: this bundle's own
verification work must never fire a real dialog at a user's Mac.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "modules" / "tool-computer-use"))

import pytest
from amplifier_module_tool_computer_use import announce_macos
from amplifier_module_tool_computer_use.announce_macos import (
    AnnounceError,
    _build_script,
    _parse_osascript_output,
    announce,
)


class _FakeCompleted:
    def __init__(self, returncode: int, stdout: str, stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


# -- the timeout MUST be disclosed in the prompt text (\u00a77.3) ----------------


def test_message_must_disclose_the_timeout():
    with pytest.raises(ValueError, match="disclose the timeout"):
        _build_script("An agent wants to drive this desktop.", timeout_seconds=20)


def test_message_with_disclosed_timeout_builds_a_valid_script():
    script = _build_script(
        "An agent wants to drive this desktop. This dialog will time out "
        "after 20 seconds.",
        timeout_seconds=20,
    )
    assert "giving up after 20" in script
    assert "display dialog" in script


# -- parsing osascript's exact O1-observed output shape ----------------------


def test_parse_button_returned_and_gave_up_false():
    button, gave_up = _parse_osascript_output("button returned:Continue, gave up:false")
    assert button == "Continue"
    assert gave_up is False


def test_parse_gave_up_true_with_no_button():
    button, gave_up = _parse_osascript_output("button returned:, gave up:true")
    assert button is None
    assert gave_up is True


# -- gave_up:true is never consent --------------------------------------------


def test_gave_up_result_is_never_acknowledged():
    button, gave_up = _parse_osascript_output("button returned:, gave up:true")
    from amplifier_module_tool_computer_use.announce_macos import AnnounceResult

    result = AnnounceResult(button=button, gave_up=gave_up, raw_stdout="")
    assert result.acknowledged is False


def test_real_button_press_is_acknowledged():
    from amplifier_module_tool_computer_use.announce_macos import AnnounceResult

    result = AnnounceResult(button="Pause", gave_up=False, raw_stdout="")
    assert result.acknowledged is True


# -- announce(): subprocess.run is fully faked, never invoked for real -------


def test_announce_returns_parsed_result_on_success(monkeypatch):
    def fake_run(cmd, **kwargs):
        assert cmd[0] == "osascript"
        return _FakeCompleted(0, "button returned:Continue, gave up:false")

    monkeypatch.setattr(announce_macos.subprocess, "run", fake_run)
    result = announce(
        "Agent wants to drive. This dialog will time out after 20 seconds.",
        timeout_seconds=20,
    )
    assert result.button == "Continue"
    assert result.gave_up is False


def test_announce_raises_on_nonzero_exit(monkeypatch):
    def fake_run(cmd, **kwargs):
        return _FakeCompleted(1, "", "some AppleScript error")

    monkeypatch.setattr(announce_macos.subprocess, "run", fake_run)
    with pytest.raises(AnnounceError):
        announce(
            "Agent wants to drive. This dialog will time out after 20 seconds.",
            timeout_seconds=20,
        )


def test_announce_raises_on_oserror(monkeypatch):
    def fake_run(cmd, **kwargs):
        raise OSError("osascript not found")

    monkeypatch.setattr(announce_macos.subprocess, "run", fake_run)
    with pytest.raises(AnnounceError):
        announce(
            "Agent wants to drive. This dialog will time out after 20 seconds.",
            timeout_seconds=20,
        )
