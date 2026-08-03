# amplifier-bundle-computer-use

Give an Amplifier session real control of a **desktop** — Windows, macOS, or Linux,
on this machine or another one across your private network — using the LLM provider's
**native computer-use tool**, not a homegrown imitation of it.

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

## The one thing that had to be solved

Amplifier's orchestrator used to stand between a mounted tool and native computer use:
tool results are collapsed to `str` before reaching the provider, so a screenshot could
never travel back as an image.

That is fixed at **one seam** — the provider's `complete()` call — by
`hook-computer-use`, which:

- unwraps the orchestrator's `ToolResult` envelope and expands screenshot markers into
  real image blocks,
- keeps only the **N most recent** screenshots inline so long sessions stay affordable.

(`ToolSpec` used to be built from `name`/`description`/`parameters` only, so a tool
couldn't declare itself a server-side tool type either — `hook-computer-use` used to
promote it and inject the beta header itself. That is now handled upstream:
`loop-streaming` preserves a tool's `native_tool_spec` through its own `ToolSpec`
construction, and `provider-anthropic` derives the required `anthropic-beta` header
itself. `hook-computer-use` now only verifies that support is present and refuses to
mount if it isn't — see `mount()`'s `_fail_if_native_tool_passthrough_unsupported`.)

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
complete: markers=1 messages_with_blocks=3
```

No `markers=` line → screenshots are not reaching the model. A mount-time
`ComputerUseNativeToolPassthroughUnsupportedError` means the installed
`loop-streaming`/`provider-anthropic` do not yet carry `computer`'s native tool form to
the wire on their own — see that error's message for the exact commit to upgrade to.

## Tests

```bash
python tests/test_wire_format.py
```

Asserts the real bytes: screenshot markers expanded to image blocks that survive
`model_dump()` without an API-rejected `visibility` key, recency window enforced, and
graceful degradation when a screenshot file is gone. (Native tool-spec passthrough is no
longer done by this hook — it's verified at mount time instead; see
`tests/test_native_tool_passthrough_guard.py`.)

---

## Credits

Originally created by [@ckrabach617](https://github.com/ckrabach617) as a
Windows-from-WSL2 bundle. That original work is preserved in this repository's
git history and its copyright is retained in `LICENSE`.

This fork extends it to a platform-backend architecture (Windows, macOS, Linux
X11), per-monitor targeting, and remote operation over a private network. See
`BACKLOG.md` for what is known and not yet done, and `docs/designs/` for the
design record.
