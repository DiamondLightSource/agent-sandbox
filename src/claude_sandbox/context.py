"""Where the CLI runs: on the HOST, in the CONTAINER, or in the JAIL.

``claude-sandbox`` is one command on both sides of the container boundary
(ADR 26). On the host it launches project containers; inside a container
it is the helper CLI; inside a sandboxed agent session (the jail) a few
helpers still work and the rest refuse. Commands declare where they run
with ``@requires``, and :func:`action` decides, once per call, whether a
command runs here, is forwarded into the project container, or refuses.

The signals are the ones the bash uses: ``IS_SANDBOX=1`` (set by the
shadow inside the jail), and the files podman and docker write into every
container. ``CLAUDE_SANDBOX_NESTED=1`` treats a container as a host, as
the bash launcher does (an engine inside a container, and the launcher
tests). ``CLAUDE_SANDBOX_CONTEXT=host|container`` is a test seam only, which lets
the helper suites run on a host; what changes the system (``update``)
checks the container files themselves. Neither can leave the jail: the jail
is decided first, and none of this is a security boundary — bwrap is.
"""

import functools
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import Enum
from typing import TypeVar


class Where(Enum):
    HOST = "host"
    CONTAINER = "container"
    JAIL = "jail"


HOST, CONTAINER, JAIL = Where.HOST, Where.CONTAINER, Where.JAIL

# Podman writes /run/.containerenv; docker writes /.dockerenv.
MARKERS = ("/run/.containerenv", "/.dockerenv")
# The shadow owns this name in a container where the sandbox is installed.
SHADOW = "/usr/local/bin/claude"


# Refusing to install (or update) outside a container: the installer runs apt
# and writes /etc and /usr/libexec, which would reshape a host.
HOST_INSTALL_REFUSAL = (
    "claude-sandbox: refusing to install outside a container.\n"
    "  Run this inside a devcontainer (as root), or set\n"
    "  CLAUDE_SANDBOX_HOST_INSTALL=1 to install on this host.\n"
)


def has_container_markers(exists: Callable[[str], bool] | None = None) -> bool:
    """The files podman and docker write into every container. Unlike
    :func:`detect`, no environment variable can stand in for them."""
    exists = exists or os.path.exists
    return any(exists(m) for m in MARKERS)


def may_install(env: Mapping[str, str] = os.environ) -> bool:
    """Whether install and update may change this system."""
    return has_container_markers() or env.get("CLAUDE_SANDBOX_HOST_INSTALL") == "1"


def detect(
    env: Mapping[str, str] = os.environ,
    exists: Callable[[str], bool] | None = None,
) -> Where:
    """Classify the current process from its environment and container files."""
    if env.get("IS_SANDBOX") == "1":
        return JAIL
    forced = env.get("CLAUDE_SANDBOX_CONTEXT", "")
    if forced in ("host", "container"):
        return Where(forced)
    if env.get("CLAUDE_SANDBOX_NESTED") == "1":
        return HOST
    return CONTAINER if has_container_markers(exists) else HOST


@functools.cache
def current() -> Where:
    """The context of this process, detected once."""
    return detect()


@dataclass(frozen=True)
class Requirement:
    """Where a command runs, and the context it is forwarded from, if any."""

    contexts: frozenset[Where]
    forward_from: Where | None = None


class Action(Enum):
    RUN = "run"
    FORWARD = "forward"
    REFUSE = "refuse"


F = TypeVar("F", bound=Callable[..., object])
_REQUIRED: dict[Callable[..., object], Requirement] = {}


def requires(*contexts: Where, forward_from: Where | None = None) -> Callable[[F], F]:
    """Declare the contexts a command runs in, e.g.
    ``@requires(CONTAINER, JAIL, forward_from=HOST)`` for ``verify``.

    A command called from ``forward_from`` is run inside the project
    container instead (the version installed there); called anywhere else
    it refuses.
    """

    def mark(fn: F) -> F:
        _REQUIRED[fn] = Requirement(frozenset(contexts), forward_from)
        return fn

    return mark


def requirement(fn: Callable[..., object]) -> Requirement:
    return _REQUIRED[fn]


def action(req: Requirement, where: Where) -> Action:
    if where in req.contexts:
        return Action.RUN
    if where is req.forward_from:
        return Action.FORWARD
    return Action.REFUSE


def sandbox_installed() -> bool:
    return os.access(SHADOW, os.X_OK)


def refusal(name: str, where: Where) -> str:
    """Why ``name`` does not run in ``where``: one line, no prefix."""
    if where is JAIL:
        return (
            f"refusing {name} inside a sandboxed agent session; "
            "run it from a shell outside the agent"
        )
    if where is CONTAINER:
        # The bash launcher's in-container messages, word for word.
        if sandbox_installed():
            return (
                "you are already inside an claude-sandbox container"
                " — run claude, codex or pi directly"
            )
        return (
            "this is a container without the sandbox"
            " — install it with: uvx claude-sandbox install"
        )
    return f"{name} runs inside a claude-sandbox container, not on the host"
