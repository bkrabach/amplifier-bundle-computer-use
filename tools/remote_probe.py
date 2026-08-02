"""Remote probe harness: deploy a script to a remote host, run it, pull
results, tear down - with the SSH discipline this project learned the hard
way, baked in rather than re-derived per ad hoc script.

**The incident this guards against.** A blocking `ssh` invocation, wrapped
in a shell `timeout`, once hung for 6 hours 55 minutes: `timeout` killed the
`ssh` process, but a forked child on the far end of the pipe kept the local
process's stdout pipe open, so a caller reading that pipe with a blocking
`.communicate()`/`.read()` blocked forever - past both the inner and the
outer timeout - because killing the process that owned the pipe's *write
end* does not force the pipe closed while some other process still holds a
copy of that descriptor open. Two independent defenses are used here,
together:

1. **Never block the foreground on a read.** Every ssh/scp invocation is
   spawned via `subprocess.Popen` and its stdout/stderr pipes are drained
   continuously by background threads for the entire lifetime of the call
   (`_run_ssh`). The main thread never calls `.communicate()` or a raw
   `.read()` - it only ever polls `proc.poll()` on a sleep loop and enforces
   its own wall-clock deadline, so a wedged pipe cannot hang the caller
   regardless of what is or is not still holding it open.
2. **Never assume the `timeout` binary.** macOS ships no coreutils
   `timeout` by default. Every bound here is enforced in Python
   (`_poll_until_done`'s deadline), not by shelling out to `timeout ssh ...`.

**scp needs its own channel.** `-n` (redirect stdin from `/dev/null`) is
correct for `ssh` running a remote command - it stops a remote command that
happens to read stdin from consuming the local terminal / blocking on it -
but `scp` does its own file-transfer protocol over stdin/stdout, and `-n`
would starve it. So `-n` is applied to every `ssh` invocation and *never* to
`scp` invocations, both of which reuse the same base `_SSH_OPTS`.

**WSL interop.** A target reached over SSH that turns out to be a WSL
shell has its own trap: `/mnt/c` is not on `PATH` in a non-login remote
shell, so `shutil.which("powershell.exe")` (or a bare `command -v
powershell.exe`) fails even though PowerShell is right there.
`resolve_powershell_over_ssh` probes the real interop path directly instead
of trusting `PATH`, and `to_windows_path_over_ssh` uses `wslpath -w` to
convert a WSL path to the Windows-style path PowerShell itself needs -
never a hardcoded `/mnt/c` assumption (WSL's automount root is
configurable).
"""

from __future__ import annotations

import logging
import shlex
import subprocess
import tempfile
import threading
import time
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)


class RemoteProbeError(RuntimeError):
    """The deploy/run/collect/teardown sequence could not complete."""


#: Discipline learned the hard way (module docstring): batch mode so nothing
#: ever prompts interactively and hangs a script; short connect timeout so a
#: dead host fails fast; no multiplexing/control-socket surprises;
#: keepalives so a half-open connection is noticed. Deliberately excludes
#: `-n` - see `_ssh_cmd`/`_scp_cmd` below, which add it only for `ssh`.
_SSH_OPTS: tuple[str, ...] = (
    "-o",
    "BatchMode=yes",
    "-o",
    "ConnectTimeout=5",
    "-o",
    "ControlMaster=no",
    "-o",
    "ControlPath=none",
    "-o",
    "ServerAliveInterval=5",
)


def _ssh_cmd(ssh_path: str, host: str, remote_command: str) -> list[str]:
    """`-n` IS included here - a remote command must never be able to read
    from (and block on) this process's stdin."""
    return [ssh_path, "-n", *_SSH_OPTS, host, remote_command]


def _scp_cmd(scp_path: str, *args: str) -> list[str]:
    """`-n` is deliberately ABSENT - scp needs stdin/stdout for its own
    transfer protocol (see module docstring)."""
    return [scp_path, *_SSH_OPTS, *args]


