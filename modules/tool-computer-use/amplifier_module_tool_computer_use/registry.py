"""Backend selection: probe candidates in order, use the first one that works.

D1 fix lives here. `mount()` must never register a tool that cannot possibly work on
this machine - this module is where that decision gets made, once, before any tool is
mounted. Every candidate backend gets a cheap `probe()` (see `backend.Backend.probe`);
the first one that reports itself available is returned. If none can serve this
machine, `select_backend` raises `NoBackendAvailable` with every attempt's reason, and
`mount()` is expected to catch it, log it clearly, and mount nothing.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from .backend import Backend
from .linux_x11 import LinuxX11Backend
from .macos import MacOSBackend
from .windows import WindowsBackend

logger = logging.getLogger(__name__)


#: Appended to `NoBackendAvailable`'s message - the "what to do next" a bare
#: exception type name and a per-backend reason string do not supply on their
#: own. `mount()` (`tool-computer-use/__init__.py`) logs this whole message via
#: `logger.warning` and echoes it into the mounted-module manifest's
#: `description` field, so this text - not a traceback - is what an operator
#: actually sees when computer-use silently isn't there. Deliberately generic
#: (not per-backend) because `attempts` already carries the specific reason for
#: each candidate that was actually tried; this is the part that reason text
#: cannot supply on its own - the remediation options that apply regardless of
#: which specific backend failed and why.
_REMEDIATION = (
    "What to do: fix the backend that applies to this machine (see its reason "
    "above - e.g. a missing dependency, no DISPLAY/XAUTHORITY, powershell.exe "
    "unreachable, or a missing macOS Accessibility/Screen Recording grant), or "
    "set config.target='ssh://user@host' to drive a different, reachable "
    "machine instead. See docs/SETUP.md \u00a74 for per-platform backend "
    "requirements."
)


class NoBackendAvailable(RuntimeError):
    """No configured backend could serve this machine.

    This is the actual operator-facing message a human sees when computer-use
    refuses to mount - not a bare exception type name. Three parts, in order:
    WHAT happened (no backend available), WHY (every candidate's own probe
    reason - `attempts` - so a real X11 connection failure is never confused
    with a missing dependency), and WHAT TO DO about it (`_REMEDIATION`).
    Fail-loud-to-the-system (refusing to mount a tool that cannot work) and
    fail-loud-to-the-human-who-has-to-act-on-it (telling them what to actually
    do) are different properties; this exists so both are built, not just the
    first.
    """

    def __init__(self, attempts: list[tuple[str, str]]) -> None:
        self.attempts = attempts
        if attempts:
            detail = "; ".join(f"{name}: {reason}" for name, reason in attempts)
            message = f"no computer-use backend available ({detail}). {_REMEDIATION}"
        else:
            message = (
                "no computer-use backends configured (registry.BACKEND_FACTORIES "
                "is empty). What to do: this is a packaging/config bug, not a "
                "normal environment problem - file an issue against "
                "amplifier-bundle-computer-use."
            )
        super().__init__(message)


#: `ssh://user@host` (or `ssh://host` - user optional, taken from the local
#: SSH config/default in that case).
_SSH_TARGET_RE = re.compile(r"^ssh://(?:(?P<user>[^@]+)@)?(?P<host>[^/]+)$")


def _parse_target(target: str) -> str:
    """Return the `[user@]host` string `ssh` itself expects, or raise
    `ValueError` for anything that isn't a well-formed `ssh://` target - a
    malformed config value should fail loud with a clear parse error, not
    silently be handed to `ssh` as a garbage argument."""
    match = _SSH_TARGET_RE.match(target.strip())
    if not match:
        raise ValueError(
            f"config.target={target!r} is not a valid ssh:// target "
            "(expected 'ssh://user@host' or 'ssh://host')"
        )
    user, host = match.group("user"), match.group("host")
    return f"{user}@{host}" if user else host


#: Probe order. Windows-over-WSL2 first preserves today's default behavior; Linux X11
#: is only tried if the Windows bridge is not reachable (e.g. a bare Linux box with no
#: `powershell.exe`, such as this bundle's original test box). macOS is only tried if
#: neither of those is reachable (e.g. a bare macOS box) - its `probe()` returns
#: unavailable immediately on any non-Darwin platform, so trying it earlier would cost
#: nothing functionally, but this order keeps the two previously-verified platforms'
#: behavior completely undisturbed by this addition.
BACKEND_FACTORIES: tuple[type[Backend], ...] = (
    WindowsBackend,
    LinuxX11Backend,
    MacOSBackend,
)


def _build_ssh_transport(host: str, package_dir: Any, config: dict[str, Any]) -> Any:
    """Return a per-target SHARED transport handle for a remote target.

    Singleton fix: this used to construct a brand-new `SshTransport` (hence a
    brand-new SSH subprocess, hence a brand-new remote agent process on the
    target) on EVERY call - so a parent session's own `computer`/`desktop`
    tools and a delegated `computer-operator` child session's `mount()` each
    built their OWN transport for the SAME target. Two concurrent agent
    processes against the same macOS target corrupt each other's Screen
    Recording TCC grant: `CGDisplayCreateImage` then returns `None` for BOTH
    agents - including the one that was capturing successfully a moment
    earlier - with no exception raised on either side (see `macos.py`).

    `shared_transport.acquire_shared_transport` makes every consumer in this
    process that resolves to the SAME `(ssh_path, host)` key share ONE
    underlying `SshTransport`/agent process, refcounted so the last consumer
    to release it is the one that actually tears it down.

    Kept separate from `select_backend` so tests can monkeypatch this one
    function to inject a fake transport without touching real SSH at all -
    that seam is unchanged; only what it returns (a shared handle instead of
    a bare `SshTransport`) is different, and `RemoteBackend` needs no changes
    since the handle duck-types the same `connect()`/`send()`/`close()`
    surface.
    """
    from .shared_transport import acquire_shared_transport
    from .ssh_transport import SshTransport

    ssh_path = str(config.get("ssh_path", "ssh"))

    def _factory() -> SshTransport:
        return SshTransport(
            host,
            package_dir=package_dir,
            ssh_path=ssh_path,
            deadman_seconds=float(config.get("deadman_seconds", 5.0)),
            read_only=bool(config.get("read_only", True)),
            with_pillow=bool(config.get("with_pillow", True)),
        )

    return acquire_shared_transport((ssh_path, host), _factory)


def select_backend(
    config: dict[str, Any], factories: tuple[type[Backend], ...] = BACKEND_FACTORIES
) -> Backend:
    """Probe each candidate backend in order; return the first that is available.

    C3: `config["target"]` absent -> exactly today's behavior, unchanged
    (probe local backends in order, `NoBackendAvailable` degrades to a silent
    skip in `mount()`). `config["target"]` present (`ssh://user@host`) ->
    `RemoteBackend` is the ONLY candidate - no probe-fallthrough to a local
    backend. An unreachable explicit target raises `RemoteTargetUnavailable`,
    which `mount()` does NOT catch (unlike `NoBackendAvailable`): the agent
    must never silently fall back to driving the controller's own local
    desktop when a specific remote machine was asked for and could not be
    reached (\u00a79 / acceptance item 7).
    """
    target = config.get("target")
    if target:
        # Import here, not at module top: remote_backend.py pulls in
        # ssh_transport.py (subprocess/tarfile) which local-only callers
        # (including remote_agent.py itself, which never uses `target`)
        # have no reason to load.
        from pathlib import Path

        from .remote_backend import RemoteBackend

        host = _parse_target(str(target))
        package_dir = Path(__file__).parent
        backend = RemoteBackend(
            {
                "_host": host,
                "_transport": _build_ssh_transport(host, package_dir, config),
            }
        )
        backend.connect(
            required_permissions=tuple(config.get("required_permissions") or ()),
            connect_timeout=float(config.get("connect_timeout", 30.0)),
        )
        logger.info(
            "computer-use: selected remote backend %r (target=%r)", backend.name, target
        )
        return backend

    attempts: list[tuple[str, str]] = []
    for factory in factories:
        backend = factory(config)  # construction is cheap; probing is the real check
        try:
            result = backend.probe()
        except Exception as exc:
            logger.exception("computer-use: %s.probe() raised", factory.__name__)
            attempts.append(
                (
                    getattr(backend, "name", factory.__name__),
                    f"probe raised {type(exc).__name__}: {exc}",
                )
            )
            continue
        if result.available:
            logger.info("computer-use: selected backend %r", backend.name)
            return backend
        attempts.append((backend.name, result.reason or "unavailable"))
        logger.info(
            "computer-use: backend %r unavailable (%s)", backend.name, result.reason
        )
    raise NoBackendAvailable(attempts)
