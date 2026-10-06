"""The per-process egress jail (ADR 0015): NOT YET PORTED.

Issue #72 phase 2b ports ``jail_stage_dns``, ``netns_launch``,
``netns_holder`` and the loopback relays from
``.devcontainer/claude-sandbox/claude-shadow`` into this module. Until then
both entry points refuse. The jail is ON by default and fail-closed, so a
Python shadow whose configuration asks for the jail must never launch
without it: an unjailed fallback would quietly drop the default security
control.

The refusal names the fix (the default bash shadow), not the operator
opt-out: weakening switches are never advertised in messages (see the
``claude-sandbox`` skill, "weakening switches exist but are never
advertised").

Standard library only: this module is on the launch path (ADR 26).
"""

from collections.abc import Callable, Mapping, Sequence
from typing import NoReturn

from .config import Config
from .errors import SandboxError

NOT_PORTED = (
    "claude-sandbox: the Python shadow cannot run the egress jail yet"
    " (issue #72 phase 2b), so it will not launch.\n"
    "  Reinstall with the default bash shadow: ./install without"
    " CLAUDE_SANDBOX_IMPL=python."
)


def stage_dns(env: Mapping[str, str], warn: Callable[[str], None]) -> dict[str, str]:
    """Stage the jail's resolv.conf; return ``env`` with its path added.

    The seam for ``jail_stage_dns``, called before the argv is built.
    """
    raise SandboxError(NOT_PORTED)


def launch(config: Config, env: Mapping[str, str], command: Sequence[str]) -> NoReturn:
    """Run ``command`` (the script(1) wrap) inside the jail, and exit with
    the agent's status; the seam for ``netns_launch``."""
    raise SandboxError(NOT_PORTED)
