#!/usr/bin/env bash
# Both installation modes ship the same files; runtime setup waits for startup
# during image builds. All file writes stay within the fixture directories.
# CLAUDE_SANDBOX_IMPL=python runs it against the Python installer, where the
# runtime setup is observed by its effect (the shared-config links) rather
# than by stubbing bash functions.
set -euo pipefail
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
source "$REPO_ROOT/tests/lib.sh"
tmp="$(mktemp -d)"
register_cleanup "$tmp"
IMPL="${CLAUDE_SANDBOX_IMPL:-bash}"
export CLAUDE_SANDBOX_SMOKE_PYTHON="${CLAUDE_SANDBOX_SMOKE_PYTHON:-$(command -v python3)}"
run_mode() (
    local mode="$1"; shift
    export CLAUDE_SANDBOX_SMOKE=1 INSTALL_PREFIX="$tmp/$mode"
    export INSTALL_USER_HOME="$tmp/$mode-home"
    mkdir -p "$INSTALL_USER_HOME/.claude"
    if [ "$IMPL" = python ]; then
        export HOME="$tmp/$mode-linkhome" CLAUDE_SHARED_CONFIG="$tmp/$mode-shared"
        mkdir -p "$HOME" "$CLAUDE_SHARED_CONFIG"
        bash "$REPO_ROOT/.devcontainer/claude-sandbox/install.sh" "$@" > "$tmp/$mode.log" 2>&1
        [ ! -L "$HOME/.claude" ] || echo share >> "$tmp/$mode.calls"
        return 0
    fi
    source "$REPO_ROOT/.devcontainer/claude-sandbox/install.sh"
    probe_userns_or_refuse() { echo probe >> "$tmp/$mode.calls"; }
    link_terminal_config() { echo share >> "$tmp/$mode.calls"; }
    main "$@" > "$tmp/$mode.log" 2>&1
)
run_mode container
run_mode image --image-build
if [ "$IMPL" = python ]; then
    assert_eq 'normal installation performs runtime setup' share "$(cat "$tmp/container.calls")"
else
    assert_eq 'normal installation performs runtime setup' $'probe\nshare' "$(cat "$tmp/container.calls")"
fi
if [ -e "$tmp/image.calls" ]; then
    fail 'image build performed runtime setup'
else
    pass
fi
assert_parse 'both modes install the same files' diff -r "$tmp/container" "$tmp/image"
assert_parse 'both modes seed the same user settings' diff -r "$tmp/container-home" "$tmp/image-home"
finish install_modes
