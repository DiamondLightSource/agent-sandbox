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

# --minimal (#101), through the install shim as `uvx claude-sandbox install
# --minimal` reaches it: Claude only, and a later full install on top of it
# leaves exactly what a full install does.
run_minimal() (
    export CLAUDE_SANDBOX_SMOKE=1 INSTALL_PREFIX="$tmp/minimal"
    export INSTALL_USER_HOME="$tmp/minimal-home"
    export HOME="$tmp/minimal-linkhome" CLAUDE_SHARED_CONFIG="$tmp/minimal-shared"
    mkdir -p "$INSTALL_USER_HOME/.claude" "$HOME" "$CLAUDE_SHARED_CONFIG"
    bash "$REPO_ROOT/install" "$@" > "$tmp/minimal.log" 2>&1
)
run_minimal --minimal
lib="$tmp/minimal/usr/libexec/claude-sandbox"
for name in claude codex pi; do
    assert_parse "minimal: the $name shadow is placed" \
        cmp "$REPO_ROOT/.devcontainer/claude-sandbox/claude-shim" "$tmp/minimal/usr/local/bin/$name"
done
for gone in "$lib/codex-launch" "$lib/pi-run" "$lib/pi-sandbox-tag.ts" \
        "$tmp/minimal/etc/codex" "$tmp/minimal-home/.codex" "$tmp/minimal-home/.pi" \
        "$tmp/minimal-linkhome/.codex" "$tmp/minimal-linkhome/.pi"; do
    if [ -e "$gone" ] || [ -L "$gone" ]; then
        fail "minimal installed $gone"
    else
        pass
    fi
done
assert_parse 'minimal: same managed settings' \
    cmp "$tmp/container/etc/claude-code/managed-settings.json" "$tmp/minimal/etc/claude-code/managed-settings.json"
assert_parse 'minimal: same battery' \
    cmp "$tmp/container/usr/libexec/claude-sandbox/verify-sandbox-battery.sh" "$lib/verify-sandbox-battery.sh"
assert_parse 'minimal: summary names it' grep -qF 'not installed (--minimal)' "$tmp/minimal.log"
run_minimal
assert_parse 'a full install over a minimal one ships the same files' diff -r "$tmp/container" "$tmp/minimal"
assert_parse 'and seeds the same user settings' diff -r "$tmp/container-home" "$tmp/minimal-home"
finish install_modes
