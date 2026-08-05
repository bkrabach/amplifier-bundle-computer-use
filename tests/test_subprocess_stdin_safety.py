"""Structural regression guard for the stdin-theft incident.

`overlay_windows.WindowsOverlay.show()` called `subprocess.run([self.powershell,
*args], capture_output=True, ...)` with no `stdin=`. `capture_output=True` pins
stdout/stderr only -- the child therefore *inherited* the parent's fd 0, which on
a real interactive session is the user's terminal. Under WSL2 interop the child
(or, more precisely, the `/init` layer bridging it to the Windows side) becomes a
reader in the pty's foreground process group and races the real REPL for whatever
is sitting in the terminal's input queue -- eating both prompt_toolkit's CPR reply
(-> the reported "doesn't support cursor position requests" warning) and the
user's own keystrokes (-> "impossible to type"). See the accompanying incident
report and `overlay_windows.py`/`windows.py`/`macos.py`/`linux_x11.py`/
`announce_macos.py`/`ssh_transport.py`'s `subprocess.run`/`Popen` call sites for
the fix (`stdin=subprocess.DEVNULL` on every call that doesn't need input).

The failure is remote from its cause -- a user sees a prompt_toolkit warning and
blames their terminal -- so this is a structural guard, not a reminder: every
`subprocess.run`/`subprocess.Popen` call in this package must make an EXPLICIT,
reviewable choice about its child's stdin. There is no allowlist. A call site
that legitimately needs the parent's terminal (none exist today) documents that
choice by passing `stdin=` to something other than `DEVNULL` -- it does not get
silently exempted here.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "modules" / "tool-computer-use"))

PKG_DIR = ROOT / "modules" / "tool-computer-use" / "amplifier_module_tool_computer_use"


def _subprocess_calls(path: Path) -> list[ast.Call]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in ("run", "Popen")
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "subprocess"
    ]


def test_every_subprocess_call_pins_stdin_or_input():
    """No `subprocess.run`/`subprocess.Popen` call in this package may omit
    both `stdin=` and `input=` -- omitting both is exactly the shape of the
    bug that shipped (see module docstring). `input=` counts as pinned:
    `subprocess.run(input=...)` sets `stdin=PIPE` internally (cpython's own
    `subprocess.run` rejects passing both `input` and `stdin` together), so a
    call using `input=` already cannot inherit the parent's fd.
    """
    offenders: list[str] = []
    for path in sorted(PKG_DIR.glob("*.py")):
        for node in _subprocess_calls(path):
            kw_names = {kw.arg for kw in node.keywords if kw.arg is not None}
            if "stdin" not in kw_names and "input" not in kw_names:
                offenders.append(f"{path.name}:{node.lineno}")
    assert not offenders, (
        "subprocess call(s) with neither stdin= nor input= -- these silently "
        "inherit the parent's fd 0 (a real terminal, on a real desktop), "
        "exactly how the CPR/stdin-theft bug shipped. Pin stdin= explicitly "
        "(subprocess.DEVNULL for a one-shot helper that needs no input) at: "
        + ", ".join(offenders)
    )


def test_ssh_transport_persistent_process_still_pipes_stdin():
    """`SshTransport.connect()`'s one `subprocess.Popen` is the persistent
    remote-agent connection: the deployed payload and every NDJSON request for
    the life of the session are written to this exact pipe (see
    `ssh_transport.py`'s module docstring). It is the one call in this package
    that legitimately needs a real stdin pipe, not `DEVNULL`. Blanket-DEVNULLing
    every call site (the trap the previous bug's fix could have fallen into)
    would silently break every remote session; this guards specifically
    against that regression, distinct from the general check above.
    """
    ssh_transport = PKG_DIR / "ssh_transport.py"
    popen_calls = _subprocess_calls(ssh_transport)
    run_calls = [
        c
        for c in popen_calls
        if isinstance(c.func, ast.Attribute) and c.func.attr == "Popen"
    ]
    assert len(run_calls) == 1, (
        f"expected exactly one subprocess.Popen call in ssh_transport.py, "
        f"found {len(run_calls)} -- update this test if that has "
        "deliberately changed"
    )
    kw = {k.arg: k for k in run_calls[0].keywords if k.arg is not None}
    assert "stdin" in kw, "ssh_transport.py's Popen must pin stdin explicitly"
    stdin_value = kw["stdin"].value
    assert isinstance(stdin_value, ast.Attribute) and stdin_value.attr == "PIPE", (
        "ssh_transport.py's persistent Popen must use stdin=subprocess.PIPE "
        "(the agent is fed NDJSON over this pipe for the life of the "
        "session) -- do not DEVNULL it"
    )
