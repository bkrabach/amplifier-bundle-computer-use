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
