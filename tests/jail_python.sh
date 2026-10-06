#!/usr/bin/env bash
# The Python egress jail (src/claude_sandbox/jail.py) against real
# namespaces, pasta and socat: runs tests/python/test_jail_netns.py with a
# skip treated as a failure. Run it INSIDE this repository's image, started
# as .github/workflows/container.yml starts it, e.g.
#
#   podman build --target claude-sandbox -t claude-sandbox:test .
#   podman run --rm -t --security-opt seccomp=unconfined \
#       --security-opt apparmor=unconfined --security-opt label=disable \
#       --device /dev/net/tun -v "$PWD:/src:ro" --entrypoint bash \
#       claude-sandbox:test /src/tests/jail_python.sh
#
# pytest comes from a throwaway venv; the package itself is copied into a
# second venv by the test, so it runs under `python -I` as installed.
set -euo pipefail
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
uv venv -q --python 3.13 "$work/venv"
uv pip install -q --python "$work/venv/bin/python" pytest
cd "$REPO_ROOT"
JAIL_NETNS_REQUIRE=1 "$work/venv/bin/python" -m pytest -p no:cacheprovider \
    -o addopts= -q -rA --color=no "$@" tests/python/test_jail_netns.py
