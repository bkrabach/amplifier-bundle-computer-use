"""Unit tests for `remote_agent.sweep_stale_agent_dirs` - the defense-in-depth
half of the scratch-dir leak fix (the other half, `atexit` registration in
`ssh_transport._bootstrap_stub`, is proven in
`test_agent_scratch_dir_cleanup.py`).

`atexit` can only run code in a process that is still alive to run it - a
SIGKILL, an OOM-kill, or the host disappearing never gets there, so some
scratch dirs are always going to survive whatever cleanup the dying process
itself could register. This sweep is what keeps THAT residual leak bounded
instead of unbounded: it runs once per new connection and removes anything
old enough to be almost certainly orphaned.

No subprocess, no real target - plain directories under `tmp_path`, with
`os.utime` used to backdate the ones that should look stale.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "modules" / "tool-computer-use"))

from amplifier_module_tool_computer_use.remote_agent import sweep_stale_agent_dirs


def _make_dir(base: Path, name: str, *, age_seconds: float) -> Path:
    d = base / name
    d.mkdir()
    (d / "marker.txt").write_text("x", encoding="utf-8")
    stamp = time.time() - age_seconds
    os.utime(d, (stamp, stamp))
    return d


def test_sweep_removes_only_dirs_older_than_the_threshold(tmp_path: Path) -> None:
    stale = _make_dir(tmp_path, "amplifier-cu-agent-stale", age_seconds=48 * 3600)
    fresh = _make_dir(tmp_path, "amplifier-cu-agent-fresh", age_seconds=5)

    removed = sweep_stale_agent_dirs(temp_dir=str(tmp_path), max_age_seconds=3600)

    assert removed == 1
    assert not stale.exists(), "stale dir must be removed"
    assert fresh.exists(), "fresh (possibly live, concurrent-session) dir must survive"


def test_sweep_never_touches_a_live_concurrent_sessions_directory(
    tmp_path: Path,
) -> None:
    """The core safety property: a second, independent session to the same
    target must never lose its scratch dir just because this one connected.
    Simulated here as a dir well under the age threshold."""
    live_sibling = _make_dir(tmp_path, "amplifier-cu-agent-sibling", age_seconds=60)

    sweep_stale_agent_dirs(temp_dir=str(tmp_path), max_age_seconds=3600)

    assert live_sibling.exists()


def test_sweep_ignores_unrelated_files_and_directories(tmp_path: Path) -> None:
    unrelated = tmp_path / "some-other-tempfile"
    unrelated.write_text("not ours", encoding="utf-8")
    stamp = time.time() - 999999
    os.utime(unrelated, (stamp, stamp))

    removed = sweep_stale_agent_dirs(temp_dir=str(tmp_path), max_age_seconds=1)

    assert removed == 0
    assert unrelated.exists()


def test_sweep_returns_zero_and_never_raises_when_temp_dir_is_missing() -> None:
    """Cleanup failing must never break a working session - a nonexistent
    or unreadable temp dir is exactly the kind of environment hiccup this
    is best-effort against."""
    removed = sweep_stale_agent_dirs(
        temp_dir="/this/path/does/not/exist/anywhere", max_age_seconds=1
    )
    assert removed == 0


def test_sweep_survives_a_directory_it_cannot_remove(
    tmp_path: Path, monkeypatch
) -> None:
    """A single unremovable directory (permissions, a race with something
    else deleting it) must not abort the sweep or raise - `shutil.rmtree`
    is called with `ignore_errors=True` for exactly this reason, but this
    proves the surrounding loop is equally tolerant of a `stat()` failure."""
    stale = _make_dir(tmp_path, "amplifier-cu-agent-a", age_seconds=48 * 3600)
    also_stale = _make_dir(tmp_path, "amplifier-cu-agent-b", age_seconds=48 * 3600)

    real_stat = Path.stat

    def _flaky_stat(self, *args, **kwargs):
        if self == stale:
            raise OSError("simulated stat failure")
        return real_stat(self, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", _flaky_stat)

    removed = sweep_stale_agent_dirs(temp_dir=str(tmp_path), max_age_seconds=3600)

    assert removed == 1
    assert not also_stale.exists()
