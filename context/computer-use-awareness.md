# Computer Use (Windows desktop)

This session can see and control the real Windows desktop through Claude's native
computer-use tool, named `computer`. It works on anything on screen — including
applications with no API, no CLI, and no extension.

**Delegate desktop work to `computer-use:computer-operator`.** It carries the operating
rules for driving a live machine safely.

Use it whenever the user asks what is on their screen, asks you to click, type, drag,
scroll, or open something in a Windows application, or hits a task that cannot be done
through an API.

The screen is captured, downscaled, and handed to the model as an image; coordinates the
model emits are scaled back to physical pixels automatically. Never guess coordinates —
take a screenshot first.

## You may not be the only one at this keyboard

A human can be sitting at this machine typing at the same time you are driving it.
Nothing separates the two input streams — your keystrokes and theirs interleave, not
queue. A command you believe you typed verbatim can land with a stray character spliced
in the middle of it, or with a character missing, and still look, from a screenshot, like
the right window has focus. When a typed result looks even slightly off — an error you
didn't expect, a typo you don't remember making, output that doesn't match what the
command you sent should produce — suspect interleaving before you suspect your own
reasoning. Verifying what actually landed is cheap; continuing on the assumption that it
matched what you sent is not.
