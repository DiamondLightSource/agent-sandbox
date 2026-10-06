"""``python -I -m claude_sandbox.installer [--image-build] --source TREE``.

What ``install.sh``'s bootstrap hands over to, from the provisioned venv.
``--container-start`` and ``--probe-userns`` are the published image's
entrypoint's share of the install (the steps an image build skips).
The environment carries the same settings ``install.sh`` reads
(``INSTALL_PREFIX``, ``CLAUDE_SANDBOX_IMPL``, ``WITH_CODEX``, ...);
``TREE`` is the clone, or the wheel's bundled tree, being installed.
"""

import argparse
import os
import sys
from dataclasses import replace
from pathlib import Path

from . import container_start, install
from .steps import InstallError, from_env
from .system import probe_userns_or_refuse


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="python -m claude_sandbox.installer")
    parser.add_argument("--image-build", action="store_true")
    parser.add_argument("--source", type=Path, required=True)
    only = parser.add_mutually_exclusive_group()
    only.add_argument("--container-start", action="store_true")
    only.add_argument("--probe-userns", action="store_true")
    args = parser.parse_args(argv)
    try:
        layout, options = from_env(args.source.resolve(), os.environ)
        if os.geteuid() == 0:
            layout = replace(layout, owner=(0, 0))
        if args.container_start:
            container_start(layout, options)
        elif args.probe_userns:
            probe_userns_or_refuse(options)
        else:
            install(layout, replace(options, image_build=args.image_build))
    except InstallError as exc:
        print(exc, file=sys.stderr)
        return exc.code
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
