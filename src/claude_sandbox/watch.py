"""The PATH watcher (ADR 27): what a session leaves behind for outside it.

Quarantine executables a session adds ahead of system commands on PATH, and
new git hooks, while the session runs; warn in outer shells.

The jail contains the agent while it runs. It cannot contain code the agent
writes that something outside the jail runs later. In the published image
and in copier devcontainers PATH starts with a project venv's ``bin`` under
an ``allow-write`` path, so an executable a session leaves there shadows a
system command (``git``, say) for every outer shell, VS Code and the
entrypoint, during the session or after it. A git hook never shows in a diff
and runs on the next outer ``git commit`` or ``git push``.

So, from outside the jail and for as long as the session lasts:

- The watched directories are the writable ones (inside a read-write bind,
  per ``bwrap.py``) that a PATH lookup searches before the last system
  command directory, and the workspace's git hooks directory.
- In a PATH directory, an executable (or a link to one) whose name a later
  PATH directory also has is a shadow. Its execute bits are cleared on the
  file itself, never through a link; a link is removed and its target
  recorded. In the hooks directory, any executable but ``*.sample`` is
  quarantined the same way.
- Only what changed is judged: a name present and unchanged since the
  baseline is left alone, so the venv's own ``python3`` stays. The baseline
  is the state when the session started; for the launch-time scan it is the
  state the previous launch recorded under ``STATE_DIR``.
- Each action is a line in ``STATE_DIR/alerts``, which the jail cannot see
  (``bwrap.py`` masks it) and which the prompt hook prints in outer shells.

inotify (through ``ctypes``) wakes the watcher at once; a pass also runs
every second, which finds directories that appear later and is the whole
mechanism when inotify is unavailable. Standard library only: this module is
on the launch path (ADR 26).
"""

import hashlib
import json
import os
import select
import signal
import stat
import sys
import threading
import time
from collections.abc import Callable, Generator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import NoReturn, cast

from .bwrap import HOST, STATE_DIR, Probe, inside, path_ahead, watched_path_dirs

# Where the alerts go when /run cannot be written (bwrap masks /tmp).
FALLBACK_STATE_DIR = "/tmp/claude-sandbox"
ALERTS = "alerts"
BASELINES = "baseline"
TICK = 1.0  # seconds between passes

# What lstat says about a name, and for a link what it leads to: changes when
# the name is replaced, written, chmodded or relinked (ctime cannot be set).
Sig = tuple[int | str, ...]


@dataclass(frozen=True)
class Target:
    """A watched directory. ``later``: the PATH directories searched after
    it, which a shadow's name must also be in; empty for git hooks."""

    directory: str
    later: tuple[str, ...] = ()
    hooks: bool = False


def git_hooks_dir(workspace: str) -> str | None:
    """The workspace's git hooks directory, resolved, if it exists.

    ``.git`` is the git directory, or in a worktree a file naming it; a
    worktree's hooks live in the common directory its ``commondir`` names.
    ``core.hooksPath`` is not followed.
    """
    dot = os.path.join(workspace, ".git")
    gitdir = dot
    if os.path.isfile(dot):
        try:
            with open(dot, encoding="utf-8", errors="surrogateescape") as f:
                line = f.readline().strip()
        except OSError:
            return None
        if not line.startswith("gitdir:"):
            return None
        gitdir = os.path.join(workspace, line.removeprefix("gitdir:").strip())
    try:
        with open(os.path.join(gitdir, "commondir"), encoding="utf-8") as f:
            gitdir = os.path.join(gitdir, f.read().strip())
    except OSError:
        pass
    hooks = os.path.realpath(os.path.join(gitdir, "hooks"))
    return hooks if os.path.isdir(hooks) else None


def targets(
    path: str, roots: Sequence[str], workspace: str, probe: Probe = HOST
) -> list[Target]:
    """Every directory to watch now: they come and go during a session."""
    order = path_ahead(path, (), probe)  # every PATH directory, in order
    found = [
        Target(d, tuple(order[order.index(d) + 1 :]))
        for d in watched_path_dirs(path, roots, probe)
    ]
    hooks = git_hooks_dir(workspace) if workspace else None
    if hooks is not None and inside(hooks, roots):
        found.append(Target(hooks, hooks=True))
    return found


