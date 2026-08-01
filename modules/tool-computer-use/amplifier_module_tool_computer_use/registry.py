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
from typing import Any

from .backend import Backend
from .linux_x11 import LinuxX11Backend
from .macos import MacOSBackend
from .windows import WindowsBackend

logger = logging.getLogger(__name__)


class NoBackendAvailable(RuntimeError):
    """No configured backend could serve this machine."""

    def __init__(self, attempts: list[tuple[str, str]]) -> None:
        self.attempts = attempts
        if attempts:
            detail = "; ".join(f"{name}: {reason}" for name, reason in attempts)
            message = f"no computer-use backend available ({detail})"
        else:
            message = "no computer-use backends configured"
        super().__init__(message)


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


def select_backend(
    config: dict[str, Any], factories: tuple[type[Backend], ...] = BACKEND_FACTORIES
) -> Backend:
    """Probe each candidate backend in order; return the first that is available.

    Raises `NoBackendAvailable` if none can serve this machine. A backend whose
    `probe()` itself raises is treated as unavailable, not as a fatal error - probing
    must never be able to take down mount().
    """
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
