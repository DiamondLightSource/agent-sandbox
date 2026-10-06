"""The Python installer (ADR 26, issue #72 phase 4), as a library.

Not wired in yet: ``install.sh`` is still what every install runs. This
package holds the same steps, each a plan (``steps.plan_*``, reading only)
and an apply (``actions.apply``), so the switch-over replaces the bash
function by function. ``provision`` installs the pinned interpreter and
venv the shadow shim will run. Standard library only: it runs as root.
"""

import os
from typing import TextIO

from .actions import apply
from .steps import STEPS, Layout, Options


def install(layout: Layout, options: Options, err: TextIO | None = None) -> None:
    """Run every ported step in ``install.sh``'s order. Each step is planned
    against what the previous ones left, then applied, under umask 022
    whatever the caller's: new directories are 0755."""
    old = os.umask(0o022)
    try:
        for _name, plan in STEPS:
            apply(plan(layout, options), err)
    finally:
        os.umask(old)
