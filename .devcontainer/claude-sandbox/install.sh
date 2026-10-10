#!/usr/bin/env bash
# claude-sandbox installer bootstrap (ADR 26). The installer is Python
# (src/claude_sandbox/installer); this bash only gets it running. It
# fetches a pinned uv (checked against a pinned SHA-256), has uv install the
# pinned CPython straight under /usr/libexec/claude-sandbox (never uv's
# cache, which the jail can write), lets provision.py prune it and build the
# root-owned venv, and execs the installer from that venv with -I.
# Idempotent: re-runs after a devcontainer rebuild re-establish container
# state without disturbing workspace edits.
#
# The installer reads the rest of its settings from the environment:
#   INSTALL_PREFIX    (default /)     — root of installed files
#   INSTALL_USER_HOME (default $HOME) — user settings and credential dirs
#   CLAUDE_SANDBOX_SMOKE=1            skip apt, the agent downloads and,
#                                    here, the interpreter: the installer
#                                    runs from the tree under the
#                                    interpreter named by
#                                    CLAUDE_SANDBOX_SMOKE_PYTHON.
#   WITH_CODEX=0, WITH_PI=0           skip fetching OpenAI's Codex CLI or Pi;
#                                    their shadows are installed either way
#                                    (Invariant 1).
#   CLAUDE_SANDBOX_MINIMAL=1          the install shim's --minimal: Claude
#                                    only. Implies WITH_CODEX=0 WITH_PI=0,
#                                    leaves out Codex's and Pi's other files
#                                    while they are not installed, and nodejs,
#                                    and skips apt when nothing is missing.
#   PI_VERSION=0.85.1                optional Pi release pin.
#   STATUS=1                         force-overwrite the user-scope
#                                    statusline script.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# REPO_ROOT is the clone — two levels above .devcontainer/claude-sandbox —
# or the wheel's bundled tree (claude_sandbox/tree).
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
PREFIX="${INSTALL_PREFIX:-/}"
SMOKE="${CLAUDE_SANDBOX_SMOKE:-0}"
LIBEXEC="/usr/libexec/claude-sandbox"
PY_DIR="$LIBEXEC/python"
PY_VENV="$LIBEXEC/venv"
UV_DIR="$LIBEXEC/uv"

# Resolve a target under $PREFIX.
prefixed() {
    if [ "$PREFIX" = "/" ]; then
        printf '%s\n' "$1"
    else
        printf '%s\n' "${PREFIX%/}$1"
    fi
}

# python_package: the claude_sandbox package being installed — a clone's
# src/, or the installed wheel this tree is bundled in (claude_sandbox/tree).
python_package() {
    if [ -f "$REPO_ROOT/src/claude_sandbox/installer/__main__.py" ]; then
        printf '%s\n' "$REPO_ROOT/src/claude_sandbox"
    elif [ "${REPO_ROOT##*/}" = tree ] && [ -f "$REPO_ROOT/../installer/__main__.py" ]; then
        (cd "$REPO_ROOT/.." && pwd)
    else
        echo "claude-sandbox: cannot find the claude_sandbox package beside $REPO_ROOT." >&2
        exit 1
    fi
}

# python_pin PACKAGE NAME: a pin from provision.py, the one place they live.
python_pin() {
    sed -n "s/^$2 = \"\([^\"]*\)\"\$/\1/p" "$1/installer/provision.py"
}

