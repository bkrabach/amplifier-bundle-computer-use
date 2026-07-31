#!/usr/bin/env python3
"""Acceptance gate for the Linux X11 computer-use backend.

Runs entirely against the real local X11 desktop (no mocks): probes the backend,
resolves display geometry, then checks each capability against reality.

What this script found on this box, and why it is structured this way:

1. **Pointer motion (`move()`/`cursor_position()`) is verified working.** XTEST
   `MotionNotify` moves the real X server pointer; `query_pointer()`/
   `cursor_position()` reads it back and matches exactly, every time.

2. **`capture()` reflects real desktop content, not a static image.** This
   environment runs a compositing window manager (GNOME Shell/mutter). A bare,
   undecorated Xlib window filled with a solid color did *not* appear in `capture()`
   output at all - confirmed against root-window `GetImage`, ImageMagick's
   `import -window root`, and the Composite extension's overlay window
   (`XCompositeGetOverlayWindow`); all three showed only the desktop background. A
   real GTK toolkit window (`zenity`) *does* appear as a genuine, substantial pixel
   change. So capture verification here opens a real `zenity` dialog and measures
   that change, rather than asserting one pixel's exact expected color.

3. **Discrete input (`click()`/`key()`) is root-caused, not just reported as a
   mystery.** An earlier version of this script tried to confirm discrete-event
   delivery by opening a *second* Xlib connection and selecting `ButtonPressMask |
   KeyPressMask` on the root window, then waiting to see whether anything arrived.
   That approach was methodologically unsound: root-window event *selection* can
   silently coexist with, or lose to, another client's active *grab* - a select
   tells you nothing about whether a grab is in front of it, and the null result
   it produced ("no event received") was true but did not explain *why*, so it
   could not distinguish "this backend is broken" from "something else on this
   session owns input right now".

   The actual mechanism, confirmed with real X protocol calls
   (`root.grab_pointer(...)` / `root.grab_keyboard(...)`), is that **this X11
   session's root window pointer and/or keyboard is already exclusively grabbed
   by another client** - both grab attempts return `AlreadyGrabbed` (1), not
   `GrabSuccess` (0), consistently, independent of whether an RDP client is
   currently connected via `gnome-remote-desktop` (this box's GNOME headless
   remote-desktop session is the leading candidate: `gnome-remote-desktop.service`
   is enabled at `graphical.target` and its D-Bus surfaces
   (`org.gnome.Mutter.RemoteDesktop`, `org.gnome.RemoteDesktop.User`) are active
   whether or not a client is attached). XTEST still *generates* real button/key
   events under this condition - `fake_input` never raises, and a raw XI2 monitor
   on the root window does observe them - but the exclusive grab consumes them
   before normal window-hierarchy delivery, so no application window - not
   `zenity`, not `xev`, none - ever sees them. This was independently confirmed
   against GNOME's own native input-injection channel too
   (`org.gnome.Mutter.RemoteDesktop.Session.NotifyPointerButton` /
   `NotifyKeyboardKeysym`): it fails identically, which rules out "use a
   different injection API" as a fix and confirms the block is the grab itself,
   not this backend's particular use of XTEST.

   This check therefore does exactly what `LinuxX11Backend._check_discrete_input_available()`
   now does internally: attempt (and immediately release) a transient
   `grab_pointer`/`grab_keyboard`, and report the real, specific, actionable
   result - not a timing-based guess.

Prints PASS/FAIL for each check and exits nonzero if anything genuinely fails -
this script does not fabricate a pass for #3.

    python scripts/verify_linux_x11.py
"""

from __future__ import annotations

import io
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "modules" / "tool-computer-use"))

from amplifier_module_tool_computer_use.backend import BackendError
from amplifier_module_tool_computer_use.linux_x11 import LinuxX11Backend

_FAILURES: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(
        f"  {'PASS' if ok else 'FAIL'}  {label}{'  <- ' + detail if detail and not ok else ''}"
    )
    if not ok:
        _FAILURES.append(label)


