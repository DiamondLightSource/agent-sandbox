"""``claude-sandbox``: one CLI on the host, in the container and in the jail.

On the HOST it is the project-container launcher (``host/``); inside a
CONTAINER it is the helper CLI (``helpers/``); inside the JAIL a few
helpers still work. Every command declares where it runs with
``@requires``; this module asks :mod:`claude_sandbox.context` where it is
and runs, forwards or refuses each command accordingly.

On the host the launcher's own options come first and are parsed by hand
(:mod:`claude_sandbox.host.options`); the agent verbs,
``shell`` and every forwarded helper then get the rest of the command line
untouched. Elsewhere, and for ``clean``, argparse parses it.
"""

import argparse
import os
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from . import context
from .context import HOST, Action, Where
from .helpers import commands as helpers
from .host import commands as host
from .host import launcher, options

Run = Callable[[argparse.Namespace], int]


def rest(p: argparse.ArgumentParser) -> None:
    """Arguments taken as typed (shown in help; never parsed)."""
    p.add_argument("args", nargs=argparse.REMAINDER, metavar="ARGS")


@dataclass(frozen=True)
class Command:
    """A verb: its function and its arguments. A ``raw`` command gets its
    arguments as typed (``ns.tail``); argparse parses the others."""

    fn: Run
    arguments: Callable[[argparse.ArgumentParser], None] | None = None
    raw: bool = False

    @property
    def name(self) -> str:
        return self.fn.__name__.rstrip("_").replace("_", "-")

    @property
    def summary(self) -> str:
        return " ".join((self.fn.__doc__ or "").split())


COMMANDS = {
    c.name: c
    for c in (
        Command(host.claude, rest, raw=True),
        Command(host.codex, rest, raw=True),
        Command(host.pi, rest, raw=True),
        Command(host.shell, rest, raw=True),
        Command(host.clean, host.clean_arguments),
        Command(helpers.gh_auth, raw=True),
        Command(helpers.glab_auth, helpers.glab_auth_arguments),
        Command(helpers.verify, helpers.verify_arguments),
        Command(helpers.pi_local, rest, raw=True),
        Command(helpers.doctor, helpers.doctor_arguments),
        Command(helpers.version, raw=True),
        Command(helpers.update, raw=True),
        Command(helpers.install, raw=True),
        Command(helpers.help_, raw=True),
    )
}

HOST_HELP = """\
Before the command, the launcher's own options:
  --recreate       remove and recreate the project container (e.g. after a
                   pull); forge auth must be re-done after
  --bridge         create on the engine's bridge network, not --network=host
  --mount PATH     bind PATH read-only into the container (repeatable)
  --mount-rw PATH  bind PATH read-write into the container and the sandbox
  --gpu            enable all NVIDIA GPUs (host NVIDIA Container Toolkit)
  --device PATH    expose a /dev device node to the container and sandbox
  --peers          also bind the project's parent directory read-write
  --no-peers       the default: no parent-directory mount
  --version        print the launcher version

Only --bridge, --peers, --gpu, --device, --mount and --mount-rw are fixed
when the container is created (--recreate to change them). Words after the
options that are not a command go to claude: `claude-sandbox --resume`.
Helpers run inside the project container, as its installed claude-sandbox.

Environment: CLAUDE_SANDBOX_IMAGE, CLAUDE_SANDBOX_ENGINE (podman|docker),
CLAUDE_SANDBOX_SHARED_CONFIG, CLAUDE_SANDBOX_CONF, CLAUDE_SANDBOX_SHELL,
CLAUDE_SANDBOX_CACHE; any other CLAUDE_SANDBOX_* is passed to the container
when it is created.
"""


def verbs(where: Where) -> dict[str, Action]:
    """What each command does here: run, forward or refuse."""
    return {
        name: context.action(context.requirement(c.fn), where)
        for name, c in COMMANDS.items()
    }


def parser(where: Where) -> argparse.ArgumentParser:
    """The parser for ``where``, listing the commands that work here."""
    top = argparse.ArgumentParser(
        prog="claude-sandbox",
        description="Sandboxed Claude Code, Codex and Pi.",
        epilog=HOST_HELP if where is HOST else None,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = top.add_subparsers(dest="command", metavar="COMMAND")
    for name, act in verbs(where).items():
        c = COMMANDS[name]
        if act is Action.REFUSE:
            continue
        summary = c.summary + (" (in the container)" if act is Action.FORWARD else "")
        p = sub.add_parser(name, help=summary, description=summary)
        if act is Action.FORWARD:
            rest(p)
        elif c.arguments:
            c.arguments(p)
    return top


def _main(args: list[str], where: Where) -> int:
    top = parser(where)
    actions = verbs(where)
    opts = None
    if where is HOST:
        visible = [n for n, a in actions.items() if a is not Action.REFUSE]
        try:
            opts = options.parse(args, visible, launcher.version(os.environ))
        except options.Stop as stop:
            if stop.code < 0:
                top.print_help()
                return 0
            options.report(stop)
            return stop.code
        name, tail = opts.verb, opts.tail
    else:
        if args[:1] in (["-v"], ["--version"]):
            args = ["version"]
        elif args[:1] in ([], ["-h"], ["--help"]):
            args = ["help"]  # usage, exit 0
        name, tail = args[0], args[1:]
    act = actions.get(name)
    if act is Action.REFUSE:
        print(f"claude-sandbox: {context.refusal(name, where)}", file=sys.stderr)
        return 1
    if act is None:
        top.parse_args([name])  # argparse's own "invalid choice", exit 2
    c = COMMANDS[name]
    if c.raw or act is Action.FORWARD:
        ns = argparse.Namespace(command=name, tail=tail)
    else:
        ns = top.parse_args([name, *tail])
    ns.opts, ns.parser = opts, top
    return (host.forward if act is Action.FORWARD else c.fn)(ns)


def main(argv: Sequence[str] | None = None) -> int:
    """Run one command; its exit status."""
    args = list(sys.argv[1:] if argv is None else argv)
    try:
        return _main(args, context.current())
    except KeyboardInterrupt:
        print(file=sys.stderr)
        return 130