# fetch_uv PACKAGE: print the path of a root-owned uv at the pinned version,
# at a fixed place under /usr/libexec, downloading the release (checked
# against the pinned SHA-256) when it is absent, stale or not root's.
fetch_uv() {
    local pkg="$1" dir uv want arch sum tmp
    dir="$(prefixed "$UV_DIR")"
    uv="$dir/uv"
    want="$(python_pin "$pkg" UV_VERSION)"
    if [ -f "$uv" ] && [ -x "$uv" ] && [ -z "$(find "$uv" -perm /022)" ] \
            && { [ "$(id -u)" != 0 ] || [ "$(stat -c %u "$uv")" = 0 ]; } \
            && [ "$("$uv" --version 2>/dev/null | awk '{print $2}')" = "$want" ]; then
        printf '%s\n' "$uv"
        return 0
    fi
    case "$(uname -m)" in
        x86_64) arch=x86_64; sum="$(python_pin "$pkg" UV_SHA256_X86_64)" ;;
        aarch64|arm64) arch=aarch64; sum="$(python_pin "$pkg" UV_SHA256_AARCH64)" ;;
        *) echo "claude-sandbox: supports x86_64 and aarch64 only." >&2; exit 1 ;;
    esac
    if ! command -v curl >/dev/null 2>&1; then
        DEBIAN_FRONTEND=noninteractive apt-get update -qq >&2
        DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends \
            curl ca-certificates >&2
    fi
    tmp="$(mktemp -d)"
    if ! curl -fsSL --retry 6 -o "$tmp/uv.tar.gz" \
            "https://github.com/astral-sh/uv/releases/download/$want/uv-$arch-unknown-linux-gnu.tar.gz" \
            || ! printf '%s  %s\n' "$sum" "$tmp/uv.tar.gz" | sha256sum -c --quiet - >&2 \
            || ! tar -xzf "$tmp/uv.tar.gz" --no-same-owner -C "$tmp"; then
        rm -rf "$tmp"
        echo "claude-sandbox: could not fetch and verify uv $want." >&2
        exit 1
    fi
    install -d -m 0755 "$dir"
    install -m 0755 "$tmp/uv-$arch-unknown-linux-gnu/uv" "$uv"
    rm -rf "$tmp"
    printf '%s\n' "$uv"
}

main() {
    local pkg uv interp py_dir want var
    local -a scrub=()
    case "$*" in
        ''|--image-build) ;;
        *) echo 'Usage: install.sh [--image-build]' >&2; exit 2 ;;
    esac
    # ADR 26: nothing found through the caller's PATH; the Python installer
    # exec'd below inherits it. The same directories as tools.TOOL_PATH.
    export PATH=/usr/bin:/bin:/usr/sbin:/sbin
    if [ "$SMOKE" != 1 ] && ! command -v apt-get >/dev/null 2>&1; then
        echo "claude-sandbox: refusing — Debian/Ubuntu only (no apt-get on PATH)." >&2
        exit 1
    fi
    pkg="$(python_package)"
    if [ "$SMOKE" = 1 ]; then
        if [ -z "${CLAUDE_SANDBOX_SMOKE_PYTHON:-}" ]; then
            echo "claude-sandbox: a smoke run needs CLAUDE_SANDBOX_SMOKE_PYTHON." >&2
            exit 1
        fi
        exec "$CLAUDE_SANDBOX_SMOKE_PYTHON" -I -c \
            'import runpy, sys; sys.path.insert(0, sys.argv.pop(1)); sys.argv[0] = "installer"; runpy.run_module("claude_sandbox.installer", run_name="__main__")' \
            "${pkg%/*}" --source "$REPO_ROOT" "$@"
    fi
    uv="$(fetch_uv "$pkg")"
    py_dir="$(prefixed "$PY_DIR")"
    want="$(python_pin "$pkg" PYTHON_VERSION)"
    # Nothing in the environment may steer uv or the interpreter.
    for var in $(compgen -e); do
        case "$var" in UV_*|PYTHON*|VIRTUAL_ENV|CONDA_*) scrub+=(-u "$var") ;; esac
    done
    # The interpreter that runs provisioning is the pinned one, installed
    # straight under /usr/libexec (never uv's cache); provisioning then
    # prunes it, builds the venv and checks both.
    env "${scrub[@]}" UV_PYTHON_INSTALL_DIR="$py_dir" UV_NO_CACHE=1 \
        "$uv" python install --no-config --no-bin --quiet "$want"
    interp="$(env "${scrub[@]}" UV_PYTHON_INSTALL_DIR="$py_dir" UV_NO_CACHE=1 \
        "$uv" python find --no-config --no-project --managed-python "$want")"
    interp="$(readlink -f "$interp")"
    case "$interp" in
        "$py_dir"/*) ;;
        *) echo "claude-sandbox: uv found $interp, not an interpreter under $py_dir; refusing." >&2; exit 1 ;;
    esac
    (cd / && env "${scrub[@]}" "$interp" -I -c \
        'import sys; sys.path.insert(0, sys.argv.pop(1)); from claude_sandbox.installer.provision import main; main(sys.argv[1:])' \
        "${pkg%/*}" "$pkg" --uv "$uv" --root "$(prefixed "$LIBEXEC")")
    exec env "${scrub[@]}" "$(prefixed "$PY_VENV")/bin/python" -I -m claude_sandbox.installer \
        --source "$REPO_ROOT" "$@"
}

# test_provision.py sources this file to read the pins as main() does.
if [ "${BASH_SOURCE[0]}" = "$0" ]; then
    main "$@"
fi
