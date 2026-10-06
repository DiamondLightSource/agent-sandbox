"""``python -I -m claude_sandbox.installer [--image-build] --source TREE``.

What ``install.sh``'s bootstrap hands over to, from the provisioned venv.
The environment carries the same settings ``install.sh`` reads
(``INSTALL_PREFIX``, ``CLAUDE_SANDBOX_IMPL``, ``WITH_CODEX``, ...);
``TREE`` is the clone, or the wheel's bundled tree, being installed.
"""

import argparse
import os
import sys
from dataclasses import replace
from pathlib import Path

from . import install
from .steps import InstallError, from_env


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="python -m claude_sandbox.installer")
    parser.add_argument("--image-build", action="store_true")
    parser.add_argument("--source", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        layout, options = from_env(args.source.resolve(), os.environ)
        if os.geteuid() == 0:
            layout = replace(layout, owner=(0, 0))
        install(layout, replace(options, image_build=args.image_build))
    except InstallError as exc:
        print(exc, file=sys.stderr)
        return exc.code
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