def signature(path: str) -> Sig | None:
    try:
        st = os.lstat(path)
    except OSError:
        return None
    sig: Sig = (st.st_ino, st.st_ctime_ns, st.st_mode, st.st_size)
    if stat.S_ISLNK(st.st_mode):
        try:
            sig += (os.readlink(path),)
            to = os.stat(path)
            sig += (to.st_ino, to.st_ctime_ns, to.st_mode)
        except OSError:
            pass
    return sig


def snapshot(directory: str) -> dict[str, Sig]:
    try:
        names = os.listdir(directory)
    except OSError:
        return {}
    sigs = {name: signature(os.path.join(directory, name)) for name in names}
    return {name: sig for name, sig in sigs.items() if sig is not None}


def runnable(path: str) -> bool:
    """A regular file with an execute bit, following links: what a PATH
    lookup or git would run."""
    try:
        st = os.stat(path)
    except OSError:
        return False
    return stat.S_ISREG(st.st_mode) and bool(st.st_mode & 0o111)


def offends(target: Target, name: str) -> str | None:
    """Why ``name`` in ``target`` must be quarantined, or None."""
    if not runnable(os.path.join(target.directory, name)):
        return None
    if target.hooks:
        return None if name.endswith(".sample") else "a git hook"
    for later in target.later:
        if runnable(os.path.join(later, name)):
            return f"it shadowed {later}/{name}"
    return None


def quarantine(path: str) -> str | None:
    """Make ``path`` unrunnable; what was done, or None if it was gone.

    A link is removed (its target recorded). A file loses its execute bits
    through a descriptor opened without following links, so a link swapped
    in after the check is never followed; chmod goes through /proc so no
    read permission is needed.
    """
    try:
        st = os.lstat(path)
        if stat.S_ISLNK(st.st_mode):
            target = os.readlink(path)
            os.unlink(path)
            return f"removed the link {path} -> {target}"
        fd = os.open(path, os.O_PATH | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError:
        return None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            return None
        os.chmod(f"/proc/self/fd/{fd}", stat.S_IMODE(st.st_mode) & ~0o111)
    except OSError:
        return None
    finally:
        os.close(fd)
    return f"cleared the execute bits of {path}"


# --- state: the alerts and the baselines ----------------------------------------


def state_dir(preferred: str = STATE_DIR) -> str | None:
    """The directory for alerts and baselines, created 0755 if need be:
    ``preferred``, else FALLBACK_STATE_DIR. Only a real directory this user
    owns is used, never a link. None when neither will do."""
    for directory in (preferred, FALLBACK_STATE_DIR):
        try:
            os.makedirs(directory, 0o755, exist_ok=True)
            st = os.lstat(directory)
        except OSError:
            continue
        if stat.S_ISDIR(st.st_mode) and st.st_uid == os.geteuid():
            return directory
    return None


def record(state: str | None, lines: Sequence[str]) -> None:
    """Append ``lines`` to the alerts, each stamped with the time."""
    if state is None or not lines:
        return
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    text = "".join(f"{stamp} {line}\n" for line in lines)
    flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC
    try:
        fd = os.open(os.path.join(state, ALERTS), flags, 0o644)
    except OSError:
        return
    try:
        os.write(fd, text.encode(errors="surrogateescape"))
    finally:
        os.close(fd)


def _baseline_path(state: str, directory: str) -> str:
    name = hashlib.sha256(os.fsencode(directory)).hexdigest()[:24]
    return os.path.join(state, BASELINES, f"{name}.json")


def load_baseline(state: str, directory: str) -> dict[str, Sig] | None:
    try:
        with open(_baseline_path(state, directory), encoding="utf-8") as f:
            data: object = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    doc = cast(dict[str, object], data)
    entries = doc.get("entries")
    if doc.get("dir") != directory or not isinstance(entries, dict):
        return None
    return {
        name: tuple(cast(list[int | str], sig))
        for name, sig in cast(dict[str, object], entries).items()
        if isinstance(sig, list)
    }


def save_baseline(state: str, directory: str, sigs: Mapping[str, Sig]) -> None:
    path = _baseline_path(state, directory)
    tmp = f"{path}.{os.getpid()}"
    try:
        os.makedirs(os.path.dirname(path), 0o755, exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"dir": directory, "entries": sigs}, f)
        os.replace(tmp, path)
    except OSError:
        try:
            os.remove(tmp)
        except OSError:
            pass


