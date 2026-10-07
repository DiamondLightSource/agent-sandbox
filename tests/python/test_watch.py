"""The PATH watcher (watch.py, ADR 27) against real directories.

A layout stands in for the container: ``rw`` is the jail-writable root (an
``allow-write`` path), ``rw/venv/bin`` the project venv first on PATH, ``sys``
a system command directory after it, and ``rw/work`` the workspace.
"""

import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
import zipfile
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from claude_sandbox import watch

SRC = Path(__file__).resolve().parents[2] / "src"


def executable(path: Path, text: str = "#!/bin/sh\n") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(0o755)
    return path


def mode(path: Path) -> int:
    return path.stat().st_mode & 0o777


def cleared(path: Path, why: str) -> str:
    return f"cleared the execute bits of {path} ({why})"


@dataclass
class Layout:
    root: Path
    venv: Path
    sys: Path
    work: Path
    state: Path

    @property
    def hooks(self) -> Path:
        return self.work / ".git/hooks"

    def session(self, path: str | None = None) -> watch.Session:
        return watch.Session(
            path or f"{self.venv}:{self.sys}",
            [str(self.root / "rw")],
            str(self.work),
            str(self.state),
        )

    def shadows(self, name: str) -> str:
        return cleared(self.venv / name, f"it shadowed {self.sys}/{name}")

    def alerts(self) -> list[str]:
        return watch.read_alerts([str(self.state)])


@pytest.fixture
def lay(tmp_path: Path) -> Iterator[Layout]:
    # On tmpfs where there is one, as /run and a container's overlay behave
    # for inotify.
    base = "/dev/shm" if os.path.isdir("/dev/shm") else str(tmp_path)
    root = Path(os.path.realpath(base)) / f"cs-watch-{os.getpid()}-{tmp_path.name}"
    venv, system, work = root / "rw/venv/bin", root / "sys", root / "rw/work"
    for d in (venv, system, work / ".git/hooks", root / "state"):
        d.mkdir(parents=True)
    for name in ("git", "python3", "ls"):
        executable(system / name)
    executable(venv / "python3")  # the venv's own: a shadow, but it was there
    executable(venv / "black")  # no system command of that name
    yield Layout(root, venv, system, work, root / "state")
    shutil.rmtree(root)


def test_targets_and_git_hooks(lay: Layout, tmp_path: Path) -> None:
    s = lay.session(f"rel::{lay.venv}:{lay.sys}:/usr/bin")
    assert s.targets() == [
        watch.Target(str(lay.venv), (str(lay.sys), "/usr/bin")),
        watch.Target(str(lay.hooks), hooks=True),
    ]
    # After the last system directory: not watched.
    assert lay.session(f"/usr/bin:{lay.venv}").targets()[:-1] == []
    # No workspace: no hooks.
    s = watch.Session(str(lay.venv), [str(lay.root / "rw")], "", None)
    assert s.targets() == [watch.Target(str(lay.venv))]
    # A worktree: .git names the git dir; hooks are in its common dir.
    wt = tmp_path / "wt"
    gitdir = lay.work / ".git/worktrees/wt"
    gitdir.mkdir(parents=True)
    (gitdir / "commondir").write_text("../..\n")
    wt.mkdir()
    (wt / ".git").write_text(f"gitdir: {gitdir}\n")
    assert watch.git_hooks_dir(str(wt)) == str(lay.hooks)
    (wt / ".git").write_text("not a gitdir line\n")
    assert watch.git_hooks_dir(str(wt)) is None
    if os.geteuid() != 0:
        (wt / ".git").chmod(0)
        assert watch.git_hooks_dir(str(wt)) is None
    (wt / ".git").unlink()
    (wt / ".git").mkdir()
    assert watch.git_hooks_dir(str(wt)) is None  # no hooks dir yet


def test_launch_scan_keeps_a_baseline_then_judges_against_it(lay: Layout) -> None:
    executable(lay.venv / "git")  # there at the first launch
    assert lay.session().scan_at_launch() == []  # the first: a baseline only
    assert lay.session().scan_at_launch() == []  # unchanged: left alone
    assert mode(lay.venv / "git") == 0o755
    executable(lay.venv / "ls")  # appeared between launches
    (lay.venv / "git").write_text("#!/bin/sh\necho changed\n")  # changed
    done = lay.session().scan_at_launch()
    assert done == [lay.shadows("git"), lay.shadows("ls")]
    assert mode(lay.venv / "git") == mode(lay.venv / "ls") == 0o644
    assert mode(lay.venv / "python3") == mode(lay.venv / "black") == 0o755
    assert [line.split(" ", 2)[2] for line in lay.alerts()] == done
    # Restored by the user, then the alerts cleared: the restored file stands.
    (lay.venv / "git").chmod(0o755)
    watch.clear_alerts([str(lay.state)])
    assert lay.alerts() == []
    assert lay.session().scan_at_launch() == []
    assert mode(lay.venv / "git") == 0o755


