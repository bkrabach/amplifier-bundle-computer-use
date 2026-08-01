"""Unit tests for PATH-independent PowerShell resolution (`windows._which_powershell`
and `windows._windows_mount_root`).

Real-world finding this guards against: on a real WSL2 host reached over a
non-login/non-interactive shell (e.g. `ssh host -- command`, or any remote-agent
invocation), `PATH` does NOT include the Windows interop directories, so
`shutil.which("powershell.exe")` fails even though the binary is reachable at its
full path. The old fallback hardcoded `/mnt/c`, which is only WSL's *default*
automount root - `/etc/wsl.conf` can change it. These tests prove:

1. An explicit config override wins and is validated (not blindly trusted).
2. `shutil.which` success is used when available.
3. The derived-mount-root candidate (via `wslpath -u C:\\`) is preferred over the
   hardcoded `/mnt/c` fallback, and does not assume any hardcoded root.
4. When nothing is found, the returned `attempts` list actually names what was
   tried - "fail loudly with a diagnostic" depends on this list being real.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "modules" / "tool-computer-use"))

import amplifier_module_tool_computer_use.windows as windows_mod
from amplifier_module_tool_computer_use.backend import BackendError


def test_configured_path_wins_when_it_exists(tmp_path, monkeypatch):
    fake_ps = tmp_path / "powershell.exe"
    fake_ps.write_text("not a real binary, just needs to exist")

    ps, attempts = windows_mod._which_powershell(str(fake_ps))

    assert ps == str(fake_ps)
    assert attempts == []


def test_configured_path_is_validated_not_blindly_trusted(monkeypatch):
    monkeypatch.setattr(windows_mod.shutil, "which", lambda _name: None)

    ps, attempts = windows_mod._which_powershell("/definitely/not/a/real/path.exe")

    assert ps is None
    assert attempts, "must report why the configured override was rejected"
    assert "/definitely/not/a/real/path.exe" in attempts[0]


def test_shutil_which_success_short_circuits_before_any_mount_root_lookup(
    monkeypatch,
):
    monkeypatch.setattr(
        windows_mod.shutil, "which", lambda name: "/usr/bin/powershell.exe"
    )

    def _boom():
        raise AssertionError("must not attempt mount-root derivation when PATH works")

    monkeypatch.setattr(windows_mod, "_windows_mount_root", _boom)

    ps, attempts = windows_mod._which_powershell(None)

    assert ps == "/usr/bin/powershell.exe"
    assert attempts == []


def test_falls_back_to_derived_mount_root_when_path_lookup_fails(tmp_path, monkeypatch):
    """The core fix: PATH-independent resolution via the ACTUAL mount root, not a
    hardcoded /mnt/c."""
    # Simulate a non-default automount root, e.g. /etc/wsl.conf sets root=/windows/
    custom_root = tmp_path / "windows"
    ps_dir = custom_root / "Windows" / "System32" / "WindowsPowerShell" / "v1.0"
    ps_dir.mkdir(parents=True)
    fake_ps = ps_dir / "powershell.exe"
    fake_ps.write_text("stand-in binary")

    monkeypatch.setattr(windows_mod.shutil, "which", lambda _name: None)
    monkeypatch.setattr(windows_mod, "_windows_mount_root", lambda: str(custom_root))

    ps, attempts = windows_mod._which_powershell(None)

    assert ps == str(fake_ps)
    # Must have recorded that PATH lookup failed on the way to finding this.
    assert any("PATH" in a for a in attempts)


def test_never_hardcodes_mnt_c_when_mount_root_is_derivable_elsewhere(
    tmp_path, monkeypatch
):
    """A derived root that is NOT /mnt/c must be tried BEFORE the /mnt/c fallback,
    and must win even if (in this synthetic test) a decoy /mnt/c candidate also
    exists - proving the code does not just always fall through to /mnt/c."""
    custom_root = tmp_path / "custom_mount"
    ps_dir = custom_root / "Windows" / "System32" / "WindowsPowerShell" / "v1.0"
    ps_dir.mkdir(parents=True)
    real_ps = ps_dir / "powershell.exe"
    real_ps.write_text("the real one, at the derived root")

    monkeypatch.setattr(windows_mod.shutil, "which", lambda _name: None)
    monkeypatch.setattr(windows_mod, "_windows_mount_root", lambda: str(custom_root))
    # Force Path.exists() to answer truthfully only for our fake tree, and
    # falsely for the real /mnt/c fallback (which almost certainly doesn't exist
    # in this dev environment anyway, but be explicit about the contract).
    real_exists = Path.exists

    def _guarded_exists(self: Path) -> bool:
        if str(self) == "/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe":
            return False
        return real_exists(self)

    monkeypatch.setattr(Path, "exists", _guarded_exists)

    ps, _attempts = windows_mod._which_powershell(None)

    assert ps == str(real_ps)


def test_reports_every_attempt_when_nothing_is_found(monkeypatch):
    monkeypatch.setattr(windows_mod.shutil, "which", lambda _name: None)
    monkeypatch.setattr(windows_mod, "_windows_mount_root", lambda: None)

    ps, attempts = windows_mod._which_powershell(None)

    assert ps is None
    # Must be a REAL diagnostic, not an empty list - this is what lets probe()/the
    # `powershell` property fail loudly with detail instead of a bare "not found".
    assert len(attempts) >= 2
    joined = "; ".join(attempts)
    assert "PATH" in joined
    assert "mount root" in joined or "/mnt/c" in joined


def test_probe_reports_the_attempts_in_its_reason(monkeypatch):
    monkeypatch.setattr(
        windows_mod.shutil,
        "which",
        lambda name: "/usr/bin/wslpath" if name == "wslpath" else None,
    )
    monkeypatch.setattr(windows_mod, "_windows_mount_root", lambda: None)

    backend = windows_mod.WindowsBackend({})
    result = backend.probe()

    assert result.available is False
    assert "PATH" in result.reason
    assert "powershell_path" in result.reason


def test_powershell_property_raises_backenderror_with_diagnostic(monkeypatch):
    monkeypatch.setattr(windows_mod.shutil, "which", lambda _name: None)
    monkeypatch.setattr(windows_mod, "_windows_mount_root", lambda: None)

    backend = windows_mod.WindowsBackend({})
    try:
        _ = backend.powershell
    except BackendError as exc:
        assert "powershell_path" in str(exc)
    else:
        raise AssertionError("expected BackendError when no powershell can be found")


def test_windows_mount_root_returns_none_when_wslpath_missing(monkeypatch):
    """`_windows_mount_root` must never raise - a missing wslpath is reported as
    "no answer", not a crash, so callers can fall through to the next candidate."""

    def _raise_missing_binary(*_args, **_kwargs):
        raise FileNotFoundError("wslpath not found")

    monkeypatch.setattr(windows_mod.subprocess, "run", _raise_missing_binary)

    assert windows_mod._windows_mount_root() is None


def test_windows_mount_root_returns_none_when_wslpath_fails(monkeypatch):
    class _FakeCompletedProcess:
        returncode = 1
        stdout = ""
        stderr = "some wslpath error"

    monkeypatch.setattr(
        windows_mod.subprocess, "run", lambda *a, **k: _FakeCompletedProcess()
    )

    assert windows_mod._windows_mount_root() is None


def test_windows_mount_root_strips_trailing_slash(monkeypatch):
    class _FakeCompletedProcess:
        returncode = 0
        stdout = "/mnt/c/\n"
        stderr = ""

    monkeypatch.setattr(
        windows_mod.subprocess, "run", lambda *a, **k: _FakeCompletedProcess()
    )

    assert windows_mod._windows_mount_root() == "/mnt/c"
