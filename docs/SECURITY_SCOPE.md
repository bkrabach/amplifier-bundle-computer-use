# Security scope — what this bundle's attack surface actually is

> Recovered from an earlier `SECURITY.md`. That file must carry Microsoft's
> canonical MSRC reporting process verbatim (it is a legal and process
> requirement, not boilerplate to customize), so this project-specific content
> lives here instead. **Report vulnerabilities via the process in
> [`SECURITY.md`](../SECURITY.md), not here.**

This bundle takes real control of a real desktop — locally or over SSH. That is
a broader trust boundary than most Amplifier bundles, and it is worth stating
plainly what is in scope.

Microsoft takes the security of our software products and services seriously, which
includes all source code repositories managed through our GitHub organizations, which
include [Microsoft](https://github.com/microsoft), [Azure](https://github.com/Azure),
[DotNet](https://github.com/dotnet), [AspNet](https://github.com/aspnet) and
[Xamarin](https://github.com/xamarin).

If you believe you have found a security vulnerability in any Microsoft-owned repository
that meets [Microsoft's definition of a security vulnerability](https://aka.ms/opensource/security/definition),
please report it to us as described below.

## What is in scope

This repository, `amplifier-bundle-computer-use`, lets an AI agent **see and control a
real desktop** — Windows, macOS, or Linux, local or remote over an operator-established
SSH connection — using an LLM provider's native computer-use tool. A vulnerability here is
not hypothetical: a compromise of this tool, or of the model/provider path feeding it,
could let an attacker move the mouse, type into any focused window, read whatever is on
screen, and read the clipboard on the machine it is mounted against — the same authority
a person sitting at the keyboard has. Treat reports involving any of the following as
security-relevant, not merely functional bugs:

- Anything that lets input reach the desktop, or a screenshot/clipboard leave it, **while
  `read_only: true` is configured** (the one hard code-enforced control this bundle has).
- Anything that defeats, delays, or disables the human/agent coexistence halt described in
  `docs/designs/coexistence.md` — this mechanism is intentionally **not configurable off**,
  on any platform, and a report showing it can be bypassed or silenced is a security report.
- Anything that lets the remote (SSH) transport execute actions against a target the
  operator did not establish a connection to, or that widens the trust boundary described
  in `docs/designs/remote-transport.md` beyond "if you can SSH to the box, you can drive
  its desktop."
- Anything that causes synthetic input (held keys, in-flight clicks) to persist or repeat
  after the controlling session disconnects or crashes.
- Anything that leaks a screenshot or clipboard capture to a location or audience beyond
  what is documented in `README.md`'s Safety section.

**Prompt-injection via on-screen content is a known, accepted risk of computer-use tools in
general** — a malicious dialog or web page telling the model to "click OK" or "enter the
password" is not, on its own, a vulnerability in this bundle. It is documented behavior;
see the README's Safety section. Reports in this category are welcome as hardening
suggestions but are not treated as security vulnerabilities unless they demonstrate a way
to escape the documented `read_only` or coexistence-halt boundaries above.
