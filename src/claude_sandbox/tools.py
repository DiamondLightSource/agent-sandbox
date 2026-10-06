"""Where the launch path finds the programs it runs.

ADR 26: no executable is found through PATH. The shadow and the egress jail
run system tools (script, bwrap, git, unshare, pasta, ...) only from this
fixed list of root-owned system directories, resolved once to an absolute
path, whatever PATH the caller has.

Standard library only: this module is on the launch path (ADR 26).
"""

import os
from collections.abc import Sequence

TOOL_PATH: tuple[str, ...] = ("/usr/bin", "/bin", "/usr/sbin", "/sbin")


def find_tool(name: str, *, search: Sequence[str] = TOOL_PATH) -> str | None:
    """The absolute path of executable regular file ``name`` in ``search``.

    The first directory that has one wins; None when none does. ``name``
    must be a bare file name. ``search`` exists for tests.
    """
    if not name or "/" in name:
        return None
    for directory in search:
        path = f"{directory}/{name}"
        if os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    return None
