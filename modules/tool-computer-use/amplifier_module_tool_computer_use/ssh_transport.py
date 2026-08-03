"""SSH transport: one persistent `ssh -T` subprocess per session, carrying both
the one-time code deployment and the NDJSON protocol session on the same
stdin/stdout pipe pair - `docs/remote-transport.md` \u00a77-8.

No network daemon, no new listening port, no bespoke auth: "if you can SSH to
the box, you can drive its desktop" (\u00a72). This module owns exactly the
SSH subprocess lifecycle and the bootstrap deploy; wire framing lives in
`wire.py`, op dispatch lives in `remote_backend.py`.
"""

from __future__ import annotations

import hashlib
import io
import logging
import re
import shlex
import subprocess
import tarfile
import threading
from pathlib import Path
from typing import Any

from .wire import Response, validate_handshake

logger = logging.getLogger(__name__)

#: This bundle's own files needed on the target - zero amplifier_core
#: dependency (see pyproject.toml), so this is the complete, self-contained
#: set `remote_agent.py` needs to run standalone. `windows.py` is included
#: unconditionally: it imports nothing platform-specific at module level (only
#: `subprocess`/`shutil`/`tempfile`), so it is safe to ship even to a
#: macOS/Linux target - `registry.select_backend()` just never picks it there.
#: Deliberately EXCLUDES this package's real `__init__.py` (`ComputerTool`,
#: `DesktopTool`, `mount()`) - that file imports `amplifier_core`, which the
#: target has never heard of. `_build_payload` synthesizes an empty
#: `__init__.py` for the deployed package instead (see below).
PAYLOAD_MODULES = (
    "backend.py",
    "geometry.py",
    "imaging.py",
    "monitors.py",
    "registry.py",
    "linux_x11.py",
    "macos.py",
    "windows.py",
    "wire.py",
    "ledger.py",
    "remote_agent.py",
    # NOT a Python module, and required anyway: `windows.py` resolves
    # `BRIDGE_PS1 = Path(__file__).parent / "bridge.ps1"` and invokes it with
    # `powershell.exe -File`. Omitting it does not fail at import - it fails at
    # the first action, where PowerShell is handed a path that does not exist,
    # prints its startup banner, and exits. The banner then arrives where JSON
    # was expected, which reads like a bridge/protocol bug rather than a missing
    # file. Verified against a live WSL target: handshake succeeded and
    # `screen_info` returned "Copyright (C) Microsoft Corporation..." until this
    # file was shipped.
    "bridge.ps1",
)

_PACKAGE_NAME = "amplifier_cu_agent"

#: Absolute-path candidates for `uv`, in order - the same "don't trust PATH in
#: a non-login shell" lesson `windows.py::_which_powershell` already encodes
#: (\u00a73.1: `uv` was found on PATH here, but a non-login remote shell is not
#: guaranteed to have it). Verified locations for this design's two real
#: targets are tried first; bare `uv` (in case PATH does carry it) is tried
#: too, cheaply, as part of the same one-line remote probe.
UV_CANDIDATES = (
    "uv",
    "$HOME/.local/bin/uv",
    "/opt/homebrew/bin/uv",
    "/usr/local/bin/uv",
)

#: Same discipline as the human-verified SSH invocation in the task brief:
#: batch mode (never prompts), no controlmaster/multiplexing surprises,
#: short connect timeout, keepalives so a half-open connection is detected
#: from the controller side too (the agent-side deadman is what actually
#: protects the desktop - \u00a710.2).
_SSH_OPTS = [
    "-o",
    "BatchMode=yes",
    "-o",
    "StrictHostKeyChecking=accept-new",
    "-o",
    "ConnectTimeout=5",
    "-o",
    "ServerAliveInterval=5",
    "-o",
    "ServerAliveCountMax=2",
    "-o",
    "ControlMaster=no",
    "-o",
    "ControlPath=none",
]


class SshConnectError(RuntimeError):
    """The SSH session could not be established or the deploy/handshake failed."""