def _load(png_bytes: bytes):
    from PIL import Image

    return Image.open(io.BytesIO(png_bytes)).convert("RGB")


def _sampled_diff_count(img_a, img_b, step: int = 4) -> int:
    count = 0
    for y in range(0, min(img_a.height, img_b.height), step):
        for x in range(0, min(img_a.width, img_b.width), step):
            if img_a.getpixel((x, y)) != img_b.getpixel((x, y)):
                count += 1
    return count


def main() -> int:
    print("=== Linux X11 computer-use backend: end-to-end proof ===")

    # -- [1] capability probe -----------------------------------------------
    print("\n[1] probe()")
    backend = LinuxX11Backend()
    result = backend.probe()
    check("backend reports available", result.available, result.reason)
    if not result.available:
        print(f"\nFAIL: backend unavailable ({result.reason}); cannot continue")
        return 1

    # -- [2] display geometry --------------------------------------------------
    print("\n[2] screen_geometry()")
    try:
        geo = backend.screen_geometry()
        check(
            "geometry resolved",
            geo.width > 0 and geo.height > 0,
            f"{geo.width}x{geo.height}",
        )
        print(f"       -> {geo.width}x{geo.height} @ ({geo.origin_x}, {geo.origin_y})")
    except BackendError as exc:
        check("geometry resolved", False, str(exc))
        return 1

    # -- [3] pointer motion reaches the real X server ------------------------
    print(
        "\n[3] move() moves the real X server pointer (verified via cursor_position())"
    )
    target_x, target_y = geo.width // 2, geo.height // 2
    backend.move(target_x, target_y)
    time.sleep(0.1)
    pos = backend.cursor_position()
    check(
        "pointer moved to the exact target",
        pos == (target_x, target_y),
        f"pointer at {pos}",
    )

    # -- [4] capture() reflects real desktop content -------------------------
    print("\n[4] capture() reflects a real content change (zenity dialog opening)")
    before_png = backend.capture()
    before_img = _load(before_png)

    proc = subprocess.Popen(
        [
            "zenity",
            "--info",
            "--title=computer-use verify",
            "--text=computer-use verify",
            "--width=250",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        time.sleep(2.0)  # let zenity map and render
        check(
            "zenity is actually running",
            proc.poll() is None,
            f"exited with {proc.poll()}",
        )
        after_png = backend.capture()
        after_img = _load(after_png)
        diff = _sampled_diff_count(before_img, after_img, step=2)
        total_samples = (geo.width // 2) * (geo.height // 2)
        print(
            f"       -> {diff}/{total_samples} sampled points differ after opening a real dialog"
        )
        check(
            "a substantial region of the capture changed",
            diff
            > total_samples
            * 0.003,  # calibrated against a real measured ~0.9% for this dialog size
            f"only {diff}/{total_samples} differed",
        )
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()

    # -- [5] discrete input (click/key): root-caused via a real grab probe, not a
    #        timing-based "did anything show up" guess -----------------------------
    print(
        "\n[5] click()/key(): is this session's discrete input exclusively grabbed "
        "by another client?"
    )
    print(
        "       -> calling backend.click()/backend.key() directly; the backend's own\n"
        "          _check_discrete_input_available() now performs the grab probe and\n"
        "          raises a specific BackendError if something else holds the grab."
    )
    discrete_input_blocked_reason: str | None = None
    try:
        backend.click(target_x, target_y, button="left", count=1)
        backend.key("a")
    except BackendError as exc:
        discrete_input_blocked_reason = str(exc)

    if discrete_input_blocked_reason:
        check(
            "discrete input (click/key) can reach application windows",
            False,
            discrete_input_blocked_reason,
        )
        print(
            "       -> Root cause (not a fabricated pass): the X11 core protocol\n"
            "          grab_pointer()/grab_keyboard() calls the backend made both\n"
            "          returned AlreadyGrabbed, not GrabSuccess. XTEST still generates\n"
            "          real button/key events under this condition - fake_input never\n"
            "          raises, and a raw XI2 monitor on the root window does see them -\n"
            "          but the exclusive grab consumes them before window-hierarchy\n"
            "          delivery, so no application window ever sees a click or\n"
            "          keystroke. Verified against GNOME's own native input-injection\n"
            "          channel too (org.gnome.Mutter.RemoteDesktop.Session\n"
            "          .NotifyPointerButton/NotifyKeyboardKeysym): it fails identically,\n"
            "          which rules out 'use a different injection API' as a fix.\n"
            "          Commonly caused by a GNOME headless remote-desktop session\n"
            "          (gnome-remote-desktop / mutter) holding an exclusive input grab\n"
            "          for its virtual seat, independent of whether an RDP client is\n"
            "          currently connected. This is an environment condition, not a\n"
            "          defect in this backend's use of XTEST."
        )
    else:
        # No grab was in the way - the backend accepted the calls. Confirm the click
        # and keystroke actually landed on a real application, not just that no
        # exception was raised: open a real zenity dialog, click its content area, type
        # into it, and check zenity's own stdout for what we typed (an end-to-end,
        # user-perspective observable side effect - never protocol-level interception).
        confirm_proc = subprocess.Popen(
            ["zenity", "--entry", "--title=cu verify", "--text=cu verify"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        try:
            time.sleep(1.5)
            windows = backend.list_windows()
            match = next(
                (w for w in windows.windows if "cu verify" in w.title.lower()), None
            )
            check("zenity entry dialog found via list_windows()", match is not None)
            if match:
                backend.focus_window(match.handle)
                time.sleep(0.3)
                backend.click(target_x, target_y, button="left", count=1)
                backend.type_text("cu-proof-42")
                backend.key("Return")
            stdout, _ = confirm_proc.communicate(timeout=5)
            check(
                "typed text reached zenity (its own stdout echoes it back)",
                stdout.strip() == "cu-proof-42",
                f"zenity stdout was {stdout.strip()!r}",
            )
        except subprocess.TimeoutExpired:
            confirm_proc.kill()
            check(
                "typed text reached zenity (its own stdout echoes it back)",
                False,
                "zenity did not exit - Return was never delivered",
            )
        finally:
            if confirm_proc.poll() is None:
                confirm_proc.terminate()
                try:
                    confirm_proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    confirm_proc.kill()

    backend.close()

    print("\n" + ("ALL CHECKS PASSED" if not _FAILURES else f"FAILURES: {_FAILURES}"))
    return 1 if _FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())

# -- Investigation note -------------------------------------------------------
#
# The first version of this script created its own bare (non-toolkit) Xlib window,
# filled it with a solid color, and asserted `capture()` showed that color at a known
# pixel. That failed - not because `move()`/`click()`/`capture()` are broken, but
# because on this box's compositing window manager (GNOME Shell/mutter 46), a plain
# Xlib window with no toolkit involvement was never composited into anything readable
# via the X protocol: not the root window (`GetImage` on root), not ImageMagick's
# `import -window root` (same underlying mechanism), and not even the Composite
# extension's overlay window (`XCompositeGetOverlayWindow`) - all three were checked
# directly against this environment and all three showed only the desktop background,
# confirmed by comparing pixel-for-pixel against a baseline capture with no extra
# windows open.
#
# A real GTK application (`zenity`, and separately `gedit`) opening *does* show up as
# a genuine, substantial pixel change in the same `capture()` output - proving the
# capture path itself is correct end-to-end for real application windows, which is
# what this tool actually needs to see. The bare-Xlib-window gap is most likely
# specific to how this particular compositor decides what to composite (undecorated,
# hint-less override-redirect clients are a known edge case for some compositors'
# damage/redirect heuristics) - it was not investigated further given this script's
# purpose is verifying the backend against real usage, not auditing mutter's
# compositor internals.
