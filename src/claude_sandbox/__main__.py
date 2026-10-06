"""``python -m claude_sandbox``: the shadow shim's entry, else the front door.

The shim at /usr/local/bin/claude (and codex, pi) runs
``python -I -m claude_sandbox _shadow NAME -- ARG...`` (ADR 26). That path
is dispatched here before anything outside the standard library could be
imported, so the launch path stays stdlib-only. Anything else is the
``claude-sandbox`` front door (``claude_sandbox:main``).
"""

import sys

# Internal entries, dispatched here before anything outside the standard
# library could be imported. Phase 2b adds the egress jail's holder entry
# beside `_shadow`.
if len(sys.argv) > 1 and sys.argv[1] == "_shadow":
    from .shadow import main as shadow

    # The shim inserts the `--`, so the agent's own arguments are never
    # read as ours.
    if len(sys.argv) < 4 or sys.argv[3] != "--":
        sys.stderr.write("usage: python -I -m claude_sandbox _shadow NAME -- ARG...\n")
        sys.exit(2)
    shadow(sys.argv[2], sys.argv[4:])
else:
    from . import main as front_door

    front_door()