def _run_ssh(
    cmd: list[str], *, timeout_s: float, poll_interval_s: float = 0.2
) -> tuple[bytes, bytes, int | None, bool]:
    """Run `cmd`, draining stdout/stderr continuously via background
    threads so a wedged or talkative remote process can never fill and
    block a pipe the caller would otherwise read from later, while the
    foreground ONLY polls `proc.poll()` on a sleep loop and enforces
    `timeout_s` itself.

    Returns `(stdout_bytes, stderr_bytes, returncode_or_None, timed_out)`.
    `returncode` is `None` only if even a SIGKILL did not reap the process
    before this function returned (should not happen on any real system,
    but this is reported honestly rather than assumed).
    """
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL
    )
    out_chunks: list[bytes] = []
    err_chunks: list[bytes] = []

    def _drain(stream, sink: list[bytes]) -> None:
        try:
            for chunk in iter(lambda: stream.read(4096), b""):
                sink.append(chunk)
        except (ValueError, OSError):
            # Stream closed out from under the reader (e.g. process killed
            # mid-read) - the drain's job is best-effort capture, not to
            # propagate a race as a crash.
            pass

    assert proc.stdout is not None and proc.stderr is not None
    t_out = threading.Thread(target=_drain, args=(proc.stdout, out_chunks), daemon=True)
    t_err = threading.Thread(target=_drain, args=(proc.stderr, err_chunks), daemon=True)
    t_out.start()
    t_err.start()

    deadline = time.monotonic() + timeout_s
    timed_out = False
    while True:
        rc = proc.poll()
        if rc is not None:
            break
        if time.monotonic() >= deadline:
            timed_out = True
            proc.terminate()
            term_deadline = time.monotonic() + 2.0
            while proc.poll() is None and time.monotonic() < term_deadline:
                time.sleep(0.1)
            if proc.poll() is None:
                proc.kill()
            break
        time.sleep(poll_interval_s)

    # Bounded flush of whatever the (now-finished-or-killed) process already
    # wrote. Bounded, not blocking-forever - a reader thread stuck on a
    # descriptor some grandchild still holds open is abandoned here (it is a
    # daemon thread; the process exits fine without it ever finishing), not
    # waited on indefinitely - that is precisely the failure mode this
    # module exists to avoid reproducing.
    t_out.join(timeout=2.0)
    t_err.join(timeout=2.0)
    return b"".join(out_chunks), b"".join(err_chunks), proc.poll(), timed_out


@dataclass(frozen=True)
class RemoteProbeResult:
    host: str
    exit_code: int | None
    stdout: str
    stderr: str
    timed_out: bool
    duration_s: float
    remote_script_path: str
    pulled_paths: dict[str, Path] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.timed_out and self.exit_code == 0


def deploy_run_collect(
    host: str,
    local_script: str | Path,
    *,
    script_args: Sequence[str] = (),
    remote_dir: str | None = None,
    pull_paths: Sequence[str] = (),
    pull_dest: str | Path | None = None,
    timeout_s: float = 60.0,
    poll_interval_s: float = 0.2,
    ssh_path: str = "ssh",
    scp_path: str = "scp",
    keep_remote: bool = False,
) -> RemoteProbeResult:
    """Deploy `local_script` to `host`, run it, pull back any files named in
    `pull_paths`, then remove the remote working directory - end to end, no
    step of which ever blocks the foreground past its own bounded timeout.

    Raises `RemoteProbeError` if deploy itself fails (mkdir/scp) - a script
    that could not even be copied over is a setup failure, not a probe
    result. A script that deploys and RUNS but exits nonzero, or times out,
    is not an exception - that is exactly what `RemoteProbeResult.exit_code`
    / `.timed_out` are for; the caller decides what a nonzero/timeout result
    means for their probe.
    """
    local_script = Path(local_script)
    if not local_script.is_file():
        raise RemoteProbeError(f"local script not found: {local_script}")

    remote_dir = remote_dir or f"/tmp/amplifier_remote_probe_{uuid.uuid4().hex[:10]}"
    remote_script = f"{remote_dir}/{local_script.name}"

    # 1. mkdir the remote working directory.
    _, err, rc, timed_out = _run_ssh(
        _ssh_cmd(ssh_path, host, f"mkdir -p {shlex.quote(remote_dir)}"), timeout_s=15
    )
    if timed_out or rc != 0:
        raise RemoteProbeError(
            f"mkdir on {host} failed: rc={rc} timed_out={timed_out} "
            f"stderr={err.decode(errors='replace')[:500]!r}"
        )

    # 2. deploy via scp - its OWN channel, no -n (see module docstring).
    _, err, rc, timed_out = _run_ssh(
        _scp_cmd(scp_path, str(local_script), f"{host}:{remote_script}"), timeout_s=30
    )
    if timed_out or rc != 0:
        raise RemoteProbeError(
            f"scp deploy to {host} failed: rc={rc} timed_out={timed_out} "
            f"stderr={err.decode(errors='replace')[:500]!r}"
        )

    # 3. run - backgrounded + polled locally, bounded by timeout_s.
    args_str = " ".join(shlex.quote(a) for a in script_args)
    remote_command = (
        f"chmod +x {shlex.quote(remote_script)} && {shlex.quote(remote_script)}"
    )
    if args_str:
        remote_command += f" {args_str}"
    started = time.monotonic()
    out, err, rc, timed_out = _run_ssh(
        _ssh_cmd(ssh_path, host, remote_command),
        timeout_s=timeout_s,
        poll_interval_s=poll_interval_s,
    )
    duration_s = time.monotonic() - started

    # 4. pull results (optional) - best-effort per file; a failed pull is
    # logged, never silently substitutes a fabricated success.
    pulled: dict[str, Path] = {}
    if pull_paths:
        dest = (
            Path(pull_dest)
            if pull_dest
            else Path(tempfile.mkdtemp(prefix="remote_probe_pull_"))
        )
        dest.mkdir(parents=True, exist_ok=True)
        for remote_path in pull_paths:
            local_path = dest / Path(remote_path).name
            _, perr, prc, ptimed_out = _run_ssh(
                _scp_cmd(scp_path, f"{host}:{remote_path}", str(local_path)),
                timeout_s=30,
            )
            if ptimed_out or prc != 0:
                logger.warning(
                    "remote-probe: failed to pull %s from %s: rc=%s timed_out=%s stderr=%s",
                    remote_path,
                    host,
                    prc,
                    ptimed_out,
                    perr.decode(errors="replace")[:300],
                )
                continue
            pulled[remote_path] = local_path

    # 5. teardown - best-effort; a failed cleanup is logged, not raised (the
    # probe's own result is already captured and must not be lost over it).
    if not keep_remote:
        _, rm_err, rm_rc, rm_timed_out = _run_ssh(
            _ssh_cmd(ssh_path, host, f"rm -rf {shlex.quote(remote_dir)}"), timeout_s=15
        )
        if rm_timed_out or rm_rc != 0:
            logger.warning(
                "remote-probe: teardown of %s on %s failed: rc=%s timed_out=%s stderr=%s",
                remote_dir,
                host,
                rm_rc,
                rm_timed_out,
                rm_err.decode(errors="replace")[:300],
            )

    return RemoteProbeResult(
        host=host,
        exit_code=rc,
        stdout=out.decode("utf-8", errors="replace"),
        stderr=err.decode("utf-8", errors="replace"),
        timed_out=timed_out,
        duration_s=duration_s,
        remote_script_path=remote_script,
        pulled_paths=pulled,
    )


