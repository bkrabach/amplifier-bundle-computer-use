---
name: Bug report
about: Something doesn't work the way this project says it should
title: ''
labels: bug
assignees: ''
---

<!--
Several past bugs in this project presented as one failure and were caused by
something else entirely — e.g. a missing Python dependency reported as an X server
connection failure, or a missing deployed file reported as a wire-protocol error.
Please paste the ACTUAL error text below rather than your interpretation of it; the
raw text is what lets us tell these apart quickly.
-->

## Platform

- OS of the machine being **driven** (Windows / macOS / Linux, + version):
- OS of the machine **running the agent** (if different, e.g. WSL2 on Windows):
- Python version:

## Local or remote?

- [ ] Local (agent and target desktop are the same machine)
- [ ] Remote (agent connects to a different machine over SSH — see `docs/designs/remote-transport.md`)

If remote: what is the target's OS, and is it reachable directly or via Tailscale/another
overlay network?

## What happened

Describe what you did and what you expected to happen.

## The actual error text

<!-- Paste the raw, unedited output here — stack trace, log lines, or console output. -->

```
paste here
```

## Relevant log lines

If you can reproduce with tracing enabled, paste the relevant lines:

```bash
AMPLIFIER_COMPUTER_USE_TRACE=/tmp/cu-trace.log amplifier run --bundle computer-use "..."
```

```
paste the relevant lines from /tmp/cu-trace.log here
```

## Configuration

Paste the relevant `tools:`/`hooks:` config block from your bundle/session config
(redact anything sensitive):

```yaml
paste here
```
