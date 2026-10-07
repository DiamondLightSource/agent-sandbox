"""The PATH watcher (watch.py, ADR 27) against real directories.

A layout stands in for the container: ``rw`` is the jail-writable root (an
``allow-write`` path), ``rw/venv/bin`` the project venv first on PATH, ``sys``
a system command directory after it, and ``rw/work`` the workspace.
"""

import json
import os
import shutil
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

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
