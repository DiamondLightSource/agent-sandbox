"""What a session's jail shows of host paths: ``python -I -m claude_sandbox _scope``.

The VS Code extension (claude-sandbox-vscode) sends Claude a file's text only
when the jail would let Claude read that file anyway, and opens diffs only for
files it could write. It asks here rather than guessing, through the
root-owned interpreter as the shim does::

    python -I -m claude_sandbox _scope CWD -- PATH...

CWD is the folder the session starts in. One JSON object per PATH, in order,
is printed on its own line: ``{"path": PATH, "read": BOOL, "write": BOOL}``.

The answer is read off the argv ``bwrap_build`` returns for a ``claude``
launch from CWD, with the conf and the environment the shadow would use, so
it cannot drift from the jail: bwrap applies its mounts in argv order, and
the last one at a path or above it decides what the jail shows there. Only a
bind of a host path onto the same path shows the host's file; a tmpfs, a
fresh /dev, /dev/null or a bind from elsewhere hides it. An argv option this
module does not know is an error, never a guess.

This adds nothing to the bwrap argv and runs nothing; it only reads it.
Standard library only, like the launch path.
"""

import json
import os
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from .bwrap import HOST, Probe, bwrap_build
from .config import CONFIG_PATH, Config, parse_config, resolve_workspace_root
from .errors import SandboxError
from .profiles import PROFILES

# bwrap's options as bwrap_build emits them, by argument count.
MOUNTS = {
    "--bind": 2,
    "--bind-try": 2,
    "--ro-bind": 2,
    "--ro-bind-try": 2,
    "--dev-bind": 2,
    "--dev-bind-try": 2,
    "--dev": 1,
    "--proc": 1,
    "--tmpfs": 1,
}
OTHERS = {
    "--setenv": 2,
    "--unsetenv": 1,
    "--cap-drop": 1,
    "--chdir": 1,
    "--clearenv": 0,
    "--die-with-parent": 0,
    "--new-session": 0,
    "--unshare-user-try": 0,
    "--unshare-pid": 0,
    "--unshare-ipc": 0,
    "--unshare-uts": 0,
    "--unshare-cgroup-try": 0,
    "--unshare-net": 0,
}
# A host path shows through only on a bind of itself; these also let it be
# written.
SHOWN = {"--bind", "--bind-try", "--ro-bind", "--ro-bind-try"}
WRITABLE = {"--bind", "--bind-try"}


@dataclass(frozen=True)
class Mount:
    op: str
    source: str  # empty for --dev, --proc and --tmpfs
    dest: str


@dataclass(frozen=True)
class View:
    read: bool
    write: bool


def mounts(argv: Sequence[str]) -> list[Mount]:
    """The mounts in ``argv``, in order, up to the ``--`` before the command."""
    found: list[Mount] = []
    i = 1  # argv[0] is bwrap
    while i < len(argv) and argv[i] != "--":
        op = argv[i]
        if op in MOUNTS:
            n = MOUNTS[op]
            if i + n >= len(argv):
                raise SandboxError(f"claude-sandbox: {op} is missing its arguments")
            found.append(Mount(op, argv[i + 1] if n == 2 else "", argv[i + n]))
        elif op in OTHERS:
            n = OTHERS[op]
        else:
            raise SandboxError(f"claude-sandbox: unknown bwrap option {op}")
        i += n + 1
    return found


def _resolved(path: str, probe: Probe) -> str:
    try:
        return probe.realpath(path)
    except OSError:
        return os.path.normpath(path)


def _covers(dest: str, path: str) -> bool:
    return os.path.commonpath([dest, path]) == dest


def view(argv: Sequence[str], path: str, probe: Probe = HOST) -> View:
    """What the jail ``argv`` builds shows at host ``path``: the host's file
    (read, and perhaps write) or not at all."""
    if not path.startswith("/"):
        return View(False, False)
    target = _resolved(path, probe)
    last: Mount | None = None
    for m in mounts(argv):
        dest = _resolved(m.dest, probe)
        if _covers(dest, target) or _covers(m.dest, target):
            last = m
    if last is None or last.op not in SHOWN:
        return View(False, False)
    if _resolved(last.source, probe) != _resolved(last.dest, probe):
        return View(False, False)
    return View(True, last.op in WRITABLE)


def session_argv(
    cwd: str, env: Mapping[str, str], config_path: str = CONFIG_PATH
) -> list[str]:
    """The bwrap argv a ``claude`` launch from ``cwd`` would get."""
    env = parse_config(config_path, env)
    config = Config.from_env(env)
    profile = PROFILES["claude"]
    workspace = resolve_workspace_root(config, cwd)
    return bwrap_build(profile, config, env, workspace, profile.real, [])


def main(
    args: Sequence[str],
    env: Mapping[str, str] | None = None,
    config_path: str = CONFIG_PATH,
) -> int:
    """``_scope CWD -- PATH...``: one JSON line per PATH; 2 on bad usage, 1
    when the launch itself would be refused."""
    if len(args) < 2 or args[1] != "--" or not args[0].startswith("/"):
        sys.stderr.write("usage: python -I -m claude_sandbox _scope CWD -- PATH...\n")
        return 2
    try:
        argv = session_argv(
            args[0], dict(os.environ) if env is None else env, config_path
        )
        for path in args[2:]:
            v = view(argv, path)
            print(json.dumps({"path": path, "read": v.read, "write": v.write}))
    except (SandboxError, OSError) as e:
        sys.stderr.write(f"{e}\n")
        return 1
    return 0
