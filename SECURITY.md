## Security

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

## Reporting Security Issues

**Please do not report security vulnerabilities through public GitHub issues.**

Instead, please report them to the Microsoft Security Response Center (MSRC) at
[https://aka.ms/opensource/security/create-report](https://aka.ms/opensource/security/create-report).

If you prefer to submit without logging in, send email to
[secure@microsoft.com](mailto:secure@microsoft.com). If possible, encrypt your message with
our PGP key; download it from the
[Microsoft Security Response Center PGP Key page](https://aka.ms/opensource/security/pgpkey).

You should receive a response within 24 hours. If for some reason you do not, please
follow up via email to ensure we received your original message. Additional information
can be found at [microsoft.com/msrc](https://www.microsoft.com/msrc).

Please include the requested information listed below (as much as you can provide) to
help us better understand the nature and scope of the possible issue:

  * Type of issue (e.g. buffer overflow, SQL injection, cross-site scripting, etc.)
  * Full paths of source file(s) related to the manifestation of the issue
  * The location of the affected source code (tag/branch/commit or direct URL)
  * Any special configuration required to reproduce the issue
  * Step-by-step instructions to reproduce the issue
  * Proof-of-concept or exploit code (if possible)
  * Impact of the issue, including how an attacker might exploit the issue

This information will help us triage your report more quickly.

If you are reporting for a bug bounty, more complete reports can contribute to a higher
bounty award. Please visit our [Microsoft Bug Bounty Program](https://aka.ms/opensource/security/bounty)
page for more details about our active programs.

## Preferred Languages

We prefer all communications to be in English.

## Policy

Microsoft follows the principle of [Coordinated Vulnerability Disclosure](https://aka.ms/opensource/security/cvd).