#: Absolute-path candidates for PowerShell over a WSL-reached target -
#: mirrors the `command -v ... || command -v ...` discipline this bundle's
#: own `ssh_transport.py::_resolve_uv_command` already uses for `uv`, for
#: the identical reason: a non-login remote shell's PATH does not carry
#: `/mnt/c`, so a bare `command -v powershell.exe` fails even when
#: PowerShell is right there. Kept here as new, independent code - not an
#: import from `ssh_transport.py` - per the task's "do not modify" scope.
_POWERSHELL_CANDIDATES: tuple[str, ...] = (
    "powershell.exe",
    "/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe",
)


def resolve_powershell_over_ssh(
    host: str, *, ssh_path: str = "ssh", timeout_s: float = 15.0
) -> str:
    """Resolve an absolute path to `powershell.exe` on a WSL target reached
    over SSH, without trusting `PATH` (see module docstring)."""
    probe = " || ".join(f"command -v {shlex.quote(c)}" for c in _POWERSHELL_CANDIDATES)
    out, err, rc, timed_out = _run_ssh(
        _ssh_cmd(ssh_path, host, f"sh -lc {shlex.quote(probe)}"), timeout_s=timeout_s
    )
    if timed_out:
        raise RemoteProbeError(f"timed out probing for powershell.exe on {host}")
    found = (
        out.decode(errors="replace").strip().splitlines()[-1].strip()
        if out.strip()
        else ""
    )
    if rc != 0 or not found:
        raise RemoteProbeError(
            f"could not find powershell.exe on {host} "
            f"(tried: {list(_POWERSHELL_CANDIDATES)}; stderr={err.decode(errors='replace')[:300]!r})"
        )
    return found


def to_windows_path_over_ssh(
    host: str, wsl_path: str, *, ssh_path: str = "ssh", timeout_s: float = 15.0
) -> str:
    """Convert a WSL-side path to its Windows-style equivalent via
    `wslpath -w`, run on the target - never a hardcoded `/mnt/c` assumption
    (WSL's automount root is configurable per `/etc/wsl.conf`)."""
    out, err, rc, timed_out = _run_ssh(
        _ssh_cmd(ssh_path, host, f"wslpath -w {shlex.quote(wsl_path)}"),
        timeout_s=timeout_s,
    )
    if timed_out or rc != 0:
        raise RemoteProbeError(
            f"wslpath -w {wsl_path!r} on {host} failed: rc={rc} timed_out={timed_out} "
            f"stderr={err.decode(errors='replace')[:300]!r}"
        )
    return out.decode(errors="replace").strip()


def _main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "host", help="SSH destination, e.g. user@host or a config alias"
    )
    parser.add_argument("script", help="local script to deploy and run")
    parser.add_argument("--arg", action="append", default=[], dest="script_args")
    parser.add_argument("--timeout", type=float, default=60.0)
    parser.add_argument(
        "--pull", action="append", default=[], help="remote path to pull back"
    )
    args = parser.parse_args(argv)

    result = deploy_run_collect(
        args.host,
        args.script,
        script_args=args.script_args,
        pull_paths=args.pull,
        timeout_s=args.timeout,
    )
    print(f"host:        {result.host}")
    print(f"exit_code:   {result.exit_code}")
    print(f"timed_out:   {result.timed_out}")
    print(f"duration_s:  {result.duration_s:.2f}")
    print(f"stdout:\n{result.stdout}")
    if result.stderr:
        print(f"stderr:\n{result.stderr}")
    if result.pulled_paths:
        print(f"pulled: {result.pulled_paths}")
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(_main())
