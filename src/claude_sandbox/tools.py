"""Where the launch path finds the programs it runs, and the few things it
does with programs, files and the terminal that other modules do too.

ADR 26: no executable is found through PATH. The shadow and the egress jail
run system tools (script, bwrap, git, unshare, pasta, ...) only from this
fixed list of root-owned system directories, resolved once to an absolute
path, whatever PATH the caller has.

Standard library only: this module is on the launch path (ADR 26).
"""

import os
import subprocess
import tempfile
import termios
from collections.abc import Mapping, Sequence

TOOL_PATH: tuple[str, ...] = ("/usr/bin", "/bin", "/usr/sbin", "/sbin")


def find_tool(name: str, *, search: Sequence[str] = TOOL_PATH) -> str | None:
    """The absolute path of executable regular file ``name`` in ``search``.

    The first directory that has one wins; None when none does. ``name``
    must be a bare file name.
    """
    if not name or "/" in name:
        return None
    for directory in search:
        path = f"{directory}/{name}"
        if os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    return None


def output(
    argv: Sequence[str],
    env: Mapping[str, str] | None = None,
    *,
    timeout: float | None = None,
) -> tuple[int, str]:
    """Run ``argv`` with stdin and stderr closed: its status and its output
    without trailing newlines; 127 when it cannot run or does not finish in
    ``timeout`` seconds."""
    try:
        done = subprocess.run(
            argv,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return 127, ""
    return done.returncode, os.fsdecode(done.stdout).rstrip("\n")


def shell_status(returncode: int) -> int:
    """A child's exit status as a shell reports it: 128+N after signal N."""
    return 128 - returncode if returncode < 0 else returncode


def spawn_and_wait(path: str, argv: list[str], env: Mapping[str, str]) -> int:
    """Run ``argv`` on this terminal and return its status as a shell
    reports it. Ctrl-C is the child's to handle, so an interrupt that
    reaches this process is waited out; anything else that stops the wait
    stops the child too."""
    proc = subprocess.Popen(argv, executable=path, env=dict(env))
    try:
        while True:
            try:
                return shell_status(proc.wait())
            except KeyboardInterrupt:
                continue
    finally:
        if proc.returncode is None:
            proc.terminate()
            proc.wait()


def write_atomic(
    path: str | os.PathLike[str],
    data: bytes,
    mode: int,
    owner: tuple[int, int] | None = None,
) -> None:
    """Replace ``path`` with ``data``: a temporary file beside it, given
    ``mode`` (and ``owner``), renamed over it. A reader never sees half a
    file, a link at ``path`` is replaced rather than followed, and the
    temporary file goes on any failure."""
    directory, name = os.path.split(str(path))
    fd, tmp = tempfile.mkstemp(dir=directory or ".", prefix=f".{name}.")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            os.fchmod(f.fileno(), mode)
            if owner is not None:
                os.fchown(f.fileno(), *owner)
        os.replace(tmp, path)
    except BaseException:
        os.unlink(tmp)
        raise


def read_key(fd: int) -> bytes:
    """One key from the terminal on ``fd``, unechoed; b"" at end of input."""
    saved = termios.tcgetattr(fd)
    quiet = termios.tcgetattr(fd)
    quiet[3] &= ~(termios.ECHO | termios.ICANON)
    quiet[6][termios.VMIN] = 1
    quiet[6][termios.VTIME] = 0
    try:
        termios.tcsetattr(fd, termios.TCSANOW, quiet)
        return os.read(fd, 1)
    finally:
        termios.tcsetattr(fd, termios.TCSANOW, saved)


def working_directory(env: Mapping[str, str]) -> str:
    """The shell's ``$PWD``: the inherited PWD when it names the current
    directory, so a directory reached through a symlink keeps the path the
    user typed; else ``getcwd``."""
    pwd = env.get("PWD", "")
    try:
        if pwd.startswith("/") and os.path.samefile(pwd, "."):
            return pwd
    except OSError:
        pass
    return os.getcwd()
