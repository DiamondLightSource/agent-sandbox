"""The Python installer (ADR 26, issue #72 phase 4).

``install.sh`` runs it when ``CLAUDE_SANDBOX_IMPL=python`` is set: its bash
bootstrap fetches uv, ``provision`` installs the pinned interpreter and the
venv, and the venv runs ``python -I -m claude_sandbox.installer``. Without
the opt-in the bash installer runs, unchanged. Each file step is a plan
(``steps.plan_*``, reading only) and an apply (``actions.apply``); the
steps that touch the system are in ``system``. Standard library only: it
runs as root.
"""

import os
import subprocess
import sys
from typing import TextIO

from . import system
from .actions import Warn, apply
from .steps import (
    CONF,
    LIBEXEC,
    STEPS,
    Layout,
    Options,
    is_mount,
    plan_conf,
    plan_cred_dirs,
    plan_shared_links,
)


def install(
    layout: Layout,
    options: Options,
    err: TextIO | None = None,
    out: TextIO | None = None,
    run: system.Run = subprocess.run,
) -> None:
    """Run ``install.sh``'s main() in its order, then print its summary.
    Each file step is planned against what the previous ones left, then
    applied, under umask 022 whatever the caller's: new directories are
    0755."""
    warn: TextIO = err or sys.stderr
    old = os.umask(0o022)
    skipped: list[str] = []
    try:
        system.probe_or_refuse(options)
        for name, plan in STEPS:
            if name == "link_terminal_config":
                system.apt_install(options, run)
                if not options.image_build:
                    system.probe_userns_or_refuse(options, run)
            actions = plan(layout, options)
            if any(isinstance(a, Warn) for a in actions):
                skipped.append(name)
            apply(actions, warn)
            if name == "link_terminal_config":
                system.install_claude_binary(layout, options, run)
                system.install_codex_binary(layout, options, warn, run)
                system.install_pi_binary(layout, options, warn, run)
    finally:
        os.umask(old)
    venv = f"{LIBEXEC}/venv"
    print(system.summary(layout, options, skipped, venv), end="", file=out)


def container_start(
    layout: Layout, options: Options, err: TextIO | None = None
) -> None:
    """What the published image's entrypoint redoes at each start, where the
    runtime mounts are: the shared-config links, the credential directories,
    and the conf, unless the operator mounted their own over it."""
    old = os.umask(0o022)
    try:
        apply(plan_shared_links(layout, options), err)
        apply(plan_cred_dirs(layout, options), err)
        if not is_mount(str(layout.system(CONF))):
            apply(plan_conf(layout, options), err)
    finally:
        os.umask(old)
