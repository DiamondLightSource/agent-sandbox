"""``python -m claude_sandbox``: the shadow shim's entry, else the front door.

The shim at /usr/local/bin/claude (and codex, pi) runs
``python -I -m claude_sandbox _shadow NAME -- ARG...`` (ADR 26), and the
egress jail's holder is ``python -I -m claude_sandbox _jail_holder -- COMMAND``.
Both are dispatched here before anything outside the standard library could be
imported, so the launch path stays stdlib-only. Anything else is the
``claude-sandbox`` CLI (``claude_sandbox.cli``), without the front door's
environment or its bash default.
"""

import sys

# Internal entries, dispatched here before anything outside the standard
# library could be imported: `_shadow` from the shim, and `_jail_holder`,
# which the egress jail re-enters inside `unshare -rn` (jail.HOLDER_ENTRY).
if len(sys.argv) > 1 and sys.argv[1] == "_jail_holder":
    from .jail import holder_main

    holder_main(sys.argv[2:])
elif len(sys.argv) > 1 and sys.argv[1] == "_shadow":
    from .shadow import main as shadow

    # The shim inserts the `--`, so the agent's own arguments are never
    # read as ours.
    if len(sys.argv) < 4 or sys.argv[3] != "--":
        sys.stderr.write("usage: python -I -m claude_sandbox _shadow NAME -- ARG...\n")
        sys.exit(2)
    shadow(sys.argv[2], sys.argv[4:])
else:
    from .cli import main as cli

    sys.exit(cli())