def _build_payload(package_dir: Path) -> bytes:
    """Deterministic tar.gz of `PAYLOAD_MODULES`, rooted at `_PACKAGE_NAME/`."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name in PAYLOAD_MODULES:
            path = package_dir / name
            data = path.read_bytes()
            info = tarfile.TarInfo(name=f"{_PACKAGE_NAME}/{name}")
            info.size = len(data)
            info.mtime = 0
            info.mode = 0o644
            tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _resolve_uv_command(user_host: str, ssh_path: str = "ssh") -> str:
    """Discover an absolute `uv` path on the target via a short, bounded probe.

    Uses Python's own `subprocess.run(..., timeout=...)`, not a shell
    `timeout` wrapper - the task brief's own incident (a bare blocking `ssh`
    hanging 6h55m past both an inner and outer timeout) was specifically a
    shell-level `timeout`+backgrounding interaction; `subprocess.run(timeout=)`
    kills the `ssh` process directly on expiry, which is not subject to that
    failure mode.
    """
    probe = " || ".join(f"command -v {shlex.quote(c)}" for c in UV_CANDIDATES)
    cmd = [ssh_path, "-n", *_SSH_OPTS, user_host, f"sh -lc {shlex.quote(probe)}"]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=15, check=False
        )
    except subprocess.TimeoutExpired as exc:
        raise SshConnectError(f"timed out probing for uv on {user_host}") from exc
    found = proc.stdout.strip().splitlines()[-1].strip() if proc.stdout.strip() else ""
    if not found:
        raise SshConnectError(
            f"could not find 'uv' on {user_host} (tried: {list(UV_CANDIDATES)}; "
            f"stderr: {proc.stderr.strip()[:300]!r})"
        )
    return found


_SIZE_LINE_RE = re.compile(rb"^\d+$")


def _bootstrap_stub(deadman_seconds: float, read_only: bool) -> str:
    """The pure-Python stub run as the SSH remote command's payload consumer.

    Reads a `SIZE\\n` line, then exactly that many raw bytes
    (`sys.stdin.buffer.read(n)` - the \u00a73.3-proven safe primitive; `head -c
    N` is explicitly unsafe here, it over-reads and swallows the NDJSON
    requests that follow), extracts the tar.gz into a scratch dir, and runs
    the agent IN-PROCESS via `runpy` (an `os.exec*` would discard whatever of
    stdin Python has already buffered past the tar bytes).
    """
    read_only_arg = "true" if read_only else "false"
    return (
        "import sys,os,tarfile,io,runpy,hashlib,tempfile;"
        "buf=sys.stdin.buffer;"
        "n=int(buf.readline().strip());"
        "data=buf.read(n);"
        "sys.exit(97) if len(data)!=n else None;"
        "d=hashlib.sha256(data).hexdigest();"
        "w=tempfile.mkdtemp(prefix='amplifier-cu-agent-');"
        "tarfile.open(fileobj=io.BytesIO(data),mode='r:gz').extractall(w);"
        "sys.path.insert(0,w);"
        "os.environ['AMPLIFIER_CU_AGENT_SHA256']=d;"
        f"sys.argv=['remote_agent','--deadman-seconds={deadman_seconds}',"
        f"'--read-only={read_only_arg}'];"
        "runpy.run_module('amplifier_cu_agent.remote_agent',run_name='__main__')"
    )


class SshTransport:
    """Owns one `ssh -T` subprocess for the life of a remote-backend session.

    `connect()` deploys the agent payload and performs the handshake;
    `send()` writes one request line and blocks for the matching response
    line (Phase 1 does not pipeline); `close()` runs the documented shutdown
    order (\u00a710.1): `release_all` -> `bye` -> close stdin -> wait -> SIGTERM
    -> wait -> SIGKILL.
    """

    def __init__(
        self,
        user_host: str,
        *,
        package_dir: Path,
        ssh_path: str = "ssh",
        deadman_seconds: float = 5.0,
        read_only: bool = True,
        with_pillow: bool = True,
    ) -> None:
        self.user_host = user_host
        self._package_dir = package_dir
        self._ssh_path = ssh_path
        self._deadman_seconds = deadman_seconds
        self._read_only = read_only
        self._with_pillow = with_pillow
        self._proc: subprocess.Popen[bytes] | None = None
        self._lock = threading.Lock()
        self._sent_sha256: str | None = None
        self.handshake: dict[str, Any] | None = None

    def connect(
        self,
        *,
        required_permissions: tuple[str, ...] = (),
        connect_timeout: float = 30.0,
    ) -> dict[str, Any]:
        payload = _build_payload(self._package_dir)
        self._sent_sha256 = hashlib.sha256(payload).hexdigest()

        uv_cmd = _resolve_uv_command(self.user_host, self._ssh_path)
        stub = _bootstrap_stub(self._deadman_seconds, self._read_only)
        if self._with_pillow:
            # Mirrors this bundle's own pyproject.toml dependency markers
            # exactly (pyobjc-framework-Quartz only on darwin, python-xlib
            # only on linux) - `uv run --with` accepts a full PEP 508
            # requirement string, environment marker included, so uv installs
            # only what the TARGET's actual platform needs. Neither backend
            # module is importable without its native extension (Quartz/Xlib)
            # - see the guarded-import fix in linux_x11.py/macos.py - so
            # skipping the wrong one here is not a functional loss, only a
            # smaller ephemeral env.
            extras = [
                "pillow",
                'pyobjc-framework-Quartz; sys_platform=="darwin"',
                'python-xlib; sys_platform=="linux"',
            ]
            with_args = " ".join(f"--with {shlex.quote(e)}" for e in extras)
            remote_cmd = (
                f"{shlex.quote(uv_cmd)} run {with_args} python3 -c {shlex.quote(stub)}"
            )
        else:
            remote_cmd = f"python3 -c {shlex.quote(stub)}"

        cmd = [self._ssh_path, "-T", *_SSH_OPTS, self.user_host, remote_cmd]
        logger.info("ssh-transport: connecting to %s", self.user_host)
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
        )
        self._proc = proc
        assert proc.stdin is not None and proc.stdout is not None

        try:
            size_line = f"{len(payload)}\n".encode()
            proc.stdin.write(size_line)
            proc.stdin.write(payload)
            proc.stdin.flush()
        except BrokenPipeError as exc:
            self._drain_stderr_on_failure()
            raise SshConnectError(
                f"broken pipe writing payload to {self.user_host} - deploy failed"
            ) from exc

        line = self._read_line_with_timeout(proc.stdout, connect_timeout)
        if line is None:
            self._drain_stderr_on_failure()
            raise SshConnectError(
                f"no handshake from {self.user_host} within {connect_timeout}s "
                "(agent may have crashed during bootstrap - see stderr)"
            )
        resp = Response.decode(line)
        if not resp.ok or not isinstance(resp.result, dict):
            raise SshConnectError(
                f"malformed handshake from {self.user_host}: {line!r}"
            )

        validate_handshake(
            resp.result,
            expected_sha256=self._sent_sha256,
            required_permissions=required_permissions,
        )
        self.handshake = resp.result
        logger.info(
            "ssh-transport: handshake ok backend=%s platform=%s",
            resp.result.get("backend"),
            resp.result.get("platform"),
        )
        return resp.result

    def send(self, line: bytes, *, timeout: float = 30.0) -> bytes:
        """Write one request line, block for exactly the matching response
        line. Phase 1 is not pipelined - one in flight at a time - so
        correlation by `id` is a cheap sanity check here, not real multiplexing."""
        proc = self._proc
        if proc is None or proc.stdin is None or proc.stdout is None:
            raise SshConnectError("not connected")
        with self._lock:
            try:
                proc.stdin.write(line)
                proc.stdin.flush()
            except BrokenPipeError as exc:
                raise SshConnectError(
                    f"connection to {self.user_host} lost while sending"
                ) from exc
            resp_line = self._read_line_with_timeout(proc.stdout, timeout)
            if resp_line is None:
                self._drain_stderr_on_failure()
                raise SshConnectError(
                    f"no response from {self.user_host} within {timeout}s "
                    "(connection likely lost)"
                )
            # Surface the agent's own stderr whenever it reports an error.
            # Previously stderr was drained ONLY on connect failure, so a
            # request-level error arrived as a bare message with the agent's
            # side of the story discarded - which is exactly the case where it
            # matters most. A platform API that fails by returning nothing
            # (macOS `CGDisplayCreateImage` under a denied TCC grant is the
            # example that motivated this) leaves no other trace at all.
            if b'"error' in resp_line or b'"ok": false' in resp_line:
                self._drain_stderr_on_failure()
            return resp_line

    def _read_line_with_timeout(self, stream: Any, timeout: float) -> bytes | None:
        result: dict[str, bytes | None] = {"line": None}

        def _read() -> None:
            result["line"] = stream.readline() or None

        t = threading.Thread(target=_read, daemon=True)
        t.start()
        t.join(timeout)
        return result["line"]

    def _drain_stderr_on_failure(self) -> None:
        if self._proc is None or self._proc.stderr is None:
            return
        try:
            data = self._proc.stderr.read(4096)
            if data:
                logger.error(
                    "ssh-transport: agent stderr: %s", data.decode(errors="replace")
                )
        except Exception as exc:  # noqa: BLE001 - best-effort diagnostics only
            logger.debug("ssh-transport: stderr drain failed: %s", exc)

    def close(self) -> None:
        """Shutdown order per \u00a710.1: release_all -> bye -> close stdin ->
        wait 2s -> SIGTERM -> wait 2s -> SIGKILL."""
        proc = self._proc
        if proc is None:
            return
        try:
            from .wire import Request

            try:
                self.send(Request(id=999_000_001, op="release_all").encode(), timeout=5)
            except Exception as exc:  # noqa: BLE001 - best-effort on the way out
                logger.debug("ssh-transport: release_all on close failed: %s", exc)
            try:
                self.send(Request(id=999_000_002, op="bye").encode(), timeout=5)
            except Exception as exc:  # noqa: BLE001 - best-effort on the way out
                logger.debug("ssh-transport: bye on close failed: %s", exc)
        finally:
            if proc.stdin is not None:
                try:
                    proc.stdin.close()
                except OSError:
                    pass
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.terminate()
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    proc.kill()
            self._proc = None