def _states(states: Sequence[str] | None) -> Sequence[str]:
    return (STATE_DIR, FALLBACK_STATE_DIR) if states is None else states


def read_alerts(states: Sequence[str] | None = None) -> list[str]:
    alerts: list[str] = []
    for state in _states(states):
        try:
            with open(os.path.join(state, ALERTS), errors="replace") as f:
                alerts += f.read().splitlines()
        except OSError:
            continue
    return alerts


def clear_alerts(states: Sequence[str] | None = None) -> bool:
    """Empty the alerts and take each directory's current state as its
    baseline: what the user has reviewed, and perhaps restored, stands.
    False if an alerts file could not be emptied."""
    cleared = True
    for state in _states(states):
        alerts = os.path.join(state, ALERTS)
        try:
            with open(alerts, "w"):
                pass
        except FileNotFoundError:
            pass
        except OSError:
            cleared = False
        try:
            names = os.listdir(os.path.join(state, BASELINES))
        except OSError:
            continue
        for name in names:
            path = os.path.join(state, BASELINES, name)
            try:
                with open(path, encoding="utf-8") as f:
                    directory = json.load(f)["dir"]
            except (OSError, ValueError, KeyError, TypeError):
                continue
            if isinstance(directory, str):
                save_baseline(state, directory, snapshot(directory))
    return cleared


# --- the watcher -----------------------------------------------------------------


@dataclass
class Session:
    """One session's watch: what to look at and what has been done.

    ``path`` is the launching PATH, ``roots`` the resolved roots of the
    jail's read-write binds, ``workspace`` the bound workspace.
    """

    path: str
    roots: Sequence[str]
    workspace: str
    state: str | None
    probe: Probe = HOST
    baseline: dict[str, dict[str, Sig]] = field(
        default_factory=dict[str, dict[str, Sig]]
    )
    seen: dict[tuple[str, str], Sig] = field(default_factory=dict[tuple[str, str], Sig])
    actions: list[str] = field(default_factory=list[str])

    def targets(self) -> list[Target]:
        return targets(self.path, self.roots, self.workspace, self.probe)

    def _judge(
        self, target: Target, current: Mapping[str, Sig], base: Mapping[str, Sig]
    ) -> list[str]:
        done: list[str] = []
        for name, sig in sorted(current.items()):
            key = (target.directory, name)
            if base.get(name) == sig or self.seen.get(key) == sig:
                continue
            self.seen[key] = sig
            why = offends(target, name)
            if why is None:
                continue
            action = quarantine(os.path.join(target.directory, name))
            if action is not None:
                done.append(f"{action} ({why})")
        record(self.state, done)
        self.actions += done
        return done

    def scan_at_launch(self) -> list[str]:
        """Judge each directory against the baseline the last launch kept,
        then keep this one. A directory with no baseline yet gets one and is
        not judged. What was done, for the launch warnings."""
        done: list[str] = []
        for target in self.targets():
            current = snapshot(target.directory)
            stored = None
            if self.state is not None:
                stored = load_baseline(self.state, target.directory)
            if stored is not None:
                done += self._judge(target, current, stored)
                current = snapshot(target.directory)
            if self.state is not None:
                save_baseline(self.state, target.directory, current)
        return done

    def start(self) -> None:
        """Take the session's baseline: what is there now is left alone."""
        self.seen.clear()
        for target in self.targets():
            self.baseline[target.directory] = snapshot(target.directory)

    def tick(self) -> list[str]:
        """One pass. A directory that appeared since the start has an empty
        baseline: everything in it is new."""
        done: list[str] = []
        for target in self.targets():
            base = self.baseline.setdefault(target.directory, {})
            done += self._judge(target, snapshot(target.directory), base)
        return done

    def summary(self) -> str:
        if not self.actions:
            return ""
        lines = "".join(f"  {action}\n" for action in self.actions)
        return (
            "\033[1;31mclaude-sandbox: quarantined during this session:\033[0m\n"
            f"{lines}  Review the session that created them.\n"
        )


