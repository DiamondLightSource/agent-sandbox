"""The commands that run on the host: the agent verbs, ``shell`` and ``clean``.

Each command takes the parsed namespace. The CLI puts the launcher options
in ``ns.opts`` (from :func:`claude_sandbox.host.options.parse`), and the
agent verbs and ``shell`` hand ``ns.opts.tail`` over untouched.
"""

import argparse
import os

from ..context import HOST, requires
from . import launcher
from .options import Options


def options(ns: argparse.Namespace) -> Options:
    opts = ns.opts
    assert isinstance(opts, Options), "the CLI sets the launcher options"
    return opts


def session(ns: argparse.Namespace, command: list[str], *, pause: bool) -> int:
    """Run ``command`` in the project container; its exit status."""
    return launcher.launcher(options(ns)).session(command, pause=pause)


def forward(ns: argparse.Namespace) -> int:
    """Run the container's own ``claude-sandbox VERB ARGS`` (its installed
    version), so a helper typed on the host does what it does inside."""
    opts = options(ns)
    return session(ns, ["claude-sandbox", opts.verb, *opts.tail], pause=False)


@requires(HOST)
def claude(ns: argparse.Namespace) -> int:
    """a sandboxed Claude Code session (the default)"""
    return session(ns, ["claude", *options(ns).tail], pause=True)


@requires(HOST)
def codex(ns: argparse.Namespace) -> int:
    """a sandboxed Codex session"""
    return session(ns, ["codex", *options(ns).tail], pause=True)


@requires(HOST)
def pi(ns: argparse.Namespace) -> int:
    """a sandboxed Pi session"""
    return session(ns, ["pi", *options(ns).tail], pause=True)


@requires(HOST)
def shell(ns: argparse.Namespace) -> int:
    """a plain, UNSANDBOXED shell in the container (e.g. for gh-auth)"""
    want = os.environ.get("CLAUDE_SANDBOX_SHELL") or launcher.detect_shell(os.environ)
    command = ["sh", "-c", launcher.SHELL_SCRIPT, "_", want, *options(ns).tail]
    return session(ns, command, pause=False)


def clean_arguments(p: argparse.ArgumentParser) -> None:
    p.add_argument("--force", action="store_true", help="also remove running ones")
    p.add_argument("--images", action="store_true", help="also remove unused tags")


@requires(HOST)
def clean(ns: argparse.Namespace) -> int:
    """remove the STOPPED project containers (and their forge logins)"""
    launcher.launcher(options(ns)).clean(bool(ns.force), bool(ns.images))
    return 0