def test_launch_scan_without_state(lay: Layout) -> None:
    s = lay.session()
    s.state = None
    executable(lay.venv / "git")
    assert s.scan_at_launch() == []  # nothing stored, nothing to judge by
    assert os.listdir(lay.state) == []


def test_session_quarantines_only_what_changed(lay: Layout) -> None:
    s = lay.session()
    executable(lay.venv / "git")  # in the baseline, though it shadows
    s.start()
    assert s.tick() == []
    executable(lay.venv / "ls")  # new, and shadows
    executable(lay.venv / "pytest")  # new, shadows nothing
    (lay.venv / "python3").write_text("#!/bin/sh\nchanged\n")  # changed
    (lay.venv / "data").write_text("x")  # not executable
    assert s.tick() == [lay.shadows("ls"), lay.shadows("python3")]
    assert mode(lay.venv / "pytest") == mode(lay.venv / "git") == 0o755
    assert s.tick() == []
    (lay.venv / "ls").chmod(0o755)  # made executable again: again
    assert s.tick() == [lay.shadows("ls")]
    # A link is removed, never followed; its target is recorded.
    target = executable(lay.root / "rw/elsewhere")
    (lay.venv / "git").unlink()
    (lay.venv / "git").symlink_to(target)
    assert s.tick() == [
        f"removed the link {lay.venv}/git -> {target} (it shadowed {lay.sys}/git)"
    ]
    assert not (lay.venv / "git").exists() and mode(target) == 0o755
    # A git hook, new or changed; samples are left alone.
    executable(lay.hooks / "pre-push")
    executable(lay.hooks / "pre-push.sample")
    assert s.tick() == [cleared(lay.hooks / "pre-push", "a git hook")]
    assert len(lay.alerts()) == 5
    assert "Review the session that created them." in s.summary()
    assert watch.Session("", [], "", None).summary() == ""


def test_a_directory_that_appears_has_no_baseline(lay: Layout) -> None:
    later = lay.root / "rw/later/bin"
    s = lay.session(f"{later}:{lay.venv}:{lay.sys}")
    s.start()
    executable(later / "git")
    assert s.tick() == [cleared(later / "git", f"it shadowed {lay.sys}/git")]