class Inotify:
    """Linux inotify through ctypes; ``available`` is False without it."""

    MASK = 0x8 | 0x80 | 0x100 | 0x4  # CLOSE_WRITE | MOVED_TO | CREATE | ATTRIB

    def __init__(self) -> None:
        self.fd = -1
        self._add: Callable[[int, bytes, int], int] | None = None
        try:
            import ctypes

            libc = ctypes.CDLL(None, use_errno=True)
            init1 = libc.inotify_init1
            self._add = libc.inotify_add_watch
            fd = init1(os.O_NONBLOCK | os.O_CLOEXEC)
        except (ImportError, OSError, AttributeError):
            return
        self.fd = int(fd)

    @property
    def available(self) -> bool:
        return self.fd >= 0

    def watch(self, directory: str) -> None:
        if self.available and self._add is not None:
            self._add(self.fd, os.fsencode(directory), self.MASK)

    def drain(self) -> None:
        try:
            while os.read(self.fd, 65536):
                pass
        except OSError:
            pass

    def close(self) -> None:
        if self.available:
            os.close(self.fd)
            self.fd = -1


def run(session: Session, stop: Callable[[], bool], wake: int = -1) -> None:
    """Watch until ``stop()``: a pass at each inotify event, every TICK, and
    when ``wake`` becomes readable."""
    notify = Inotify()
    try:
        while not stop():
            for target in session.targets():
                notify.watch(target.directory)
            session.tick()
            ready = [fd for fd in (notify.fd, wake) if fd >= 0]
            readable, _, _ = select.select(ready, [], [], TICK)
            if notify.fd in readable:
                notify.drain()
    finally:
        notify.close()
    session.tick()  # last look, so the summary is complete


@contextmanager
def watching(session: Session, report: Callable[[str], None]) -> Generator[None]:
    """Watch in a thread while the body runs (the jailed launch); then
    ``report`` the summary."""
    session.start()
    wake_r, wake_w = os.pipe()
    stopping = threading.Event()
    thread = threading.Thread(
        target=run, args=(session, stopping.is_set, wake_r), daemon=True
    )
    thread.start()
    try:
        yield
    finally:
        stopping.set()
        os.write(wake_w, b"x")
        thread.join(timeout=5)
        os.close(wake_r)
        os.close(wake_w)
        if summary := session.summary():
            report(summary)


def launcher_ended(pid: int) -> Callable[[], bool]:
    """Whether process ``pid`` (this one, before it execs) has exited.

    A pidfd, opened now and close-on-exec, so only a forked watcher keeps
    it; else, where the interpreter has no pidfd_open, a signal-0 probe of
    the pid, which a reused pid could fool only after the pids wrap round.
    """
    try:
        pidfd = os.pidfd_open(pid)
    except (AttributeError, OSError):

        def gone() -> bool:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return True
            except OSError:
                return False
            return False

        return gone
    return lambda: bool(select.select([pidfd], [], [], 0)[0])


def fork_watcher(session: Session) -> None:
    """Watch from a grandchild for a launch that execs (the jail off).

    Not a child: this process is about to become script(1), which must not
    find another child to wait for. The grandchild leaves the terminal's
    session, so ^C and ^Z never reach it, and stops within a tick of this
    process (by then script) exiting. Its summary goes to the terminal when
    stderr is one.
    """
    session.start()
    ended = launcher_ended(os.getpid())
    sys.stdout.flush()
    sys.stderr.flush()
    child = os.fork()
    if child:
        os.waitpid(child, 0)
        return
    _watcher(session, ended)  # pragma: no cover - the child


def _watcher(  # pragma: no cover
    session: Session, ended: Callable[[], bool]
) -> NoReturn:
    """The forked watcher (its tests run it in a real process: test_watch,
    watch_e2e)."""
    status = 0
    try:
        if os.fork():
            os._exit(0)
        os.setsid()
        null = os.open(os.devnull, os.O_RDWR)
        for fd in (0, 1) if os.isatty(2) else (0, 1, 2):
            os.dup2(null, fd)
        signal.signal(signal.SIGTERM, signal.SIG_DFL)
        run(session, ended)
        if summary := session.summary():
            os.write(2, summary.encode(errors="replace"))
    except BaseException:
        status = 1
    finally:
        os._exit(status)
