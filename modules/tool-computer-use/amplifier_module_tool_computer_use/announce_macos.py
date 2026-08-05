"""The macOS announcement - `docs/designs/coexistence.md` \u00a77.3, \u00a79.1, \u00a713 (D2).

O1 (`coexistence-probes.md`) proved `osascript -e 'display dialog ...'` reaches
the console user's screen and returns which button was pressed, even launched
from a plain SSH/Background-domain process. O2 proved an agent-drawn `NSWindow`
does NOT reach the desktop from that same context. Together: macOS gets a
human-visible, human-answerable channel at zero install (no LaunchAgent, no
resident process, \u00a74/D2) - but it is a one-time **announce-and-acknowledge**
interaction, not a persistent ambient indicator like the Linux overlay. Do not
try to make the two platforms share one UX model (\u00a77.3's "different
interaction models... forcing them into one abstraction would produce a worse
design than admitting they are two").

THE TIMEOUT RULE, load-bearing (\u00a77.3/\u00a712)
------------------------------------------
`osascript`'s `giving up after N` returns `gave up:true` if nobody answers in
time. **`gave up:true` is never consent.** \u00a712's success metric is explicit:
"macOS sessions where the timeout path granted permission while a human was
active" must be **zero**. So the timeout's disposition depends on whether a
human was actually detected active at the moment the dialog gave up (the
`PresenceMonitor` sample taken right then) - not on the dialog's own return
value in isolation. This module reports the raw outcome; the caller (the
coexistence guard / mount-time policy) is responsible for combining
`gave_up` with a fresh presence sample before deciding whether to proceed.

Also load-bearing: the dialog's own text must **disclose the timeout** to
the human reading it (\u00a77.3) - a dialog that silently times out after an
undisclosed interval is not a real announcement, it is a countdown nobody
was told about.

HARD SAFETY RULE for this bundle's own development and testing
----------------------------------------------------------------
This module is implemented and unit-tested (with `subprocess.run` faked -
see `tests/test_announce_macos.py`), but this repository's own verification
work must never actually FIRE an `osascript` dialog at a real user's Mac they
are sitting at - see the top-level task's hard rules. `announce()` is a plain,
inert function until a caller with real authorization to disturb a specific
console session invokes it.
"""

from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass

logger = logging.getLogger(__name__)

#: Default disclosed timeout, matching O1's probe (\u00a712's dialog example used 6s
#: for a quick probe; a real announce should give a human enough time to read
#: and react - 20s is a reasonable default a caller can override).
DEFAULT_TIMEOUT_SECONDS = 20


class AnnounceError(RuntimeError):
    """`osascript` itself failed (nonzero exit, not found, ...) - distinct
    from a successful dialog that simply timed out (`AnnounceResult.gave_up`
    covers that case)."""


@dataclass(frozen=True)
class AnnounceResult:
    button: str | None
    gave_up: bool
    raw_stdout: str

    @property
    def acknowledged(self) -> bool:
        """A real human pressed a real button - the only case that is
        unambiguous consent to proceed."""
        return not self.gave_up and self.button is not None


def _build_script(message: str, timeout_seconds: int) -> str:
    """Build the AppleScript text. `message` MUST already state the timeout
    in plain language (\u00a77.3) - this function does not inject that text for
    the caller, so the disclosure is visibly part of the caller's own prompt
    rather than something this module could silently omit or get wrong.
    """
    if (
        str(timeout_seconds) not in message
        and f"{timeout_seconds} second" not in message
    ):
        raise ValueError(
            "message must disclose the timeout in plain text (\u00a77.3) - "
            f"expected the literal number {timeout_seconds!r} to appear "
            f"somewhere in: {message!r}"
        )
    escaped = message.replace("\\", "\\\\").replace('"', '\\"')
    return (
        f'display dialog "{escaped}" buttons {{"Pause", "Continue"}} '
        f"default button 2 giving up after {timeout_seconds}"
    )


def _parse_osascript_output(stdout: str) -> tuple[str | None, bool]:
    """Parse `button returned:X, gave up:true/false` (O1's exact observed
    output shape). Missing `button returned:` entirely (a `gave up:true` with
    no button chosen) reports `button=None`."""
    gave_up = "gave up:true" in stdout
    button: str | None = None
    marker = "button returned:"
    idx = stdout.find(marker)
    if idx != -1:
        rest = stdout[idx + len(marker) :]
        button = rest.split(",", 1)[0].strip() or None
    return button, gave_up


def announce(
    message: str,
    *,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    osascript_path: str = "osascript",
) -> AnnounceResult:
    """Show the session-start announcement dialog and return the outcome.

    Blocking, modal, for at most `timeout_seconds` (\u00a710.1: "Session start
    only - never on the injection path"). Raises `AnnounceError` if
    `osascript` itself could not be run or returned nonzero for a reason
    other than the dialog timing out.
    """
    script = _build_script(message, timeout_seconds)
    try:
        proc = subprocess.run(
            [osascript_path, "-e", script],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_seconds + 10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AnnounceError(f"osascript invocation failed: {exc}") from exc
    if proc.returncode != 0:
        raise AnnounceError(
            f"osascript exited {proc.returncode}: {proc.stderr.strip()[:400]}"
        )
    button, gave_up = _parse_osascript_output(proc.stdout)
    result = AnnounceResult(button=button, gave_up=gave_up, raw_stdout=proc.stdout)
    logger.info(
        "macOS announce: button=%r gave_up=%s (raw=%r)",
        result.button,
        result.gave_up,
        proc.stdout.strip(),
    )
    return result
