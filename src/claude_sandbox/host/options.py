"""The launcher's own options, which come before the command.

Parsed by hand, as the bash loop does, rather than by argparse: the first
word that is not one of these ends them, and everything after it belongs
to the command or the agent (``claude-sandbox --resume`` is ``claude
--resume``). The errors exit 1, as the bash's do.
"""

import os
import stat
import sys
from collections.abc import Collection
from dataclasses import dataclass, field

# The verb when none is given, and for agent arguments with no verb.
DEFAULT_VERB = "claude"


@dataclass
class Options:
    """The launcher options, the command, and its arguments as typed."""

    recreate: bool = False
    host_net: bool = True
    peers: bool = False
    gpu: bool = False
    mounts_ro: list[str] = field(default_factory=list[str])
    mounts_rw: list[str] = field(default_factory=list[str])
    devices: list[str] = field(default_factory=list[str])
    # The create-time flags as typed, for the warning on reuse.
    create_opts: list[str] = field(default_factory=list[str])
    verb: str = DEFAULT_VERB
    tail: list[str] = field(default_factory=list[str])


class Stop(Exception):
    """Parsing ends the run: print ``message`` (if any) and exit ``code``."""

    def __init__(self, code: int, message: str = "", *, out: bool = False) -> None:
        super().__init__(message)
        self.code, self.message, self.out = code, message, out


def _device(arg: str) -> str:
    """``--device PATH`` resolved to a character or block device under /dev."""
    if not arg.startswith("/dev/") or "\n" in arg:
        raise Stop(1, "claude-sandbox: --device needs an absolute /dev device path")
    bad = (
        f"claude-sandbox: --device needs a character or block device under /dev: {arg}"
    )
    try:
        device = os.path.realpath(arg, strict=True)
        mode = os.stat(device).st_mode
    except OSError:
        raise Stop(1, bad) from None
    if not device.startswith("/dev/") or not (stat.S_ISCHR(mode) or stat.S_ISBLK(mode)):
        raise Stop(1, bad)
    return device


def _realpath(arg: str) -> str:
    """``realpath PATH``: every component but the last must exist."""
    path = os.path.realpath(arg) if arg else ""
    if not path or not os.path.isdir(os.path.dirname(path)):
        raise Stop(1, f"realpath: {arg}: No such file or directory")
    return path


def parse(args: list[str], verbs: Collection[str], version: str) -> Options:
    """The launcher options, then the verb (``claude`` when none is named).

    Raises :class:`Stop` for ``--version``, for ``--help`` (code -1: show
    the CLI's help) and for a bad option.
    """
    opts = Options()
    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "--recreate":
            opts.recreate = True
        elif arg == "--bridge":
            opts.host_net = False
            opts.create_opts.append(arg)
        elif arg == "--peers":
            opts.peers = True
            opts.create_opts.append(arg)
        elif arg == "--no-peers":
            opts.peers = False
        elif arg == "--gpu":
            opts.gpu = True
            opts.create_opts.append(arg)
        elif arg == "--device":
            if i + 1 >= len(args):
                raise Stop(
                    1, "claude-sandbox: --device needs an absolute /dev device path"
                )
            opts.devices.append(_device(args[i + 1]))
            opts.create_opts.append(f"--device {args[i + 1]}")
            i += 1
        elif arg in ("--mount", "--mount-rw"):
            if i + 1 >= len(args):
                raise Stop(1, f"claude-sandbox: {arg} needs a PATH")
            path = _realpath(args[i + 1])
            (opts.mounts_ro if arg == "--mount" else opts.mounts_rw).append(path)
            opts.create_opts.append(f"{arg} {args[i + 1]}")
            i += 1
        elif arg == "--version":
            raise Stop(0, f"claude-sandbox {version}", out=True)
        elif arg in ("-h", "--help"):
            raise Stop(-1)
        elif arg == "--":
            i += 1
            break
        else:
            break
        i += 1
    rest = args[i:]
    if rest and rest[0] in verbs:
        opts.verb, opts.tail = rest[0], rest[1:]
    else:
        opts.tail = rest
    return opts


def report(stop: Stop) -> None:
    if stop.message:
        print(stop.message, file=sys.stdout if stop.out else sys.stderr)
