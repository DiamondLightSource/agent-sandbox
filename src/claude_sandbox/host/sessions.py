"""Each launcher session's processes inside the project container (issue #69).

A session is ``exec -it`` into the keeper. When the host terminal closes,
only the engine's client dies: conmon (or dockerd) holds the pty, so
nothing inside ever sees a hangup and the session runs on. So the
launcher wraps every session in ``start``, which records the session's
first process here and execs the command in its place, and on the way out
(normal exit, SIGHUP or SIGTERM) runs ``end``, which hangs up that process
and everything under it, then prints how many other sessions still live:
the keeper is stopped at 0.

This runs inside the container, by the image's root-owned interpreter with
``-I``. The launcher passes this file's source with ``-c`` rather than
naming the module, so a container made from an older 5.x image, which
lacks it, is tracked too. Standard library only, and self-contained.
"""

import os
import signal
import sys
import time

# Root-owned; read-only inside the jail, which binds / read-only.
STATE = "/run/claude-sandbox-sessions"
PROC = "/proc"
# How long a hung-up process has to exit before it is killed.
GRACE = 3.0


def stat(pid: int, proc: str = PROC) -> tuple[int, str] | None:
    """``(ppid, start time)`` of a live process; None when gone or a zombie."""
    try:
        with open(f"{proc}/{pid}/stat", encoding="utf-8", errors="replace") as f:
            fields = f.read().rsplit(") ", 1)[1].split()
    except (OSError, IndexError):
        return None
    if fields[0] in "ZX":
        return None
    return int(fields[1]), fields[19]


def live(entry: str, proc: str = PROC) -> int | None:
    """The PID a session file names, while that same process lives."""
    try:
        pid, start = entry.split()
        alive = stat(int(pid), proc)
    except ValueError:
        return None
    return int(pid) if alive and alive[1] == start else None


def tree(root: int, proc: str = PROC) -> dict[int, str]:
    """``root`` and its descendants, each with its start time."""
    children: dict[int, list[tuple[int, str]]] = {}
    for name in os.listdir(proc):
        if name.isdigit() and (st := stat(int(name), proc)):
            children.setdefault(st[0], []).append((int(name), st[1]))
    root_stat = stat(root, proc)
    found: dict[int, str] = {}
    todo = [(root, root_stat[1])] if root_stat else []
    while todo:
        pid, start = todo.pop()
        found[pid] = start
        todo += children.get(pid, [])
    return found


def hang_up(root: int, proc: str = PROC, grace: float = GRACE) -> None:
    """SIGHUP the tree under ``root``, as a closed terminal would; SIGKILL
    whatever of it is still there after ``grace`` seconds."""
    members = tree(root, proc)
    for pid in members:
        signal_quietly(pid, signal.SIGHUP)
    deadline = time.monotonic() + grace
    while True:
        left = [p for p, s in members.items() if (st := stat(p, proc)) and st[1] == s]
        if not left or time.monotonic() >= deadline:
            break
        time.sleep(0.1)
    for pid in left:
        signal_quietly(pid, signal.SIGKILL)


def signal_quietly(pid: int, signum: int) -> None:
    try:
        os.kill(pid, signum)
    except ProcessLookupError:
        pass


def read(path: str) -> str:
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except OSError:
        return ""


def remove(path: str) -> None:
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass


def start(sid: str, command: list[str], state: str = STATE) -> None:
    """Record this process as session ``sid``, then become ``command``."""
    os.makedirs(state, mode=0o700, exist_ok=True)
    me = stat(os.getpid())
    assert me, "this process is alive"
    with open(f"{state}/{sid}", "w", encoding="utf-8") as f:
        f.write(f"{os.getpid()} {me[1]}\n")
    try:
        os.execv(command[0], command)
    except OSError as e:
        remove(f"{state}/{sid}")
        print(f"claude-sandbox: cannot run {command[0]}: {e.strerror}", file=sys.stderr)
        sys.exit(127)


def end(sid: str, state: str = STATE, proc: str = PROC, grace: float = GRACE) -> int:
    """Hang up session ``sid`` if it still runs; the number of other live
    sessions, forgetting those that are gone."""
    path = f"{state}/{sid}"
    pid = live(read(path), proc)
    remove(path)
    if pid:
        hang_up(pid, proc, grace)
    others = 0
    for name in os.listdir(state) if os.path.isdir(state) else []:
        if live(read(f"{state}/{name}"), proc):
            others += 1
        else:
            remove(f"{state}/{name}")
    return others


def main(argv: list[str]) -> int:
    match argv:
        case ["start", sid, _, *_] if sid.isalnum():
            start(sid, argv[2:])
        case ["end", sid] if sid.isalnum():
            print(end(sid))
        case _:
            print("usage: start ID COMMAND... | end ID", file=sys.stderr)
            return 2
    return 0


if __name__ == "__main__":  # pragma: no cover - run inside the container
    sys.exit(main(sys.argv[1:]))
