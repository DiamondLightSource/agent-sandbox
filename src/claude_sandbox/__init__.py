"""The uvx front door for claude-sandbox (ADR 23).

This module holds no sandbox logic. It locates the bash files shipped
under ``tree/`` (the launcher, the installer and what the installer
reads), sets the two environment variables that tie the wheel version to
what runs, and execs bash. Read the bash: it is what actually runs.
"""

import os
import re
import sys

from importlib.resources import files

IMAGE = "ghcr.io/diamondlightsource/claude-sandbox"


def _release_tag() -> str:
    """The git tag this wheel was built from, as the image is tagged.

    hatch-vcs writes the PEP 440 form to ``_version.py`` (``4.0.0-beta.1``
    becomes ``4.0.0b1``); image tags are the raw git tag, so map back.
    """
    from ._version import __version__

    names = {"a": "alpha", "b": "beta", "rc": "rc"}
    return re.sub(
        r"^(\d+(?:\.\d+)*)(a|b|rc)(\d+)",
        lambda m: f"{m.group(1)}-{names[m.group(2)]}.{m.group(3)}",
        __version__,
    )


def _in_container() -> bool:
    """True inside podman or docker, where ``install`` belongs.

    Podman writes ``/run/.containerenv`` and sets ``container``; docker
    writes ``/.dockerenv``. A devcontainer is one of the two.
    """
    return (
        os.path.exists("/run/.containerenv")
        or os.path.exists("/.dockerenv")
        or bool(os.environ.get("container"))
    )


def main() -> None:
    """Exec the launcher, or the installer when the first word is ``install``."""
    tree = str(files(__name__).joinpath("tree"))
    launcher = os.path.join(tree, "container", "claude-container")
    ver = _release_tag()
    argv = sys.argv[1:]
    env = dict(os.environ)
    if argv and argv[0] == "install":
        # The installer runs apt and writes /etc and /usr/libexec. Outside a
        # container that would reshape the host; the twelve-line postCreate
        # it replaces could not be typed on a host by accident, and this can.
        if not _in_container() and env.get("CLAUDE_SANDBOX_HOST_INSTALL") != "1":
            sys.stderr.write(
                "claude-sandbox: refusing to install outside a container.\n"
                "  Run this inside a devcontainer (as root), or set\n"
                "  CLAUDE_SANDBOX_HOST_INSTALL=1 to install on this host.\n"
            )
            sys.exit(1)
        # The shipped tree has no .git, so the installer would stamp
        # `unknown`; the wheel version is the release it was built from.
        env.setdefault("CLAUDE_SANDBOX_VERSION", ver)
        env["CLAUDE_SANDBOX_INSTALLER"] = "uvx"
        os.execvpe("bash", ["bash", os.path.join(tree, "install"), *argv[1:]], env)
    env.setdefault("CLAUDE_SANDBOX_LAUNCHER_VERSION", ver)
    # The wheel pins the image: launcher and image are the same release. A
    # wheel built between tags (a dev version) has no image of its own.
    tag = "latest" if ("dev" in ver or "+" in ver) else ver
    env.setdefault("CLAUDE_SANDBOX_IMAGE", f"{IMAGE}:{tag}")
    env["CLAUDE_SANDBOX_LAUNCHER"] = "uvx"
    os.execvpe("bash", ["bash", launcher, *argv], env)
