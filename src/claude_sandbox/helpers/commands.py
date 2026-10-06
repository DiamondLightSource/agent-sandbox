"""The helper commands and where they run.

Inside a container each runs here. Typed on the host, ``@requires(...,
forward_from=HOST)`` runs it in the project container instead, as that
container's installed ``claude-sandbox``. Inside the jail a helper that
would take a credential, change the install or edit the user's files
refuses: the agent shares that terminal.
"""

import argparse
import os
import subprocess
import sys
import tempfile

from .. import context, watch
from ..context import CONTAINER, HOST, JAIL, requires
from ..tools import find_tool
from . import auth
from .doctor import Doctor
from .pi_local import pi_local as configure_pi

REPO_URL = "https://github.com/DiamondLightSource/claude-sandbox"
LIBEXEC = "/usr/libexec/claude-sandbox"
AGENTS = ("claude", "codex", "pi")
# Only the published image has this; it is updated by pulling a new image.
IMAGE_INSTALL = "/opt/claude-sandbox"


def _exec(path: str, args: list[str]) -> int:
    os.execv(path, [path, *args])


def _fail(message: str, code: int = 1) -> int:
    print(f"claude-sandbox: {message}", file=sys.stderr)
    return code


def _cat(path: str) -> str | None:
    """``$(cat PATH)``, or None when it cannot be read."""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read().rstrip("\n")
    except OSError:
        return None


# PATs: typed only where the agent cannot read the terminal (Invariant 2).
@requires(CONTAINER, forward_from=HOST)
def gh_auth(ns: argparse.Namespace) -> int:
    """authenticate gh with a GitHub PAT (kept out of shell history)"""
    return auth.gh_auth()


def glab_auth_arguments(p: argparse.ArgumentParser) -> None:
    p.add_argument("hostname", nargs="?", default=auth.GITLAB)


@requires(CONTAINER, forward_from=HOST)
def glab_auth(ns: argparse.Namespace) -> int:
    """authenticate glab with a GitLab PAT (default host: gitlab.diamond.ac.uk)"""
    return auth.glab_auth(str(ns.hostname))


def verify_arguments(p: argparse.ArgumentParser) -> None:
    p.add_argument("--agent", choices=AGENTS, default="claude")


@requires(CONTAINER, JAIL, forward_from=HOST)
def verify(ns: argparse.Namespace) -> int:
    """run the isolation battery directly (no agent login needed)"""
    if context.current() is JAIL:
        return _exec("/bin/bash", [f"{LIBEXEC}/verify-sandbox-battery.sh"])
    return _exec(f"/usr/local/bin/{ns.agent}", ["--sandbox-verify"])


@requires(CONTAINER, JAIL, forward_from=HOST)
def pi_local(ns: argparse.Namespace) -> int:
    """configure Pi's lllm2 provider: [--port PORT] or MODEL CONTEXT [PORT]"""
    return configure_pi(list[str](ns.tail))


def alerts_arguments(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--clear",
        action="store_true",
        help="empty the list and accept what the directories hold now",
    )


# Not in the jail: the alerts are what a session must not see or clear.
@requires(CONTAINER, forward_from=HOST)
def alerts(ns: argparse.Namespace) -> int:
    """list what the PATH watcher quarantined (ADR 27); --clear once reviewed"""
    if ns.clear:
        if not watch.clear_alerts():
            return _fail("could not empty the alerts (run as root).")
        return 0
    for line in watch.read_alerts():
        print(line)
    return 0


def doctor_arguments(p: argparse.ArgumentParser) -> None:
    p.add_argument("--fix", action="store_true", help="apply it, with backups")


@requires(CONTAINER, JAIL, forward_from=HOST)
def doctor(ns: argparse.Namespace) -> int:
    """check the recommended setup: container tag in the status line, Pi
    footer and shell prompts"""
    if ns.fix and context.current() is JAIL:
        return _fail(
            "refusing doctor --fix inside a sandboxed agent session;"
            " run it from a shell."
        )
    return Doctor(bool(ns.fix)).run()


@requires(CONTAINER, JAIL, forward_from=HOST)
def version(ns: argparse.Namespace) -> int:
    """show the installed claude-sandbox version"""
    path = os.environ.get("CLAUDE_SANDBOX_VERSION_FILE") or f"{LIBEXEC}/version"
    stamped = _cat(path)
    if stamped is None:
        return _fail(f"version unknown ({path} missing — re-run install)")
    print(f"claude-sandbox {stamped}")
    return 0


@requires(CONTAINER, forward_from=HOST)
def update(ns: argparse.Namespace) -> int:
    """clone and install the latest claude-sandbox release"""
    if not context.may_install():
        sys.stderr.write(context.HOST_INSTALL_REFUSAL)
        return 1
    if os.geteuid() != 0:
        return _fail("update must run as root (install requires it).")
    if os.path.isdir(IMAGE_INSTALL):
        return _fail(
            "this is the published container image — update by pulling a newer image"
            " and recreating the container (uvx claude-sandbox@latest --recreate)."
        )
    installer = (
        os.environ.get("CLAUDE_SANDBOX_INSTALLER_FILE") or f"{LIBEXEC}/installer"
    )
    if _cat(installer) == "uvx":
        # The wheel is the pin (ADR 23): a git clone here would step past it.
        return _fail(
            "this sandbox was installed from the PyPI wheel — update with:"
            " uvx claude-sandbox@latest install\n"
            "  (or bump the pinned version in your devcontainer's postCreate)"
        )
    git = find_tool("git")
    if git is None:
        return _fail("git is not installed")
    tmp = tempfile.mkdtemp()
    clone = [git, "clone", "--quiet", REPO_URL, f"{tmp}/claude-sandbox"]
    rc = subprocess.run(clone, check=False).returncode
    if rc:
        return rc
    # `install` picks the newest stable tag; the clone is removed after.
    script = '/bin/bash "$1/claude-sandbox/install" && /bin/rm -rf "$1"'
    return _exec("/bin/bash", ["-c", script, "claude-sandbox-update", tmp])


@requires(HOST, CONTAINER, JAIL)
def install(ns: argparse.Namespace) -> int:
    """explain where the sandbox is installed from"""
    where = context.current()
    if where is HOST:
        return _fail(
            "install is a uvx verb: run `uvx claude-sandbox install` INSIDE the"
            " devcontainer",
            2,
        )
    if not context.sandbox_installed():
        return _fail(context.refusal("install", CONTAINER))
    print(
        "claude-sandbox is already installed in this container."
        " No installation is needed."
    )
    print("Run claude, codex or pi to start a sandboxed agent session.")
    return 0


@requires(CONTAINER, JAIL)
def help_(ns: argparse.Namespace) -> int:
    """show this help"""
    parser = ns.parser
    assert isinstance(parser, argparse.ArgumentParser)
    parser.print_help()
    return 0
