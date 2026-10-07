#!/usr/bin/env bash
# Both installation modes ship the same files; runtime setup (the
# shared-config links, observed by their effect) waits for startup during
# image builds. All file writes stay within the fixture directories.
set -euo pipefail
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
source "$REPO_ROOT/tests/lib.sh"
tmp="$(mktemp -d)"
register_cleanup "$tmp"
export CLAUDE_SANDBOX_SMOKE_PYTHON="${CLAUDE_SANDBOX_SMOKE_PYTHON:-$(command -v python3)}"
run_mode() (
    local mode="$1"; shift
    export CLAUDE_SANDBOX_SMOKE=1 INSTALL_PREFIX="$tmp/$mode"
    export INSTALL_USER_HOME="$tmp/$mode-home"
    export HOME="$tmp/$mode-linkhome" CLAUDE_SHARED_CONFIG="$tmp/$mode-shared"
    mkdir -p "$INSTALL_USER_HOME/.claude" "$HOME" "$CLAUDE_SHARED_CONFIG"
    bash "$REPO_ROOT/.devcontainer/claude-sandbox/install.sh" "$@" > "$tmp/$mode.log" 2>&1
    [ ! -L "$HOME/.claude" ] || echo share >> "$tmp/$mode.calls"
)
run_mode container
run_mode image --image-build
assert_eq 'normal installation performs runtime setup' share "$(cat "$tmp/container.calls")"
if [ -e "$tmp/image.calls" ]; then
    fail 'image build performed runtime setup'
else
    pass
fi
assert_parse 'both modes install the same files' diff -r "$tmp/container" "$tmp/image"
assert_parse 'both modes seed the same user settings' diff -r "$tmp/container-home" "$tmp/image-home"
finish install_modes
