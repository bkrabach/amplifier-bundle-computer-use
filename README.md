# amplifier-bundle-computer-use

Give an Amplifier session real control of a **Windows desktop from WSL2**, using
**Claude's native computer-use tool** — not a homegrown imitation of it.

If a human could do it by looking at the screen and clicking, this can do it. No API, no
CLI, no browser extension required.

```
"What's on my screen right now?"
"Click the Export button in that accounting app and save it to my desktop."
"Fill this dialog in for me — I'm done pressing buttons."
```

---

## What makes this the native thing

Claude is post-trained on a specific server-side tool definition
(`computer_20251124`). Sending it a lookalike function tool gets you noticeably worse
targeting. This bundle sends the real one:

```json
{"type": "computer_20251124", "name": "computer",
 "display_width_px": 1280, "display_height_px": 720, "enable_zoom": true}
```

…with the `computer-use-2025-11-24` beta header, and returns screenshots as genuine
base64 **image** content blocks inside `tool_result`, exactly as Anthropic's own loop
does.

## The two things that had to be solved

Amplifier's orchestrator stands between a mounted tool and native computer use:

| Blocker | Where | Effect |
|---|---|---|
| `ToolSpec` is built from `name`/`description`/`parameters` only | `loop-streaming` | A tool cannot declare itself a server-side tool type |
| Tool results are collapsed to `str` before reaching the provider | `loop-streaming` | A screenshot can never travel back as an image |

Neither is forked or patched. Both are fixed at **one seam** — the provider's
`complete()` call — by `hook-computer-use`, which:

- promotes any mounted tool exposing `native_tool_spec` to its native wire form,
- adds the required `anthropic-beta` header,
- unwraps the orchestrator's `ToolResult` envelope and expands screenshot markers into
  real image blocks,
- keeps only the **N most recent** screenshots inline so long sessions stay affordable.

Remove the hook and the tool degrades cleanly to an ordinary function tool. Nothing is
monkey-patched on disk; nothing rots when the orchestrator changes.

## Components

| Piece | Role |
|---|---|
| `modules/tool-computer-use` | `computer` (native action set) + `desktop` (windows, clipboard) |
| `modules/hook-computer-use` | The wire-format seam described above |
| `agents/computer-operator.md` | Operating discipline for driving a live machine |
| `bridge.ps1` | WSL2 → Windows execution via `powershell.exe` + Win32 |

### Tools

**`computer`** — Anthropic's action set: `screenshot`, `zoom`, `left_click`,
`right_click`, `middle_click`, `double_click`, `triple_click`, `mouse_move`,
`left_click_drag`, `left_mouse_down`, `left_mouse_up`, `scroll`, `key`, `hold_key`,
`type`, `wait`, `cursor_position`.

**`desktop`** — what the native schema cannot express: `list_windows`, `focus_window`,
`screen_info`, `get_clipboard`, `set_clipboard`. Focus a window before typing into it;
use the clipboard to pull exact text out of an app.

## Coordinates

The display is captured at full physical resolution, downscaled to a model-friendly size
(default 1280 long edge, aspect ratio preserved), and coordinates the model emits are
scaled back to physical pixels. On a 3840×2160 display that is an exact 3× factor.

Anthropic's guidance is explicit: sending screenshots above WXGA is both slower **and**
less accurate. Raising `max_edge` is usually the wrong lever.

## Configuration

```yaml
tools:
  - module: tool-computer-use
    config:
      max_edge: 1280              # long edge of the image the model sees
      tool_version: computer_20251124
      enable_zoom: true
      read_only: false            # true = screenshots only, all input blocked

hooks:
  - module: hook-computer-use
    config:
      max_inline_screenshots: 3   # older screenshots collapse to text
```

## Requirements

- WSL2 with Windows interop enabled (the default)
- An Anthropic provider on a model that supports computer use (Opus 5, Sonnet 5, …)
- Pillow (installed with the tool module)

No admin rights, no agent installed on the Windows side, no extra service.

## Safety

**There is exactly one hard control, and it is `read_only`.** Be clear about which of the
two mechanisms below you are relying on:

| Control | Kind | Strength |
|---|---|---|
| `read_only: true` | Code | **Enforced.** Every input action is rejected in `execute()` before anything reaches the desktop. Screenshots still work. |
| Stop Conditions in `agents/computer-operator.md` | Prompt | **Model judgment.** Nothing inspects the screen or blocks an action. |

The agent is instructed to stop and ask when it sees credential prompts, CAPTCHAs,
destructive confirmations, anything that would send or publish on your behalf, or a screen
that twice fails to match expectations. That instruction is followed well in practice —
but it is guidance to a model, not a gate. Treat it as such.

**On-screen content can try to manipulate the agent.** Anything the agent can read, it can
be influenced by: a dialog, a web page, or a document saying "click OK to confirm" or
"enter the password" is indistinguishable from a legitimate UI. This is an inherent risk of
computer use, not a defect in this bundle. Do not point it at untrusted screens
unsupervised, and use `read_only: true` when you only need it to look.

**The clipboard goes to your model provider.** `desktop.get_clipboard` returns the full
current Windows clipboard as tool output, which becomes part of the conversation sent to
the API. If you have just copied a password, token, or other secret, clear the clipboard
before letting the agent read it.

**Screenshots touch disk briefly.** Captures land in `%TEMP%\amplifier-computer-use\`
(cleaned after 30 minutes) and `~/.amplifier/computer-use/shots/` (cleaned after 2 hours).
On a shared machine, that is a window in which another user with filesystem access could
read images of your screen.

## Troubleshooting

Set a trace path and you get a plain-text record of what the hook actually did:

```bash
AMPLIFIER_COMPUTER_USE_TRACE=/tmp/cu-trace.log amplifier run --bundle computer-use "..."
```

```
MOUNTED max_inline=3
WRAPPED provider=AnthropicProvider module=amplifier_module_provider_anthropic
complete: tools=['computer', ...] promoted_betas=['computer-use-2025-11-24']
complete: markers=1 messages_with_blocks=3
```

`promoted_betas` empty → the tool was not promoted. No `markers=` line → screenshots are
not reaching the model.

## Tests

```bash
python tests/test_wire_format.py
```

Asserts the real bytes: native tool type present, no `parameters` key on the wire, beta
header set, screenshot markers expanded to image blocks that survive `model_dump()`
without an API-rejected `visibility` key, recency window enforced, and graceful
degradation when a screenshot file is gone.