def test_quarantine_edges(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    t = str(tmp_path)
    assert watch.quarantine(t, "gone") is None
    os.mkfifo(tmp_path / "fifo")
    assert watch.quarantine(t, "fifo") is None
    executable(tmp_path / "f").chmod(0o751)
    assert watch.quarantine(t, "f") == f"cleared the execute bits of {t}/f"
    assert mode(tmp_path / "f") == 0o640
    executable(tmp_path / "g")
    assert watch.quarantine(f"{t}/none", "g") is None  # no such directory

    def refuse(*args: object) -> None:
        raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr(os, "chmod", refuse)
    assert watch.quarantine(t, "g") is None


def test_quarantine_never_follows_a_swapped_directory(tmp_path: Path) -> None:
    """A component of the directory's path swapped for a link: no action
    lands where the link leads."""
    real, elsewhere = tmp_path / "real", tmp_path / "elsewhere"
    executable(elsewhere / "bin/git")
    (real / "bin").mkdir(parents=True)
    (tmp_path / "venv").symlink_to(elsewhere)  # was a directory at the look
    assert watch.quarantine(str(tmp_path / "venv/bin"), "git") is None
    assert mode(elsewhere / "bin/git") == 0o755
    assert watch.open_dir(str(tmp_path / "venv")) is None  # the link itself
    fd = watch.open_dir(str(real))
    assert fd is not None
    os.close(fd)


def test_open_dir_without_proc(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def no_proc(path: str) -> str:
        raise FileNotFoundError(2, "No such file or directory")

    monkeypatch.setattr(os, "readlink", no_proc)
    assert watch.open_dir(str(tmp_path)) is None


def test_signatures(tmp_path: Path) -> None:
    (tmp_path / "l").symlink_to(tmp_path / "nowhere")
    sig = watch.signature(str(tmp_path / "l"))
    assert sig is not None and sig[-1] == str(tmp_path / "nowhere")
    assert watch.snapshot(str(tmp_path / "none")) == {}
    assert watch.signature(str(tmp_path / "none")) is None


def test_a_name_gone_before_its_quarantine(
    lay: Layout, monkeypatch: pytest.MonkeyPatch
) -> None:
    s = lay.session()
    s.start()
    executable(lay.venv / "git")

    def gone(directory: str, name: str) -> None:
        return None

    monkeypatch.setattr(watch, "quarantine", gone)
    assert s.tick() == []


def test_state_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert watch.state_dir(str(tmp_path / "s")) == str(tmp_path / "s")
    fallback = tmp_path / "fallback"
    monkeypatch.setattr(watch, "FALLBACK_STATE_DIR", str(fallback))
    (tmp_path / "file").write_text("")
    assert watch.state_dir(str(tmp_path / "file/s")) == str(fallback)
    # Never through a link; and with no usable fallback, none.
    (tmp_path / "link").symlink_to(tmp_path / "s")
    fallback.rmdir()
    fallback.write_text("")
    assert watch.state_dir(str(tmp_path / "link")) is None


def test_records_and_baselines_survive_bad_state(tmp_path: Path) -> None:
    watch.record(None, ["x"])
    watch.record(str(tmp_path / "missing"), ["x"])  # cannot open: dropped
    watch.record(str(tmp_path), [])
    assert not (tmp_path / "alerts").exists()
    state = str(tmp_path)
    assert watch.load_baseline(state, "/d") is None
    watch.save_baseline(state, "/d", {"a": (1, "x")})
    assert watch.load_baseline(state, "/d") == {"a": (1, "x")}
    (path,) = (tmp_path / "baseline").iterdir()
    for bad in ("[]", '{"dir": "/other", "entries": {}}', '{"dir": "/d"}', "{"):
        path.write_text(bad)
        assert watch.load_baseline(state, "/d") is None
    path.write_text(json.dumps({"dir": "/d", "entries": {"a": [1], "b": 2}}))
    assert watch.load_baseline(state, "/d") == {"a": (1,)}
    # A baseline that cannot be written is not saved, and leaves nothing.
    (tmp_path / "baseline").rename(tmp_path / "moved")
    (tmp_path / "baseline").write_text("")  # in the way of the directory
    watch.save_baseline(state, "/d", {})
    assert sorted(os.listdir(tmp_path)) == ["baseline", "moved"]
    # Clearing skips what it cannot read or use.
    other = tmp_path / "other"
    (other / "baseline").mkdir(parents=True)
    (other / "baseline/junk.json").write_text("{")
    (other / "baseline/num.json").write_text('{"dir": 1}')
    (other / "alerts").mkdir()  # cannot be emptied
    watch.clear_alerts([str(other), str(tmp_path / "none")])
    assert watch.read_alerts([str(other)]) == []


def wait_until(check: Callable[[], bool], seconds: float = 3.0) -> float:
    start = time.monotonic()
    while not check():
        assert time.monotonic() - start < seconds, "timed out"
        time.sleep(0.01)
    return time.monotonic() - start


class CountedInotify(watch.Inotify):
    """Records whether each instance got an inotify descriptor."""

    made: list[bool] = []

    def __init__(self) -> None:
        super().__init__()
        CountedInotify.made.append(self.available)


class NoInotify(watch.Inotify):
    """A host without inotify: the watcher polls."""

    def __init__(self) -> None:
        self.fd = -1
        self._add = None


@pytest.mark.parametrize("inotify", [True, False], ids=["inotify", "polling"])
def test_watching_in_a_thread(
    lay: Layout, monkeypatch: pytest.MonkeyPatch, inotify: bool
) -> None:
    if inotify:
        # A tick far longer than the test: only inotify can wake it in time.
        monkeypatch.setattr(watch, "TICK", 60.0)
        monkeypatch.setattr(watch, "Inotify", CountedInotify)
        CountedInotify.made.clear()
    else:
        monkeypatch.setattr(watch, "Inotify", NoInotify)
        monkeypatch.setattr(watch, "TICK", 0.2)
    reports: list[str] = []
    with watch.watching(lay.session(), reports.append):
        time.sleep(0.05)  # the thread has taken its first look
        if inotify and not all(CountedInotify.made):
            # This user's inotify instances (max_user_instances) ran out, so
            # the watcher polls, and this test cannot wait a minute's tick.
            pytest.skip("no inotify instance free on this host")
        executable(lay.venv / "git")
        took = wait_until(lambda: mode(lay.venv / "git") == 0o644)
        executable(lay.hooks / "pre-commit")
        wait_until(lambda: mode(lay.hooks / "pre-commit") == 0o644)
    assert took < 1.0
    assert len(reports) == 1 and f"{lay.venv}/git" in reports[0]
    assert len(lay.alerts()) == 2
    with watch.watching(lay.session(), reports.append):
        pass
    assert len(reports) == 1  # nothing done, nothing reported


def test_inotify_without_ctypes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "ctypes", None)  # the import fails
    notify = watch.Inotify()
    assert not notify.available
    notify.watch("/")
    notify.close()


def test_launcher_ended(monkeypatch: pytest.MonkeyPatch) -> None:
    # With a pidfd: readable once the process has exited.
    r, w = os.pipe()

    def pidfd_open(pid: int) -> int:
        return r

    monkeypatch.setattr(os, "pidfd_open", pidfd_open, raising=False)
    ended = watch.launcher_ended(4242)
    assert not ended()
    os.write(w, b"x")
    assert ended()
    os.close(r)
    os.close(w)
    # Without one (some interpreters lack pidfd_open): a signal-0 probe.
    monkeypatch.delattr(os, "pidfd_open")
    child = subprocess.Popen(["sleep", "30"])
    ended = watch.launcher_ended(child.pid)
    assert not ended()
    child.kill()
    child.wait()
    assert ended()
    assert not watch.launcher_ended(1)()  # alive, even if not ours to signal


def test_fork_watcher_parent_side(lay: Layout, monkeypatch: pytest.MonkeyPatch) -> None:
    waited: list[int] = []

    def waitpid(pid: int, options: int) -> tuple[int, int]:
        waited.append(pid)
        return pid, 0

    monkeypatch.setattr(os, "fork", lambda: 4242)
    monkeypatch.setattr(os, "waitpid", waitpid)
    s = lay.session()
    watch.fork_watcher(s)
    assert s.baseline  # taken before the fork, so the watcher judges by it
    assert waited[-1] == 4242  # the intermediate child, reaped at once


CHILD = """
import os, sys, time
sys.path.insert(0, {src!r})
from claude_sandbox import watch
s = watch.Session({path!r}, [{root!r}], {work!r}, {state!r})
watch.fork_watcher(s)
with open({git!r}, "w") as f:
    f.write("#!/bin/sh\\n")
os.chmod({git!r}, 0o755)
time.sleep(0.5)
"""


def test_fork_watcher_watches_until_its_parent_has_gone(lay: Layout) -> None:
    """The jail-off watcher: a child that quarantines while its parent runs
    and stops within a tick of the parent's exit."""
    git = lay.venv / "git"
    code = CHILD.format(
        src=str(SRC),
        path=f"{lay.venv}:{lay.sys}",
        root=str(lay.root / "rw"),
        work=str(lay.work),
        state=str(lay.state),
        git=str(git),
    )
    proc = subprocess.run(
        [sys.executable, "-I", "-c", code], capture_output=True, timeout=30
    )
    assert proc.returncode == 0, proc.stderr
    assert mode(git) == 0o644  # before the parent exited
    assert len(lay.alerts()) == 1

    def child_running() -> bool:
        found = subprocess.run(["pgrep", "-f", str(lay.state)], capture_output=True)
        return found.returncode == 0

    wait_until(lambda: not child_running(), seconds=5)


HOOK = (
    Path(__file__).resolve().parents[2]
    / ".devcontainer/claude-sandbox/alerts-prompt.sh"
)


@pytest.mark.parametrize("shell", ["bash", "zsh", "sh"])
def test_prompt_hook(tmp_path: Path, shell: str) -> None:
    """Each outer prompt shows the alerts that shell has not shown yet."""
    path = shutil.which(shell)
    if path is None:
        pytest.skip(f"no {shell}")
    run, alerts = tmp_path / "run", tmp_path / "run/alerts"
    run.mkdir()
    hook = tmp_path / "hook.sh"
    hook.write_text(
        HOOK.read_text()
        .replace("/run/claude-sandbox", str(run))
        .replace("/tmp/claude-sandbox", str(tmp_path / "tmp"))
    )
    steps = [
        f". {hook}",
        f". {hook}",  # sourced twice (profile.d, then the rc): hooked once
        'echo "hooked: $PROMPT_COMMAND${precmd_functions:-}"',
        "__cs_alerts; echo 1>&2 '-- empty'",
        f"printf 'one\\n' > {alerts}; __cs_alerts; __cs_alerts; echo 1>&2 '-- one'",
        f"printf 'two' >> {alerts}; __cs_alerts; echo 1>&2 '-- two'",
        f": > {alerts}; printf 'three\\n' > {alerts}; __cs_alerts; echo 1>&2 '-- 3'",
    ]
    if shell == "sh":
        proc = subprocess.run(
            [path, "-c", f". {hook}; command -v __cs_alerts || echo none"],
            capture_output=True,
            text=True,
        )
        assert proc.stdout == "none\n"  # parsed, and nothing done
        return
    env = {"HOME": str(tmp_path), "ZDOTDIR": str(tmp_path), "PATH": "/usr/bin:/bin"}
    proc = subprocess.run(
        [path, "-i", "-c", "; ".join(steps)],
        capture_output=True,
        text=True,
        env=env,
        stdin=subprocess.DEVNULL,
    )
    assert proc.stdout.count("__cs_alerts") == 1, proc.stdout
    shown = [
        line.split("m", 1)[-1] if "\033" in line else line
        for line in proc.stderr.splitlines()
        if line.startswith(("  ", "--", "\033[1;31mclaude"))
    ]
    assert shown == [
        "-- empty",
        "claude-sandbox: quarantined what a sandboxed session left:\033[0m",
        "  one",
        "-- one",
        "claude-sandbox: quarantined what a sandboxed session left:\033[0m",
        "  two",
        "-- two",
        "claude-sandbox: quarantined what a sandboxed session left:\033[0m",
        "  three",
        "-- 3",
    ], proc.stderr


EVIL = "git\n2026-01-01 00:00:00 all clear\x1b[2J\x9b‮\\x"


def test_jail_chosen_names_are_escaped(lay: Layout, tmp_path: Path) -> None:
    """A name cannot forge alert lines or reach the terminal raw."""
    assert watch.describe(EVIL) == (
        "git\\n2026-01-01 00:00:00 all clear\\x1b[2J\\x9b\\u202e\\\\x"
    )
    assert watch.describe("/a/é b") == "/a/é b"
    assert watch.describe(os.fsdecode(b"/a/\xff")) == "/a/\\udcff"
    executable(lay.sys / EVIL)  # a later directory has one too, so it shadows
    s = lay.session()
    s.start()
    executable(lay.venv / EVIL)
    target = lay.root / "rw/x\x1bt"
    executable(target)
    (lay.venv / "ls").symlink_to(target)
    done = s.tick()
    assert len(done) == 2 and all(c.isprintable() for line in done for c in line)
    alerts = lay.alerts()
    assert len(alerts) == 2  # one line each: nothing forged
    assert all("\x1b" not in line and "\x9b" not in line for line in alerts)
    assert "\\x1b" in s.summary() and "\x1b[2J" not in s.summary()


def test_a_venv_interpreter_link_to_a_system_python_stays(lay: Layout) -> None:
    """``uv venv`` in a session links python3 to an interpreter the session
    cannot write: left alone. Anything else of that name is judged."""
    system_python = executable(lay.root / "sys/python3.14")  # outside rw/
    executable(lay.sys / "python")
    executable(lay.sys / "python3.14")
    s = lay.session()
    (lay.venv / "python3").unlink()  # recreated during the session
    s.start()
    (lay.venv / "python").symlink_to(system_python)
    (lay.venv / "python3").symlink_to("python")  # a chain, as uv makes it
    (lay.venv / "python3.14").symlink_to("python")
    assert s.tick() == []
    # Into the writable tree: quarantined.
    script = executable(lay.root / "rw/python-evil")
    (lay.venv / "python3").unlink()
    (lay.venv / "python3").symlink_to(script)
    assert s.tick() == [
        f"removed the link {lay.venv}/python3 -> {script}"
        f" (it shadowed {lay.sys}/python3)"
    ]
    # A regular file: quarantined.
    (lay.venv / "python3.14").unlink()
    executable(lay.venv / "python3.14")
    assert s.tick() == [lay.shadows("python3.14")]
    # A name that is not an interpreter's, to a system python: quarantined.
    (lay.venv / "ls").symlink_to(system_python)
    assert s.tick() == [
        f"removed the link {lay.venv}/ls -> {system_python} (it shadowed {lay.sys}/ls)"
    ]
    # The launch scan applies the same rule.
    assert not (lay.venv / "python3").exists()  # the quarantined link went
    (lay.venv / "python3").symlink_to(system_python)
    assert lay.session().scan_at_launch() == []  # the first: a baseline
    (lay.venv / "python3").unlink()
    (lay.venv / "python3").symlink_to("python")
    assert lay.session().scan_at_launch() == []


def test_system_interpreter_needs_a_python_target(tmp_path: Path) -> None:
    other = executable(tmp_path / "sys/bash")
    (tmp_path / "python3").symlink_to(other)
    assert not watch.system_interpreter(str(tmp_path / "python3"), [])
    (tmp_path / "python3").unlink()
    (tmp_path / "python3").symlink_to(tmp_path / "sys/python-missing")
    assert not watch.system_interpreter(str(tmp_path / "python3"), [])


def git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def test_core_hooks_path(lay: Layout, monkeypatch: pytest.MonkeyPatch) -> None:
    """A hooks directory named by core.hooksPath is watched like .git/hooks,
    and a change of the setting is an alert of its own."""
    monkeypatch.setenv("HOME", str(lay.root))  # no user config
    shutil.rmtree(lay.work / ".git")
    git("init", "-q", cwd=lay.work)
    assert watch.git_hooks_path(str(lay.work)) is None

    def nowhere(name: str) -> None:
        return None

    with monkeypatch.context() as m:
        m.setattr(watch, "find_tool", nowhere)  # no git at all
        assert watch.git_hooks_path(str(lay.work)) is None
    assert watch.git_hooks_path("") is None
    s = lay.session()
    s.start()
    (lay.work / "hooks").mkdir()
    git("config", "core.hooksPath", "hooks", cwd=lay.work)
    assert s.tick() == [f"core.hooksPath of {lay.work} changed from (unset) to hooks"]
    assert watch.Target(str(lay.work / "hooks"), hooks=True) in s.targets()
    executable(lay.work / "hooks/pre-push")
    assert s.tick() == [cleared(lay.work / "hooks/pre-push", "a git hook")]
    # Outside the writable roots: not watched (nothing the session wrote).
    git("config", "core.hooksPath", str(lay.sys), cwd=lay.work)
    assert s.tick() == [f"core.hooksPath of {lay.work} changed from hooks to {lay.sys}"]
    assert all(t.directory != str(lay.sys) for t in s.targets())
    git("config", "--unset", "core.hooksPath", cwd=lay.work)
    assert s.tick()[0].endswith(f"from {lay.sys} to (unset)")
    assert len(lay.alerts()) == 4


def test_git_hooks_path_survives_git_failing(
    lay: Layout, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(*args: object, **kwargs: object) -> None:
        raise subprocess.TimeoutExpired("git", 5)

    monkeypatch.setattr(subprocess, "run", broken)
    assert watch.git_hooks_path(str(lay.work)) is None


# --- uv from PyPI: a venv's genuine uv is given back ------------------------------

UV = b"\x7fELF the genuine uv"
UVX = b"\x7fELF the genuine uvx"
VERSION = "0.9.7"
WHEEL_URL = f"{watch.PYPI_FILES}packages/uv-{VERSION}-manylinux.whl"
JSON_URL = watch.PYPI_JSON.format(VERSION)


def wheel(uv: bytes = UV, uvx: bytes = UVX) -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        z.writestr(f"uv-{VERSION}.data/scripts/uv", uv)
        z.writestr(f"uv-{VERSION}.data/scripts/uvx", uvx)
        z.writestr(f"uv-{VERSION}.dist-info/RECORD", "")
    return out.getvalue()


def pypi_json(files: list[tuple[str, str, str]]) -> bytes:
    """PyPI's JSON for ``files``: (filename, url, sha256)."""
    urls = [
        {"filename": f, "url": u, "digests": {"sha256": d}, "packagetype": "x"}
        for f, u, d in files
    ]
    return json.dumps({"info": {"version": VERSION}, "urls": urls}).encode()


class PyPI:
    """A stand-in for ``watch.fetch``: serves ``pages``, counts requests,
    and raises OSError (as urllib does) for anything else."""

    def __init__(self, whl: bytes | None = None) -> None:
        whl = wheel() if whl is None else whl
        sha = hashlib.sha256(whl).hexdigest()
        plat = "manylinux_2_17_x86_64.manylinux2014_x86_64"
        files = [
            (f"uv-{VERSION}-py3-none-{plat}.whl", WHEEL_URL, sha),
            (f"uv-{VERSION}-py3-none-musllinux_1_1_x86_64.whl", WHEEL_URL + "m", sha),
            (f"uv-{VERSION}-py3-none-manylinux_2_28_aarch64.whl", WHEEL_URL + "a", sha),
            (f"uv-{VERSION}-py3-none-{plat}.whl", "http://evil/x.whl", sha),
            (f"uv-{VERSION}-py3-none-{plat}.whl", WHEEL_URL + "s", "not-a-sha"),
            (f"uv-{VERSION}.tar.gz", WHEEL_URL + "t", sha),
        ]
        self.pages = {JSON_URL: pypi_json(files), WHEEL_URL: whl}
        self.got: list[str] = []

    def __call__(self, url: str, out: Any) -> None:
        self.got.append(url)
        if url not in self.pages:
            raise OSError(f"no route to {url}")
        out.write(self.pages[url])


@pytest.fixture
def pypi(lay: Layout, monkeypatch: pytest.MonkeyPatch) -> PyPI:
    """A venv that says it has uv VERSION, a system uv and uvx it shadows,
    a glibc x86_64 machine, and PyPI mocked."""
    executable(lay.sys / "uv")
    executable(lay.sys / "uvx")
    site = lay.venv.parent / "lib/python3.12/site-packages"
    (site / f"uv-{VERSION}.dist-info").mkdir(parents=True)
    (site / "uv-junk.dist-info").mkdir()
    monkeypatch.setattr(watch.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(watch.platform, "libc_ver", lambda: ("glibc", "2.39"))
    fake = PyPI()
    monkeypatch.setattr(watch, "fetch", fake)
    return fake


def install(lay: Layout, uv: bytes = UV, uvx: bytes = UVX) -> None:
    for name, data in (("uv", uv), ("uvx", uvx)):
        path = lay.venv / name
        path.unlink(missing_ok=True)
        path.write_bytes(data)
        path.chmod(0o755)


def verified(lay: Layout) -> list[str]:
    try:
        lines = (lay.state / watch.VERIFIED).read_text().splitlines()
    except FileNotFoundError:
        return []
    return [line.split(" ", 2)[2] for line in lines]


def test_a_genuine_uv_is_restored_without_an_alert(lay: Layout, pypi: PyPI) -> None:
    assert lay.session().scan_at_launch() == []  # a stored baseline
    s = lay.session()
    s.start()
    install(lay)  # what `uv sync` does with tox-uv in the project
    assert s.tick() == []  # held, not alerted
    s.settle()
    assert mode(lay.venv / "uv") == mode(lay.venv / "uvx") == 0o755
    assert lay.alerts() == [] and s.actions == [] and s.summary() == ""
    assert verified(lay) == [
        f"verified {lay.venv}/{name} as uv {VERSION} from PyPI; execute bits restored"
        for name in ("uv", "uvx")
    ]
    # The baseline takes it: not judged again, in this session or the next.
    assert s.tick() == [] and s.held == []
    assert mode(lay.venv / "uv") == 0o755
    assert lay.session().scan_at_launch() == []
    assert mode(lay.venv / "uv") == 0o755
    # The wheel came down once; the second file used the cache.
    assert pypi.got.count(WHEEL_URL) == 1
    assert (lay.state / watch.PYPI_CACHE).is_dir()


def test_a_genuine_uv_found_at_launch(lay: Layout, pypi: PyPI) -> None:
    """Rebuilt between sessions: held at the launch scan (no warning), and
    checked once the watcher runs."""
    assert lay.session().scan_at_launch() == []
    install(lay)
    s = lay.session()
    assert s.scan_at_launch() == []
    assert mode(lay.venv / "uv") == 0o644 and len(s.held) == 2
    assert s.verifier is None  # nothing started on the launch path
    s.start()
    s.tick()
    s.settle()
    assert mode(lay.venv / "uv") == mode(lay.venv / "uvx") == 0o755
    assert lay.alerts() == []
    assert lay.session().scan_at_launch() == []


@pytest.mark.parametrize(
    "case", ["planted", "network", "no-version", "bad-wheel", "wheel-sha"]
)
def test_anything_but_a_genuine_uv_stays_quarantined(
    lay: Layout, pypi: PyPI, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    uv = UV
    if case == "planted":
        uv = b"#!/bin/sh\necho pwned\n"
    elif case == "network":
        pypi.pages.clear()
    elif case == "no-version":
        shutil.rmtree(lay.venv.parent / "lib")
    elif case == "bad-wheel":
        pypi.pages[WHEEL_URL] = b"not a zip"
        pypi.pages[JSON_URL] = pypi.pages[JSON_URL].replace(
            json.loads(pypi.pages[JSON_URL])["urls"][0]["digests"]["sha256"].encode(),
            hashlib.sha256(b"not a zip").hexdigest().encode(),
        )
    else:
        pypi.pages[WHEEL_URL] = wheel(uv=b"tampered")  # not PyPI's digest
    s = lay.session()
    s.start()
    install(lay, uv=uv)
    assert s.tick() == []
    s.settle()
    assert mode(lay.venv / "uv") == 0o644
    expected = (
        [lay.shadows("uv")]
        if case == "planted"
        else [
            lay.shadows("uv"),
            lay.shadows("uvx"),
        ]
    )
    assert sorted(line.split(" ", 2)[2] for line in lay.alerts()) == expected
    assert sorted(s.actions) == expected
    assert "quarantined during this session" in s.summary()
    assert verified(lay) == (
        [f"verified {lay.venv}/uvx as uv {VERSION} from PyPI; execute bits restored"]
        if case == "planted"
        else []
    )


def test_only_uv_and_uvx_are_checked(lay: Layout, pypi: PyPI) -> None:
    """Another name, a link named uv, or uv as a git hook: alerted at once,
    nothing fetched."""
    s = lay.session()
    s.start()
    executable(lay.venv / "git", UV.decode())
    (lay.venv / "uv").symlink_to(lay.sys / "uv")
    executable(lay.hooks / "uv")
    done = s.tick()
    assert done == [
        lay.shadows("git"),
        f"removed the link {lay.venv}/uv -> {lay.sys}/uv (it shadowed {lay.sys}/uv)",
        cleared(lay.hooks / "uv", "a git hook"),
    ]
    assert s.held == [] and s.verifier is None and pypi.got == []
    # Without a state directory there is nowhere to cache: alerted at once.
    s = lay.session()
    s.state = None
    s.start()
    install(lay)
    assert s.tick() == [lay.shadows("uv"), lay.shadows("uvx")]
    assert pypi.got == []


def test_a_uv_changed_while_held_is_judged_afresh(
    lay: Layout, pypi: PyPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The check in flight is dropped for the newer one; and one still in
    flight when the session ends alerts."""
    release = threading.Event()
    real = pypi.__call__

    def slow(url: str, out: Any) -> None:
        release.wait(5)
        real(url, out)

    monkeypatch.setattr(watch, "fetch", slow)
    s = lay.session()
    s.start()
    install(lay, uv=b"first")
    s.tick()
    first = s.held[0]
    install(lay)  # the genuine one, rewritten while the first is checked
    s.tick()
    assert first not in s.held and len(s.held) == 2
    s.settle(0.05)  # the session ends before the checks finish: alerts
    assert sorted(s.actions) == [lay.shadows("uv"), lay.shadows("uvx")]
    release.set()
    assert s.verifier is not None
    s.verifier.join(5)
    assert mode(lay.venv / "uv") == 0o644 and verified(lay) == []
    assert len(lay.alerts()) == 2


def test_restore_edges(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    digest = hashlib.sha256(UV).hexdigest()
    (tmp_path / "uv").write_bytes(UV)
    (tmp_path / "uv").chmod(0o640)
    assert watch.restore(str(tmp_path / "gone"), "uv", digest) is None
    assert watch.restore(str(tmp_path), "nothing", digest) is None
    (tmp_path / "dir").mkdir()
    assert watch.restore(str(tmp_path), "dir", digest) is None
    (tmp_path / "link").symlink_to(tmp_path / "uv")
    assert watch.restore(str(tmp_path), "link", digest) is None  # not followed
    assert watch.restore(str(tmp_path), "uv", "0" * 64) is None
    assert mode(tmp_path / "uv") == 0o640
    # Changed while it was read: left alone.
    real_fstat = os.fstat
    calls: list[int] = []

    def moving(fd: int) -> os.stat_result:
        calls.append(fd)
        st = real_fstat(fd)
        if len(calls) == 2:
            return os.stat_result((*st[:6], st.st_size + 1, *st[7:]))
        return st

    with monkeypatch.context() as m:
        m.setattr(os, "fstat", moving)
        assert watch.restore(str(tmp_path), "uv", digest) is None
    with monkeypatch.context() as m:

        def refuse(*_: object) -> None:
            raise PermissionError

        m.setattr(os, "chmod", refuse)
        assert watch.restore(str(tmp_path), "uv", digest) is None
    sig = watch.restore(str(tmp_path), "uv", digest)
    assert mode(tmp_path / "uv") == 0o750  # an x for each r
    assert sig == watch.signature(str(tmp_path / "uv"))
    assert watch.restore(str(tmp_path), "uv", digest) is None  # not quarantined


def test_pypi_helpers(
    lay: Layout, pypi: PyPI, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    assert watch.uv_versions(str(tmp_path)) == []
    (tmp_path / "lib/notpython").mkdir(parents=True)
    assert watch.uv_versions(str(tmp_path / "bin")) == []
    assert watch.uv_versions(str(lay.venv)) == [VERSION]
    # Only this machine's wheels: the glibc x86_64 one from files.pythonhosted.
    assert [u for u, _ in watch._wheels(VERSION)] == [WHEEL_URL]  # pyright: ignore[reportPrivateUsage]
    monkeypatch.setattr(watch.platform, "libc_ver", lambda: ("", ""))
    assert [u for u, _ in watch._wheels(VERSION)] == [WHEEL_URL + "m"]  # pyright: ignore[reportPrivateUsage]
    monkeypatch.setattr(watch.platform, "machine", lambda: "aarch64")
    assert watch._wheels(VERSION) == []  # pyright: ignore[reportPrivateUsage]
    # A corrupt cache entry is fetched again.
    monkeypatch.setattr(watch.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(watch.platform, "libc_ver", lambda: ("glibc", "2.39"))
    [first] = watch.pypi_digests(str(lay.state), VERSION)
    [entry] = (lay.state / watch.PYPI_CACHE).iterdir()
    entry.write_text('{"uv": 1}')
    assert list(watch.pypi_digests(str(lay.state), VERSION)) == [first]
    assert pypi.got.count(WHEEL_URL) == 2


def test_fetch(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValueError):
        watch.fetch("http://pypi.org/x", io.BytesIO())

    def urlopen(url: str, timeout: float) -> io.BytesIO:
        assert timeout == watch.FETCH_TIMEOUT
        return io.BytesIO(url.encode())

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    out = io.BytesIO()
    watch.fetch("https://pypi.org/x", out)
    assert out.getvalue() == b"https://pypi.org/x"
