#!/usr/bin/env bash
# Bash unit test for the bwrap_argv_build function defined inline in
# .devcontainer/claude-sandbox/claude-shadow. The shadow exposes a
# CLAUDE_SHADOW_SOURCE_ONLY=1 guard so we can source the function
# definitions without running the launch body.
#
# We assert on the argv as a contract with bwrap (line-equality grep),
# not on the internal control flow that built it. Failures print a
# diff-friendly diagnostic.
#
# Run via `bash tests/bwrap_argv.sh`.

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SHADOW="$REPO_ROOT/.devcontainer/claude-sandbox/claude-shadow"

if [ ! -f "$SHADOW" ]; then
    echo "FAIL: cannot find $SHADOW" >&2
    exit 1
fi

# Shared assertions + PASS/FAIL counters + register_cleanup.
# shellcheck source=lib.sh
source "$REPO_ROOT/tests/lib.sh"

# Pull bwrap_argv_build into scope without running the shadow's launch
# body. The shadow returns early when CLAUDE_SHADOW_SOURCE_ONLY=1.
export CLAUDE_SHADOW_SOURCE_ONLY=1
# shellcheck source=../.devcontainer/claude-sandbox/claude-shadow
source "$SHADOW"

# Normalise the runner's env so the pure-function assertions are
# deterministic regardless of where the suite is run. The shadow passes
# VIRTUAL_ENV / UV_* / PRE_COMMIT_HOME through and appends $VIRTUAL_ENV/bin
# to PATH — running the suite from inside an activated venv (e.g. the
# dogfood devcontainer) would otherwise leak that into Scenario 1's
# canonical-PATH assertion. Scenarios that exercise these vars set them
# explicitly in their own subshell, so clearing them here is safe.
unset VIRTUAL_ENV UV_PROJECT_ENVIRONMENT UV_CACHE_DIR UV_PYTHON_CACHE_DIR \
      UV_PYTHON_INSTALL_DIR UV_TOOL_DIR \
      PRE_COMMIT_HOME CLAUDE_SANDBOX_WORKSPACE_ROOT CLAUDE_SANDBOX_NO_FORGE \
      CLAUDE_SANDBOX_ALLOW_WRITE CLAUDE_SANDBOX_EGRESS_JAIL CLAUDE_SANDBOX_ALLOW_IP \
      CLAUDE_SANDBOX_JAIL_RESOLV

# --- Scenario 1: vanilla (workspace=/workspaces/foo, $HOME=/root) ---
unset TERM LANG LC_ALL LC_CTYPE LC_MESSAGES LC_TIME LC_COLLATE LC_NUMERIC LC_MONETARY
ARGV1="$(HOME=/root CLAUDE_SANDBOX_GITCONFIG_PATH=/etc/claude-gitconfig \
    bwrap_argv_lines /workspaces/foo /test/.local/bin/claude)"

assert_contains scenario1 "$ARGV1" "bwrap"
assert_contains scenario1 "$ARGV1" "--ro-bind"
assert_contains scenario1 "$ARGV1" "--dev"
assert_contains scenario1 "$ARGV1" "/dev"
# Unconditional --ro-bind /proc /proc.
assert_pair scenario1 "$ARGV1" "--ro-bind" "/proc"
assert_contains scenario1 "$ARGV1" "--cap-drop"
assert_contains scenario1 "$ARGV1" "ALL"
# All five unshare flags including --unshare-user-try.
assert_contains scenario1 "$ARGV1" "--unshare-user-try"
assert_contains scenario1 "$ARGV1" "--unshare-pid"
assert_contains scenario1 "$ARGV1" "--unshare-ipc"
assert_contains scenario1 "$ARGV1" "--unshare-uts"
assert_contains scenario1 "$ARGV1" "--unshare-cgroup-try"
assert_contains scenario1 "$ARGV1" "--die-with-parent"
# --new-session is DROPPED (delegated to script(1) wrap).
assert_not_contains scenario1 "$ARGV1" "--new-session"
# The shadow's procfs probe is gone — no --proc primitive emission.
assert_not_contains scenario1 "$ARGV1" "--proc"
# Env scrub.
assert_contains scenario1 "$ARGV1" "--clearenv"
assert_contains scenario1 "$ARGV1" "PATH"
assert_contains scenario1 "$ARGV1" "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:/root/.local/bin"
assert_contains scenario1 "$ARGV1" "HOME"
assert_contains scenario1 "$ARGV1" "/root"
assert_contains scenario1 "$ARGV1" "USER"
assert_contains scenario1 "$ARGV1" "root"
assert_contains scenario1 "$ARGV1" "IS_SANDBOX"
assert_pair scenario1 "$ARGV1" "IS_SANDBOX_AGENT" "claude"
assert_contains scenario1 "$ARGV1" "GIT_CONFIG_GLOBAL"
assert_contains scenario1 "$ARGV1" "/etc/claude-gitconfig"
assert_contains scenario1 "$ARGV1" "GIT_CONFIG_SYSTEM"
assert_contains scenario1 "$ARGV1" "/dev/null"
# Final terminator and real claude path. The real binary is bound
# from its off-PATH host location to the conventional in-sandbox
# ~/.local/bin/claude, and bwrap execs the in-sandbox path.
assert_contains scenario1 "$ARGV1" "--"
assert_contains scenario1 "$ARGV1" "/test/.local/bin/claude"
assert_pair scenario1 "$ARGV1" "--bind" "/test/.local/bin/claude"
assert_contains scenario1 "$ARGV1" "/root/.local/bin/claude"
# /run/secrets mask only when the host has it.
if [ -d /run/secrets ]; then
    assert_contains scenario1 "$ARGV1" "/run/secrets"
else
    assert_not_contains scenario1 "$ARGV1" "/run/secrets"
fi

# --- Scenario 2: workspace empty string → no workspace bind line ---
ARGV2="$(HOME=/root CLAUDE_SANDBOX_GITCONFIG_PATH=/etc/claude-gitconfig \
    bwrap_argv_lines "" /test/.local/bin/claude)"
# No bind for an empty workspace. The argv is otherwise intact.
assert_contains scenario2 "$ARGV2" "bwrap"
assert_contains scenario2 "$ARGV2" "--clearenv"
# Nothing that looks like a path bind for /tmp/... or /workspaces/... should appear.
# We can't enumerate all possible non-emissions, but workspace=""
# means the workspace bind branch is skipped.

# --- Scenario 3: workspace at an unusual non-existent path ---
ARGV3="$(HOME=/root CLAUDE_SANDBOX_GITCONFIG_PATH=/etc/claude-gitconfig \
    bwrap_argv_lines /srv/weird-workspace-path /test/.local/bin/claude)"
assert_not_contains scenario3 "$ARGV3" "/srv/weird-workspace-path"

# --- Scenario 4: bind-back loop over $HOME (tmpdir-based fixture) ---
TMPHOME="$(mktemp -d)"
register_cleanup "$TMPHOME"
mkdir -p "$TMPHOME/.claude" "$TMPHOME/.config/gh"

ARGV4a="$(HOME="$TMPHOME" CLAUDE_SANDBOX_GITCONFIG_PATH=/etc/claude-gitconfig \
    bwrap_argv_lines "$TMPHOME" /test/.local/bin/claude)"
assert_contains scenario4a "$ARGV4a" "$TMPHOME/.claude"
assert_contains scenario4a "$ARGV4a" "$TMPHOME/.config/gh"
# Absent paths must NOT appear.
assert_not_contains scenario4a "$ARGV4a" "$TMPHOME/.cache"
assert_not_contains scenario4a "$ARGV4a" "$TMPHOME/.config/glab-cli"
assert_not_contains scenario4a "$ARGV4a" "$TMPHOME/.local/share"
assert_not_contains scenario4a "$ARGV4a" "$TMPHOME/.local/share/applications"
assert_not_contains scenario4a "$ARGV4a" "$TMPHOME/.local/share/claude"
assert_not_contains scenario4a "$ARGV4a" "$TMPHOME/.claude.json"
assert_not_contains scenario4a "$ARGV4a" "$TMPHOME/.local/bin/uv"
assert_not_contains scenario4a "$ARGV4a" "$TMPHOME/.local/bin/uvx"
# Note: $TMPHOME/.local/bin/claude IS expected — it's the unconditional
# bind dest for the off-PATH real binary, regardless of $HOME state.
assert_contains scenario4a "$ARGV4a" "$TMPHOME/.local/bin/claude"

# Now populate the full set and re-check.
mkdir -p "$TMPHOME/.cache" "$TMPHOME/.config/glab-cli" "$TMPHOME/.local/share/helm" "$TMPHOME/.local/bin"
touch "$TMPHOME/.claude.json" "$TMPHOME/.local/bin/uv" "$TMPHOME/.local/bin/uvx" "$TMPHOME/.local/bin/claude"
# A sibling under .config (e.g. VS Code) must NOT be bound even when
# present — only the explicit allowlist is exposed.
mkdir -p "$TMPHOME/.config/Code"

ARGV4b="$(HOME="$TMPHOME" CLAUDE_SANDBOX_GITCONFIG_PATH=/etc/claude-gitconfig \
    bwrap_argv_lines "$TMPHOME" /test/.local/bin/claude)"
assert_contains scenario4b "$ARGV4b" "$TMPHOME/.claude"
assert_contains scenario4b "$ARGV4b" "$TMPHOME/.cache"
assert_contains scenario4b "$ARGV4b" "$TMPHOME/.config/gh"
assert_contains scenario4b "$ARGV4b" "$TMPHOME/.config/glab-cli"
# .local/share is bulk-bound, with applications/ + claude/ tmpfs-masked.
assert_contains scenario4b "$ARGV4b" "$TMPHOME/.local/share"
assert_contains scenario4b "$ARGV4b" "$TMPHOME/.local/share/applications"
assert_contains scenario4b "$ARGV4b" "$TMPHOME/.local/share/claude"
assert_contains scenario4b "$ARGV4b" "$TMPHOME/.claude.json"
assert_contains scenario4b "$ARGV4b" "$TMPHOME/.local/bin/uv"
assert_contains scenario4b "$ARGV4b" "$TMPHOME/.local/bin/uvx"
assert_contains scenario4b "$ARGV4b" "$TMPHOME/.local/bin/claude"
assert_not_contains scenario4b "$ARGV4b" "$TMPHOME/.config/Code"
# Workspace ($TMPHOME exists) IS bound.
# (--bind <src> <dst> emits the path twice; existence is enough.)

# --- Scenario 5: pass-through env (TERM, LANG) appear as --setenv pairs ---
ARGV5="$(HOME=/root CLAUDE_SANDBOX_GITCONFIG_PATH=/etc/claude-gitconfig \
    TERM=xterm-256color LANG=en_US.UTF-8 \
    bwrap_argv_lines /workspaces/foo /test/.local/bin/claude)"
assert_contains scenario5 "$ARGV5" "TERM"
assert_contains scenario5 "$ARGV5" "xterm-256color"
assert_contains scenario5 "$ARGV5" "LANG"
assert_contains scenario5 "$ARGV5" "en_US.UTF-8"

# --- Scenario 6: defence-in-depth masks at $HOME ---
assert_contains scenario6 "$ARGV1" "/root/.netrc"
assert_contains scenario6 "$ARGV1" "/root/.Xauthority"
assert_contains scenario6 "$ARGV1" "/root/.ICEauthority"

# --- Scenario 7: chrome browser-extension disable ---
# Every launch must inject --no-chrome immediately after the -- terminator,
# and any user-supplied --chrome must be stripped so it can't override
# the injection. The browser-extension native-messaging-host RPC channel
# is outside the threat model.
assert_pair scenario7-default "$ARGV1" "/root/.local/bin/claude" "--no-chrome"

# User passes --chrome — it must be filtered out, --no-chrome stays.
ARGV7="$(HOME=/root CLAUDE_SANDBOX_GITCONFIG_PATH=/etc/claude-gitconfig \
    bwrap_argv_lines /workspaces/foo /test/.local/bin/claude --chrome --version)"
assert_contains scenario7-strip "$ARGV7" "--no-chrome"
assert_not_contains scenario7-strip "$ARGV7" "--chrome"
# User's legit args still pass through.
assert_contains scenario7-strip "$ARGV7" "--version"

# --- Scenario 8: resolve_workspace_root ---
# Pure function: priority is env override > /workspaces auto-detect > $PWD.
unset CLAUDE_SANDBOX_WORKSPACE_ROOT

# Default: $PWD — no auto-promotion to /workspaces.
assert_eq scenario8-pwd-direct "/workspaces/claude-sandbox2" \
    "$(resolve_workspace_root /workspaces/claude-sandbox2)"

assert_eq scenario8-pwd-nested "/workspaces/claude-sandbox2/sub/deeper" \
    "$(resolve_workspace_root /workspaces/claude-sandbox2/sub/deeper)"

# $PWD outside /workspaces/ → $PWD itself.
assert_eq scenario8-fallback-tmp "/tmp/myproject" \
    "$(resolve_workspace_root /tmp/myproject)"

# $PWD is /workspaces itself → /workspaces (coincidentally same as $PWD).
assert_eq scenario8-edge-bare "/workspaces" \
    "$(resolve_workspace_root /workspaces)"

# Override: CLAUDE_SANDBOX_WORKSPACE_ROOT wins regardless of $PWD.
assert_eq scenario8-override-custom "/srv/custom" \
    "$(CLAUDE_SANDBOX_WORKSPACE_ROOT=/srv/custom resolve_workspace_root /workspaces/foo)"

assert_eq scenario8-override-from-tmp "/srv/custom" \
    "$(CLAUDE_SANDBOX_WORKSPACE_ROOT=/srv/custom resolve_workspace_root /tmp/bar)"

# Migration knob: set to /workspaces to restore the old broad bind.
assert_eq scenario8-restore-broad "/workspaces" \
    "$(CLAUDE_SANDBOX_WORKSPACE_ROOT=/workspaces resolve_workspace_root /workspaces/foo)"

# Empty override treated as unset — falls back to $PWD.
assert_eq scenario8-empty-override "/workspaces/foo" \
    "$(CLAUDE_SANDBOX_WORKSPACE_ROOT= resolve_workspace_root /workspaces/foo)"

assert_eq scenario8-empty-override-fallback "/tmp/bar" \
    "$(CLAUDE_SANDBOX_WORKSPACE_ROOT= resolve_workspace_root /tmp/bar)"

# --- Scenario 8b: VIRTUAL_ENV/bin APPENDED to PATH (never prepended) ---
# A venv binary must not be able to shadow `claude` or a system tool
# (Invariant 1), and VIRTUAL_ENV itself is passed through into the
# sandbox. Guards the deliberate uv/venv passthrough.
mkdir -p "$TMPHOME/venv/bin"
ARGV8B="$(HOME="$TMPHOME" VIRTUAL_ENV="$TMPHOME/venv" \
    CLAUDE_SANDBOX_GITCONFIG_PATH=/etc/claude-gitconfig \
    bwrap_argv_lines "$TMPHOME" /test/.local/bin/claude)"
assert_contains scenario8b-venv-appended "$ARGV8B" \
    "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:$TMPHOME/.local/bin:$TMPHOME/venv/bin"
assert_pair scenario8b-venv-passthrough "$ARGV8B" '--setenv' 'VIRTUAL_ENV'
# The container image (Dockerfile, claude-sandbox stage) relies on these two
# reaching the jail: without them uv re-downloads the baked interpreter and
# re-installs tools into the ephemeral home every session.
ARGV8C="$(HOME="$TMPHOME" UV_PYTHON_INSTALL_DIR=/opt/uv/python UV_TOOL_DIR=/cache/uv-tools \
    CLAUDE_SANDBOX_GITCONFIG_PATH=/etc/claude-gitconfig \
    bwrap_argv_lines "$TMPHOME" /test/.local/bin/claude)"
assert_pair scenario8c-uv-python-dir "$ARGV8C" '--setenv' 'UV_PYTHON_INSTALL_DIR'
assert_pair scenario8c-uv-tool-dir "$ARGV8C" '--setenv' 'UV_TOOL_DIR'

# --- Scenario 9: CLAUDE_SANDBOX_NO_FORGE=1 omits forge token dirs ---
# $TMPHOME/.config/gh and glab-cli were created in scenario 4b.
ARGV9="$(HOME="$TMPHOME" CLAUDE_SANDBOX_GITCONFIG_PATH=/etc/claude-gitconfig \
    CLAUDE_SANDBOX_NO_FORGE=1 \
    bwrap_argv_lines "$TMPHOME" /test/.local/bin/claude)"
assert_not_contains scenario9-no-gh   "$ARGV9" "$TMPHOME/.config/gh"
assert_not_contains scenario9-no-glab "$ARGV9" "$TMPHOME/.config/glab-cli"
assert_contains     scenario9-claude  "$ARGV9" "$TMPHOME/.claude"
assert_contains     scenario9-cache   "$ARGV9" "$TMPHOME/.cache"

# --- Scenario 10: CLAUDE_SANDBOX_ALLOW_WRITE adds extra rw bind ---
mkdir -p "$TMPHOME/extra-rw" "$TMPHOME/extra-rw2"
ARGV10a="$(HOME="$TMPHOME" CLAUDE_SANDBOX_ALLOW_WRITE="$TMPHOME/extra-rw" \
    CLAUDE_SANDBOX_GITCONFIG_PATH=/etc/claude-gitconfig \
    bwrap_argv_lines "$TMPHOME" /test/.local/bin/claude)"
assert_contains scenario10-single "$ARGV10a" "$TMPHOME/extra-rw"

ARGV10b="$(HOME="$TMPHOME" \
    CLAUDE_SANDBOX_ALLOW_WRITE="$TMPHOME/extra-rw
$TMPHOME/extra-rw2" \
    CLAUDE_SANDBOX_GITCONFIG_PATH=/etc/claude-gitconfig \
    bwrap_argv_lines "$TMPHOME" /test/.local/bin/claude)"
assert_contains scenario10-multi-a "$ARGV10b" "$TMPHOME/extra-rw"
assert_contains scenario10-multi-b "$ARGV10b" "$TMPHOME/extra-rw2"

# Non-existent allow-write path must NOT appear (silently skipped).
ARGV10c="$(HOME="$TMPHOME" CLAUDE_SANDBOX_ALLOW_WRITE="/nonexistent/path" \
    CLAUDE_SANDBOX_GITCONFIG_PATH=/etc/claude-gitconfig \
    bwrap_argv_lines "$TMPHOME" /test/.local/bin/claude)"
assert_not_contains scenario10-absent "$ARGV10c" "/nonexistent/path"

# Non-regular files must be bound, not silently dropped. The guard was
# `[ -d ] || [ -f ]`, and -f is true only for regular files — so a unix
# socket failed both tests and `allow-write = <sock>` emitted nothing.
# Rootless podman/docker expose their engine as a socket under
# $XDG_RUNTIME_DIR, which is exactly what allow-write is needed for.
mkfifo "$TMPHOME/extra.fifo"
ARGV10d="$(HOME="$TMPHOME" CLAUDE_SANDBOX_ALLOW_WRITE="$TMPHOME/extra.fifo" \
    CLAUDE_SANDBOX_GITCONFIG_PATH=/etc/claude-gitconfig \
    bwrap_argv_lines "$TMPHOME" /test/.local/bin/claude)"
assert_contains scenario10-fifo "$ARGV10d" "$TMPHOME/extra.fifo"

# The real thing, when the host has a perl to mint a socket with. Guarded
# rather than assumed: the suite is bash + coreutils by policy, so perl is
# a fixture convenience here, never a runtime dependency of the sandbox.
if command -v perl >/dev/null 2>&1 && perl -e 'use IO::Socket::UNIX;
    IO::Socket::UNIX->new(Type => SOCK_STREAM(), Local => $ARGV[0], Listen => 1)
        or exit 1' "$TMPHOME/podman.sock" 2>/dev/null; then
    ARGV10e="$(HOME="$TMPHOME" CLAUDE_SANDBOX_ALLOW_WRITE="$TMPHOME/podman.sock" \
        CLAUDE_SANDBOX_GITCONFIG_PATH=/etc/claude-gitconfig \
        bwrap_argv_lines "$TMPHOME" /test/.local/bin/claude)"
    assert_contains scenario10-socket "$ARGV10e" "$TMPHOME/podman.sock"
else
    echo "note: scenario10-socket skipped (no perl to create a unix socket)" >&2
fi

# Mask-then-bind ordering is load-bearing, not incidental: bwrap applies
# operations in argv sequence, so allow-write must be emitted after the
# tmpfs masks for a bind to re-expose a single path through one (e.g. the
# podman socket under a masked /run/user). Hoisting allow-write above the
# masks would let a mask clobber the operator's bind silently, so lock the
# order. The /run masks are host-conditional; the $HOME ones are not, so
# assert against the last tmpfs the builder emits either way.
assert_order scenario10-mask-before-bind "$ARGV10d" "--tmpfs" "$TMPHOME/extra.fifo"

# --- Scenario 10e: CLAUDE_SANDBOX_PASS_ENV forwards named vars ---
# Values come from the launching env, never from the name list.
ARGV10p="$(HOME="$TMPHOME" CLAUDE_SANDBOX_PASS_ENV="DOCKER_HOST" \
    DOCKER_HOST="unix:///run/user/1000/podman/podman.sock" \
    CLAUDE_SANDBOX_GITCONFIG_PATH=/etc/claude-gitconfig \
    bwrap_argv_lines "$TMPHOME" /test/.local/bin/claude)"
assert_contains scenario10p-name "$ARGV10p" "DOCKER_HOST"
assert_contains scenario10p-value "$ARGV10p" "unix:///run/user/1000/podman/podman.sock"
assert_pair scenario10p-pair "$ARGV10p" "--setenv" "DOCKER_HOST"

# Comma-, space- and newline-separated name lists all split.
ARGV10q="$(HOME="$TMPHOME" CLAUDE_SANDBOX_PASS_ENV="FOO_A, FOO_B
FOO_C" FOO_A=a FOO_B=b FOO_C=c \
    CLAUDE_SANDBOX_GITCONFIG_PATH=/etc/claude-gitconfig \
    bwrap_argv_lines "$TMPHOME" /test/.local/bin/claude)"
assert_contains scenario10q-comma "$ARGV10q" "FOO_A"
assert_contains scenario10q-space "$ARGV10q" "FOO_B"
assert_contains scenario10q-newline "$ARGV10q" "FOO_C"

# A named-but-unset var emits nothing (no empty --setenv).
ARGV10r="$(HOME="$TMPHOME" CLAUDE_SANDBOX_PASS_ENV="DEFINITELY_UNSET_VAR" \
    CLAUDE_SANDBOX_GITCONFIG_PATH=/etc/claude-gitconfig \
    bwrap_argv_lines "$TMPHOME" /test/.local/bin/claude)"
assert_not_contains scenario10r-unset "$ARGV10r" "DEFINITELY_UNSET_VAR"

# Deny-list: pass-env must never override what the sandbox sets itself,
# nor the loader/shell hooks. The sandbox's own value has to survive.
ARGV10s="$(HOME="$TMPHOME" \
    CLAUDE_SANDBOX_PASS_ENV="PATH,HOME,USER,IS_SANDBOX,GIT_CONFIG_GLOBAL,GIT_CONFIG_SYSTEM,LD_PRELOAD,BASH_ENV,SHELLOPTS" \
    PATH="/evil/bin" IS_SANDBOX="0" LD_PRELOAD="/evil/x.so" BASH_ENV="/evil/rc" \
    GIT_CONFIG_SYSTEM="/evil/gitconfig" \
    CLAUDE_SANDBOX_GITCONFIG_PATH=/etc/claude-gitconfig \
    bwrap_argv_lines "$TMPHOME" /test/.local/bin/claude)"
assert_not_contains scenario10s-path "$ARGV10s" "/evil/bin"
assert_not_contains scenario10s-preload "$ARGV10s" "LD_PRELOAD"
assert_not_contains scenario10s-preload-val "$ARGV10s" "/evil/x.so"
assert_not_contains scenario10s-bashenv "$ARGV10s" "BASH_ENV"
assert_not_contains scenario10s-shellopts "$ARGV10s" "SHELLOPTS"
assert_not_contains scenario10s-gitconfig "$ARGV10s" "/evil/gitconfig"
# IS_SANDBOX=0 must not reach the sandbox — it would trip the shadow's
# recursion guard into skipping the jail on a nested spawn.
assert_not_contains scenario10s-is-sandbox "$ARGV10s" "0"
# The sandbox's own values survive the attempted override.
assert_contains scenario10s-real-path "$ARGV10s" "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:$TMPHOME/.local/bin"
assert_contains scenario10s-real-is-sandbox "$ARGV10s" "1"
assert_contains scenario10s-real-gitconfig "$ARGV10s" "/etc/claude-gitconfig"

# Junk names are skipped rather than emitted as a broken --setenv.
ARGV10t="$(HOME="$TMPHOME" CLAUDE_SANDBOX_PASS_ENV="9BAD,has-dash,has.dot,ok_name" \
    ok_name=fine \
    CLAUDE_SANDBOX_GITCONFIG_PATH=/etc/claude-gitconfig \
    bwrap_argv_lines "$TMPHOME" /test/.local/bin/claude)"
assert_not_contains scenario10t-leading-digit "$ARGV10t" "9BAD"
assert_not_contains scenario10t-dash "$ARGV10t" "has-dash"
assert_not_contains scenario10t-dot "$ARGV10t" "has.dot"
assert_contains scenario10t-valid "$ARGV10t" "ok_name"

# --- Scenario 11: parse_config (data-driven) ---
# Each case writes a conf fixture, then asserts a predicate in a FRESH
# shadow-sourced bash so parse_config's exports don't leak between cases.
# SHADOW/TMPCONF are exported so the case bodies can reference them; the
# bodies are single-quoted, so there is no \"…\" / \$… escaping (the old
# pain point). egress-jail defaults ON per ADR 0015.
TMPCONF="$(mktemp)"
register_cleanup "$TMPCONF"
export SHADOW TMPCONF

# parse_case NAME CONF BODY — write CONF (\n-expanded) to $TMPCONF, then assert
# BODY exits 0 in a fresh bash that has sourced the shadow.
parse_case() {
    local name="$1" conf="$2" body="$3"
    printf '%b' "$conf" > "$TMPCONF"
    assert_parse "$name" bash -c 'export CLAUDE_SHADOW_SOURCE_ONLY=1; . "$SHADOW"; '"$body"
}

parse_case scenario11-workspace-root 'workspace-root = /custom/root\n' '
    unset CLAUDE_SANDBOX_WORKSPACE_ROOT
    parse_config "$TMPCONF"
    [ "${CLAUDE_SANDBOX_WORKSPACE_ROOT:-}" = "/custom/root" ]
'

parse_case scenario11-no-forge 'no-forge\n' '
    unset CLAUDE_SANDBOX_NO_FORGE
    parse_config "$TMPCONF"
    [ "${CLAUDE_SANDBOX_NO_FORGE:-}" = "1" ]
'

parse_case devices-config 'gpu\nallow-device = /dev/null\nallow-device = /dev/zero\n' '
    unset CLAUDE_SANDBOX_GPU
    CLAUDE_SANDBOX_ALLOW_DEVICES=/dev/full
    parse_config "$TMPCONF"
    [ "$CLAUDE_SANDBOX_GPU" = 1 ] &&
    [ "$CLAUDE_SANDBOX_ALLOW_DEVICES" = "$(printf "%s\n" /dev/full /dev/null /dev/zero)" ]
'
parse_case gpu-env-wins 'gpu\n' '
    CLAUDE_SANDBOX_GPU=0
    parse_config "$TMPCONF"
    [ "$CLAUDE_SANDBOX_GPU" = 0 ]
'
device_argv="$(CLAUDE_SANDBOX_ALLOW_DEVICES=/dev/zero bwrap_argv_lines /repo /fake/claude)"
assert_pair 'device uses dev-bind' "$device_argv" --dev-bind /dev/zero
assert_order 'private dev before device bind' "$device_argv" --dev --dev-bind
for invalid in /dev /dev/pts /etc/passwd /dev/../etc/passwd /dev/no-such-claude-device; do
    if (CLAUDE_SANDBOX_ALLOW_DEVICES="$invalid" bwrap_argv_build rejected_args /repo /fake/claude) >/dev/null 2>&1; then
        fail "invalid sandbox device accepted: $invalid"
    else
        pass
    fi
done
gpu_argv="$(CLAUDE_SANDBOX_GPU=1 bwrap_argv_lines /repo /fake/claude)"
assert_pair 'GPU keeps private dev' "$gpu_argv" --dev /dev
assert_not_contains 'GPU does not expose tun' "$gpu_argv" /dev/net/tun
for device in /dev/nvidia* /dev/nvidia-caps/* /dev/dri/*; do
    [ -c "$device" ] || continue
    assert_pair 'available GPU device bound' "$gpu_argv" --dev-bind "$device"
done

parse_case scenario11-allow-write-single 'allow-write = /some/path\n' '
    unset CLAUDE_SANDBOX_ALLOW_WRITE
    parse_config "$TMPCONF"
    [ "${CLAUDE_SANDBOX_ALLOW_WRITE:-}" = "/some/path" ]
'

parse_case scenario11-allow-write-multi 'allow-write = /path/one\nallow-write = /path/two\n' '
    unset CLAUDE_SANDBOX_ALLOW_WRITE
    parse_config "$TMPCONF"
    printf "%s\n" "$CLAUDE_SANDBOX_ALLOW_WRITE" | grep -qxF "/path/one" &&
    printf "%s\n" "$CLAUDE_SANDBOX_ALLOW_WRITE" | grep -qxF "/path/two"
'

parse_case scenario11-pass-env-single 'pass-env = DOCKER_HOST\n' '
    unset CLAUDE_SANDBOX_PASS_ENV
    parse_config "$TMPCONF"
    [ "${CLAUDE_SANDBOX_PASS_ENV:-}" = "DOCKER_HOST" ]
'

# Repeated keys accumulate; a comma-separated value is passed through intact
# for the argv builder to split.
parse_case scenario11-pass-env-multi 'pass-env = A_VAR, B_VAR\npass-env = C_VAR\n' '
    unset CLAUDE_SANDBOX_PASS_ENV
    parse_config "$TMPCONF"
    printf "%s\n" "$CLAUDE_SANDBOX_PASS_ENV" | grep -qxF "A_VAR, B_VAR" &&
    printf "%s\n" "$CLAUDE_SANDBOX_PASS_ENV" | grep -qxF "C_VAR"
'

# egress-jail ON by default (ADR 0015): absent from conf + unset env => enabled
parse_case scenario11-egress-jail-default-on 'no-forge\n' '
    unset CLAUDE_SANDBOX_EGRESS_JAIL
    parse_config "$TMPCONF"
    egress_jail_enabled
'

# a bare `egress-jail` key reaffirms on
parse_case scenario11-egress-jail-bare 'egress-jail\n' '
    unset CLAUDE_SANDBOX_EGRESS_JAIL
    parse_config "$TMPCONF"
    [ "${CLAUDE_SANDBOX_EGRESS_JAIL:-}" = "1" ] && egress_jail_enabled
'

# `egress-jail = 0` in conf disables the jail
parse_case scenario11-egress-jail-conf-off 'egress-jail = 0\n' '
    unset CLAUDE_SANDBOX_EGRESS_JAIL
    parse_config "$TMPCONF"
    [ "${CLAUDE_SANDBOX_EGRESS_JAIL:-}" = "0" ] && ! egress_jail_enabled
'

# env CLAUDE_SANDBOX_EGRESS_JAIL=0 wins over a bare conf `egress-jail`
parse_case scenario11-egress-jail-env-off-wins 'egress-jail\n' '
    export CLAUDE_SANDBOX_EGRESS_JAIL=0
    parse_config "$TMPCONF"
    [ "$CLAUDE_SANDBOX_EGRESS_JAIL" = "0" ] && ! egress_jail_enabled
'

# the predicate's default-on holds with no conf parsed at all
parse_case scenario11-egress-jail-predicate-default '' '
    unset CLAUDE_SANDBOX_EGRESS_JAIL
    egress_jail_enabled
'

# allow-ip single
parse_case scenario11-allow-ip-single 'allow-ip = 172.23.1.2\n' '
    unset CLAUDE_SANDBOX_ALLOW_IP
    parse_config "$TMPCONF"
    [ "${CLAUDE_SANDBOX_ALLOW_IP:-}" = "172.23.1.2" ]
'

# allow-ip multiple, accumulated newline-separated
parse_case scenario11-allow-ip-multi 'allow-ip = 172.23.1.2\nallow-ip = 10.0.5.6\n' '
    unset CLAUDE_SANDBOX_ALLOW_IP
    parse_config "$TMPCONF"
    printf "%s\n" "$CLAUDE_SANDBOX_ALLOW_IP" | grep -qxF "172.23.1.2" &&
    printf "%s\n" "$CLAUDE_SANDBOX_ALLOW_IP" | grep -qxF "10.0.5.6"
'

# allow-ip with empty value is skipped (no trailing blank entry)
parse_case scenario11-allow-ip-empty 'allow-ip =\n' '
    unset CLAUDE_SANDBOX_ALLOW_IP
    parse_config "$TMPCONF"
    [ -z "${CLAUDE_SANDBOX_ALLOW_IP:-}" ]
'

# comments and blank lines are ignored
parse_case scenario11-comments '# comment\n\nworkspace-root = /from/conf\n# another\n' '
    unset CLAUDE_SANDBOX_WORKSPACE_ROOT
    parse_config "$TMPCONF"
    [ "${CLAUDE_SANDBOX_WORKSPACE_ROOT:-}" = "/from/conf" ]
'

# env var wins over config file value
parse_case scenario11-env-wins 'workspace-root = /from/config\n' '
    export CLAUDE_SANDBOX_WORKSPACE_ROOT=/from/env
    parse_config "$TMPCONF"
    [ "${CLAUDE_SANDBOX_WORKSPACE_ROOT:-}" = "/from/env" ]
'

# absent config file is silently skipped
parse_case scenario11-absent '' '
    unset CLAUDE_SANDBOX_WORKSPACE_ROOT
    parse_config "/nonexistent/claude-sandbox.conf"
    [ -z "${CLAUDE_SANDBOX_WORKSPACE_ROOT:-}" ]
'

# --- Scenario 12: egress-jail DNS override bind (issue #60) ---
# bwrap_argv_build binds a forwarder-pointed resolv.conf over /etc/resolv.conf
# only when CLAUDE_SANDBOX_JAIL_RESOLV names a readable file (the stub-resolver
# path). Absent / missing-file => no override, argv unchanged.
RESOLV_OVERRIDE="$(mktemp)"
printf 'nameserver 192.0.2.53\n' > "$RESOLV_OVERRIDE"
ARGV12="$(HOME=/root CLAUDE_SANDBOX_GITCONFIG_PATH=/etc/claude-gitconfig \
    CLAUDE_SANDBOX_JAIL_RESOLV="$RESOLV_OVERRIDE" \
    bwrap_argv_lines /workspaces/foo /test/.local/bin/claude)"
assert_pair scenario12-resolv-bind "$ARGV12" "--ro-bind" "$RESOLV_OVERRIDE"
assert_contains scenario12-resolv-dest "$ARGV12" "/etc/resolv.conf"
# Default scenario (env unset) must NOT bind /etc/resolv.conf.
assert_not_contains scenario12-default-no-bind "$ARGV1" "/etc/resolv.conf"
# A non-readable path is ignored (no bind, no crash).
ARGV12b="$(HOME=/root CLAUDE_SANDBOX_GITCONFIG_PATH=/etc/claude-gitconfig \
    CLAUDE_SANDBOX_JAIL_RESOLV="/nonexistent/resolv.conf" \
    bwrap_argv_lines /workspaces/foo /test/.local/bin/claude)"
assert_not_contains scenario12-missing-no-bind "$ARGV12b" "/etc/resolv.conf"
rm -f "$RESOLV_OVERRIDE"

# --- Scenario 13: jail_stage_dns always forwards (issues #60, #11) ---
# Drives jail_stage_dns against a fixture resolv.conf via a RESOLV_CONF-aware
# wrapper. EVERY case overrides now: the jail has one DNS path, the pasta
# forwarder, so netns_holder need punch no route to a real resolver (#11).
# search / domain / options must survive the rewrite.
FAKE_RESOLV="$(mktemp)"
stage_dns_with() {
    # Re-source the shadow per case so a fresh CLAUDE_SANDBOX_JAIL_RESOLV is
    # exported, then run jail_stage_dns reading $FAKE_RESOLV in place of /etc.
    bash -c "
        source \"$SHADOW\"
        unset CLAUDE_SANDBOX_JAIL_RESOLV
        jail_stage_dns_test() { sed 's#/etc/resolv.conf#$FAKE_RESOLV#' <<< \"\$(declare -f jail_stage_dns)\"; }
        eval \"\$(jail_stage_dns_test)\"
        jail_stage_dns 2>/dev/null
        if [ -n \"\${CLAUDE_SANDBOX_JAIL_RESOLV:-}\" ]; then
            printf 'OVERRIDE:'; cat \"\$CLAUDE_SANDBOX_JAIL_RESOLV\"
            rm -f \"\$CLAUDE_SANDBOX_JAIL_RESOLV\"
        else
            printf 'NONE\n'
        fi
    "
}

# Routable resolver present => STILL overridden (#11). This is the case that
# used to be left alone, and the one pasta's --dns-host translation broke.
printf 'nameserver 8.8.8.8\n' > "$FAKE_RESOLV"
assert_eq scenario13-routable "OVERRIDE:nameserver 192.0.2.53" \
    "$(stage_dns_with | tr -d '\n')"

# Loopback-only (systemd-resolved stub) => forwarder override.
printf 'nameserver 127.0.0.53\n' > "$FAKE_RESOLV"
assert_eq scenario13-stub "OVERRIDE:nameserver 192.0.2.53" \
    "$(stage_dns_with | tr -d '\n')"

# Mixed loopback + routable => override, same as every other case.
printf 'nameserver 127.0.0.53\nnameserver 192.168.1.1\n' > "$FAKE_RESOLV"
assert_eq scenario13-mixed "OVERRIDE:nameserver 192.0.2.53" \
    "$(stage_dns_with | tr -d '\n')"

# Empty resolv.conf => forwarder override (best-effort, with a warning).
printf '' > "$FAKE_RESOLV"
assert_eq scenario13-empty "OVERRIDE:nameserver 192.0.2.53" \
    "$(stage_dns_with | tr -d '\n')"

# search / domain / options carried over verbatim; real resolvers dropped.
printf 'search a.example b.example\nnameserver 8.8.8.8\noptions timeout:1\n' > "$FAKE_RESOLV"
assert_eq scenario13-search \
    "OVERRIDE:nameserver 192.0.2.53search a.example b.exampleoptions timeout:1" \
    "$(stage_dns_with | tr -d '\n')"
rm -f "$FAKE_RESOLV"

# --- Scenario 14: multi-agent profiles (claude + codex) ---
# The shadow is installed under BOTH names and picks its profile from
# argv[0]. These assertions lock the two things that must differ (which
# binary is exec'd, which $HOME paths carry the login) and the much longer
# list that must NOT (every isolation primitive).

# 14a: argv[0] dispatch. detect_agent is the whole of the decision.
assert_eq scenario14a-claude  claude "$(detect_agent /usr/local/bin/claude  '')"
assert_eq scenario14a-codex   codex  "$(detect_agent /usr/local/bin/codex   '')"
# Anything unrecognised (a test runner's $0, a rename) falls back to claude.
assert_eq scenario14a-default claude "$(detect_agent /tmp/bwrap_argv.sh     '')"
# The override wins, but only for names in the closed set.
assert_eq scenario14a-override codex "$(detect_agent /usr/local/bin/claude  codex)"
if detect_agent /usr/local/bin/claude "/bin/sh" >/dev/null 2>&1; then
    fail "scenario14a — CLAUDE_SANDBOX_AGENT accepted an out-of-set value"
else
    pass
fi

# 14b: the codex profile binds ~/.codex and execs the codex binary.
CODEXHOME="$(mktemp -d)"
register_cleanup "$CODEXHOME"
mkdir -p "$CODEXHOME/.codex" "$CODEXHOME/.claude" "$CODEXHOME/.cache"
touch "$CODEXHOME/.claude.json"
# agent_profile mutates globals; the claude profile is restored at the end
# of the scenario so later scenarios keep the default contract.
agent_profile codex
ARGV14="$(HOME="$CODEXHOME" CLAUDE_SANDBOX_GITCONFIG_PATH=/etc/claude-gitconfig \
    bwrap_argv_lines "$CODEXHOME" "$AGENT_REAL")"

# Codex is exec'd IN PLACE from its root-owned /usr/libexec package, which is
# already visible via --ro-bind / /. Two things follow, and both are the point:
#   - the package's internal layout (ripgrep, its own bwrap/zsh helpers)
#     stays intact at the relocated path;
#   - the binary we exec is READ-ONLY in the session, so an in-session
#     self-update cannot rewrite it — unlike Claude's rw bind-back.
assert_pair scenario14b "$ARGV14" "--" "/usr/libexec/claude-sandbox/codex-launch"
assert_pair scenario14b "$ARGV14" "/usr/libexec/claude-sandbox/codex-launch" "$AGENT_REAL"
assert_not_contains scenario14b "$ARGV14" "$CODEXHOME/.local/bin/codex"
assert_contains scenario14b "$ARGV14" "$CODEXHOME/.codex"
# Claude's login state is NOT bound into a codex session, and vice versa:
# each agent sees only its own credentials.
assert_not_contains scenario14b "$ARGV14" "$CODEXHOME/.claude"
assert_not_contains scenario14b "$ARGV14" "$CODEXHOME/.claude.json"
assert_not_contains scenario14b "$ARGV14" "$CODEXHOME/.local/bin/claude"
# --no-chrome is Claude-only; injecting it would abort codex.
assert_not_contains scenario14b "$ARGV14" "--no-chrome"
# Shared, non-agent-specific binds still apply.
assert_contains scenario14b "$ARGV14" "$CODEXHOME/.cache"
# The vendor unpacks the codex binary INSIDE ~/.codex (which is bound rw), so
# a writable copy of the agent's own binary would otherwise sit in its own
# session. It must be tmpfs-masked, and the mask must come AFTER the bind it
# covers — bwrap applies argv in order, so a mask hoisted above the bind would
# be silently lifted by it.
assert_contains scenario14b "$ARGV14" "$CODEXHOME/.codex/packages"
assert_order scenario14b "$ARGV14" "$CODEXHOME/.codex" "$CODEXHOME/.codex/packages"

# Foreground app-server bypasses managed installation discovery. Keep the
# vendor cache masked, with no package mounted back into it.
assert_not_contains scenario14b-standalone "$ARGV14" "$CODEXHOME/.codex/packages/standalone/current"
assert_not_contains scenario1-standalone "$ARGV1" "/root/.codex/packages/standalone/current"

# 14c: every isolation primitive is identical to the claude path — the
# whole reason this is one file and not two.
for tok in --ro-bind --dev --tmpfs --cap-drop ALL --unshare-user-try \
           --unshare-pid --unshare-ipc --unshare-uts --unshare-cgroup-try \
           --die-with-parent --clearenv; do
    assert_contains scenario14c "$ARGV14" "$tok"
done
assert_contains scenario14c "$ARGV14" "IS_SANDBOX"
assert_contains scenario14c "$ARGV14" "GIT_CONFIG_GLOBAL"
# The in-sandbox verifier needs to know whose session it is: check 03
# asserts the EXACT contents of $HOME, and the expected set is per-agent.
assert_pair scenario14c "$ARGV14" "IS_SANDBOX_AGENT" "codex"
# Must NOT be the CLAUDE_SANDBOX_AGENT override: a nested `claude` spawned
# inside a codex session would then re-dispatch itself to codex.
assert_not_contains scenario14c "$ARGV14" "CLAUDE_SANDBOX_AGENT"
assert_not_contains scenario14c "$ARGV14" "--new-session"
agent_profile claude

# 14d: CODEX_HOME can never be forwarded by pass-env — it would point
# Codex's config + auth.json at an unbound tmpfs path and lose the login.
ARGV14D="$(HOME=/root CLAUDE_SANDBOX_GITCONFIG_PATH=/etc/claude-gitconfig \
    CODEX_HOME=/tmp/evil CLAUDE_SANDBOX_AGENT=codex \
    CLAUDE_SANDBOX_PASS_ENV="CODEX_HOME,CLAUDE_SANDBOX_AGENT" \
    bwrap_argv_lines /workspaces/foo /usr/libexec/claude-sandbox/codex)"
assert_not_contains scenario14d "$ARGV14D" "CODEX_HOME"
assert_not_contains scenario14d "$ARGV14D" "/tmp/evil"

# 14e: nor can IS_SANDBOX_AGENT. It is emitted ABOVE the pass-env loop and a
# later --setenv of the same name wins, so forwarding it would let the conf
# overwrite the sandbox's own value — and it is what battery check 03 reads to
# decide WHICH agent's config dir may legitimately sit under $HOME. Forge it
# and check 03 stops noticing the OTHER agent's credentials in the session.
# Blocked for exactly the reason IS_SANDBOX is.
ARGV14E="$(HOME=/root CLAUDE_SANDBOX_GITCONFIG_PATH=/etc/claude-gitconfig \
    IS_SANDBOX_AGENT=codex \
    CLAUDE_SANDBOX_PASS_ENV="IS_SANDBOX_AGENT" \
    bwrap_argv_lines /workspaces/foo /usr/libexec/claude-sandbox/claude)"
# Exactly one --setenv for it, carrying the sandbox's own value (claude).
assert_eq scenario14e-once 1 "$(grep -cx 'IS_SANDBOX_AGENT' <<<"$ARGV14E")"
assert_pair scenario14e "$ARGV14E" "IS_SANDBOX_AGENT" "claude"


# --- Scenario 15: shipped skills are ro-bound per skill into the agent's ---
# --- own skills dir; nothing is written to the host ~/.claude by the builder ---
# The installed tree under /usr/libexec/claude-sandbox/skills/<name>/ lands at
# ~/<agent skills dir>/<name> inside the jail. One bind per skill (agents
# discover skills exactly one level deep, and a per-skill bind leaves the
# user's own skills in the same dir visible); --ro-bind so a compromised
# session cannot rewrite the bundled scripts.
SKILLSFIX="$(mktemp -d)"
register_cleanup "$SKILLSFIX"
mkdir -p "$SKILLSFIX/shipped/alpha" "$SKILLSFIX/shipped/beta/scripts" "$SKILLSFIX/home/.claude" "$SKILLSFIX/home/.codex" "$SKILLSFIX/home/.pi"
touch "$SKILLSFIX/shipped/alpha/SKILL.md" "$SKILLSFIX/shipped/beta/SKILL.md" "$SKILLSFIX/home/.claude.json"
_saved_skills_dir="$SHIPPED_SKILLS_DIR"
SHIPPED_SKILLS_DIR="$SKILLSFIX/shipped"
ARGV15="$(HOME="$SKILLSFIX/home" CLAUDE_SANDBOX_GITCONFIG_PATH=/etc/claude-gitconfig \
    bwrap_argv_lines "$SKILLSFIX/home" /test/.local/bin/claude)"
assert_pair scenario15-alpha "$ARGV15" "--ro-bind" "$SKILLSFIX/shipped/alpha"
assert_pair scenario15-alpha "$ARGV15" "$SKILLSFIX/shipped/alpha" "$SKILLSFIX/home/.claude/skills/alpha"
assert_pair scenario15-beta  "$ARGV15" "$SKILLSFIX/shipped/beta"  "$SKILLSFIX/home/.claude/skills/beta"
# Never rw: the shipped tree must not be reachable through a --bind.
assert_eq scenario15-ro 0 "$(printf '%s\n' "$ARGV15" | grep -B1 -x "$SKILLSFIX/shipped/alpha" | grep -cx -- '--bind')"
# Per skill, not per tree.
assert_not_contains scenario15-tree "$ARGV15" "$SKILLSFIX/home/.claude/skills"
# The builder is pure: it does not create the host skills dir (the launch
# body does that, explicitly, just before launch).
if [ -e "$SKILLSFIX/home/.claude/skills" ]; then
    fail "scenario15-pure — bwrap_argv_build created the host skills dir"
else
    pass
fi
# The launch body creates the host skills dir. bwrap leaves an empty mount
# point per shipped skill on the host bind, and that empty dir masks nothing,
# so the next launch must not warn about it. A real host skill of the same
# name warns. A host skill no shipped one masks never warns.
mkdir -p "$SKILLSFIX/home/.claude/skills/alpha" "$SKILLSFIX/home/.claude/skills/beta" "$SKILLSFIX/home/.claude/skills/gamma"
touch "$SKILLSFIX/home/.claude/skills/beta/SKILL.md" "$SKILLSFIX/home/.claude/skills/gamma/SKILL.md"
WARN15="$(HOME="$SKILLSFIX/home" prepare_shipped_skills 2>&1)"
assert_eq scenario15-warn "claude-sandbox: ~/.claude/skills/beta is shadowed by the shipped skill for this session." "$WARN15"
rm -rf "$SKILLSFIX/home/.claude/skills"
WARN15="$(HOME="$SKILLSFIX/home" prepare_shipped_skills 2>&1)"
assert_eq scenario15-warn-fresh "" "$WARN15"
if [ -d "$SKILLSFIX/home/.claude/skills" ]; then
    pass
else
    fail "scenario15-mkdir — prepare_shipped_skills did not create the host skills dir"
fi
# Each agent gets the same skills at ITS OWN discovery path.
agent_profile codex
ARGV15C="$(HOME="$SKILLSFIX/home" CLAUDE_SANDBOX_GITCONFIG_PATH=/etc/claude-gitconfig \
    bwrap_argv_lines "$SKILLSFIX/home" "$AGENT_REAL")"
assert_pair scenario15-codex "$ARGV15C" "$SKILLSFIX/shipped/alpha" "$SKILLSFIX/home/.codex/skills/alpha"
assert_not_contains scenario15-codex "$ARGV15C" "$SKILLSFIX/home/.claude/skills/alpha"
agent_profile pi
ARGV15P="$(HOME="$SKILLSFIX/home" CLAUDE_SANDBOX_GITCONFIG_PATH=/etc/claude-gitconfig \
    bwrap_argv_lines "$SKILLSFIX/home" "$AGENT_REAL")"
assert_pair scenario15-pi "$ARGV15P" "$SKILLSFIX/shipped/alpha" "$SKILLSFIX/home/.pi/agent/skills/alpha"
agent_profile claude
# An empty or absent shipped tree emits nothing.
SHIPPED_SKILLS_DIR="$SKILLSFIX/nonexistent"
ARGV15N="$(HOME="$SKILLSFIX/home" CLAUDE_SANDBOX_GITCONFIG_PATH=/etc/claude-gitconfig \
    bwrap_argv_lines "$SKILLSFIX/home" /test/.local/bin/claude)"
assert_not_contains scenario15-none "$ARGV15N" "$SKILLSFIX/nonexistent/*"
SHIPPED_SKILLS_DIR="$_saved_skills_dir"


# --- Scenario 16: ~/.agents/skills is bound rw into EVERY agent (ADR 25) ---
# The one home path shared across agents: skills, not credentials. Only the
# skills dir is bound — never ~/.agents itself — and each agent keeps its own
# config (and so its own skills dir) unshared.
SHAREDFIX="$(mktemp -d)"
register_cleanup "$SHAREDFIX"
mkdir -p "$SHAREDFIX/.agents/skills" "$SHAREDFIX/.agents/plugins" \
    "$SHAREDFIX/.claude" "$SHAREDFIX/.codex" "$SHAREDFIX/.pi"
touch "$SHAREDFIX/.claude.json"
for _agent in claude codex pi; do
    agent_profile "$_agent"
    ARGV16="$(HOME="$SHAREDFIX" CLAUDE_SANDBOX_GITCONFIG_PATH=/etc/claude-gitconfig \
        bwrap_argv_lines "$SHAREDFIX" "$AGENT_REAL")"
    assert_pair "scenario16-$_agent-rw"  "$ARGV16" "--bind" "$SHAREDFIX/.agents/skills"
    assert_pair "scenario16-$_agent-dst" "$ARGV16" "$SHAREDFIX/.agents/skills" "$SHAREDFIX/.agents/skills"
    # Narrow: the parent and its other entries stay on the tmpfs.
    assert_not_contains "scenario16-$_agent-narrow"  "$ARGV16" "$SHAREDFIX/.agents"
    assert_not_contains "scenario16-$_agent-plugins" "$ARGV16" "$SHAREDFIX/.agents/plugins"
done
# Sharing skills does not share credentials: the other agents' config dirs
# stay out of a pi session.
assert_not_contains scenario16-isolation "$ARGV16" "$SHAREDFIX/.claude"
assert_not_contains scenario16-isolation "$ARGV16" "$SHAREDFIX/.codex"
agent_profile claude
# Absent on the host: no bind, and the builder does not create it.
rm -rf "$SHAREDFIX/.agents"
ARGV16N="$(HOME="$SHAREDFIX" CLAUDE_SANDBOX_GITCONFIG_PATH=/etc/claude-gitconfig \
    bwrap_argv_lines "$SHAREDFIX" /test/.local/bin/claude)"
assert_not_contains scenario16-absent "$ARGV16N" "$SHAREDFIX/.agents/skills"
if [ -e "$SHAREDFIX/.agents" ]; then
    fail "scenario16-pure — bwrap_argv_build created ~/.agents"
else
    pass
fi

# --- Scenario 17: config word lists never glob against the cwd ---
# The cwd at launch is the workspace, writable from the jail. If a conf word
# list were pathname-expanded there, `pass-env = *` or `local-port = *` would
# pick up whatever file names a compromised session planted: a file named
# after a secret variable would forward that secret, one named 5432 would
# relay that port. Every list must reach its consumer as literal words.
GLOBFIX="$(mktemp -d)"
register_cleanup "$GLOBFIX"
touch "$GLOBFIX/PLANTED_SECRET" "$GLOBFIX/5432" "$GLOBFIX/6543"
ARGV17="$(cd "$GLOBFIX" && HOME="$TMPHOME" CLAUDE_SANDBOX_PASS_ENV="*, PLANTED_*" \
    PLANTED_SECRET=planted-value \
    CLAUDE_SANDBOX_GITCONFIG_PATH=/etc/claude-gitconfig \
    bwrap_argv_lines "$TMPHOME" /test/.local/bin/claude)"
assert_not_contains scenario17-pass-env-name "$ARGV17" "PLANTED_SECRET"
assert_not_contains scenario17-pass-env-value "$ARGV17" "planted-value"

# local-port and callback-port: the literal word survives, so validation
# names it and refuses the launch, and no planted port reaches the relay set.
assert_eq scenario17-local-ports "$(printf '%s\n' '*' '5[0-9]*')" \
    "$(cd "$GLOBFIX" && CLAUDE_SANDBOX_LOCAL_MODEL_PORT=0 \
        CLAUDE_SANDBOX_LOCAL_PORTS='*
5[0-9]*' local_ports)"
if (cd "$GLOBFIX" && CLAUDE_SANDBOX_LOCAL_MODEL_PORT=0 CLAUDE_SANDBOX_LOCAL_PORTS='*' \
        validate_local_model_port) >/dev/null 2>&1; then
    fail "scenario17-local-port-validate — a glob local-port was accepted"
else
    pass
fi
assert_eq scenario17-callback-ports '*' \
    "$(cd "$GLOBFIX" && CLAUDE_SANDBOX_CALLBACK_PORTS='*' callback_ports)"
if (cd "$GLOBFIX" && CLAUDE_SANDBOX_LOCAL_MODEL_PORT=0 CLAUDE_SANDBOX_CALLBACK_PORTS='*' \
        validate_callback_ports) >/dev/null 2>&1; then
    fail "scenario17-callback-port-validate — a glob callback-port was accepted"
else
    pass
fi

# --- Scenario 18: a callback/local overlap is reported however long the list ---
# The check used to pipe local_ports into grep -q. Under pipefail, grep exits
# on an early match, local_ports takes SIGPIPE on its next write, and the
# pipeline fails, so an overlap first in a long list went unreported. Put the
# overlap first, follow it with hundreds of ports, and repeat the check: every
# run must refuse.
_long_ports="$(seq 1001 1400 | tr '\n' ' ')"
_overlap_missed=0
for _ in 1 2 3 4 5 6 7 8 9 10; do
    if (CLAUDE_SANDBOX_LOCAL_MODEL_PORT=0 CLAUDE_SANDBOX_LOCAL_PORTS="1000 $_long_ports" \
            CLAUDE_SANDBOX_CALLBACK_PORTS=1000 validate_callback_ports) >/dev/null 2>&1; then
        _overlap_missed=$((_overlap_missed + 1))
    fi
done
assert_eq scenario18-overlap-first-in-long-list 0 "$_overlap_missed"
# And a callback port absent from that list still passes.
assert_parse scenario18-no-overlap env CLAUDE_SANDBOX_LOCAL_MODEL_PORT=0 \
    CLAUDE_SANDBOX_LOCAL_PORTS="1000 $_long_ports" CLAUDE_SANDBOX_CALLBACK_PORTS=2000 \
    bash -c 'export CLAUDE_SHADOW_SOURCE_ONLY=1; . "$SHADOW"; validate_callback_ports'

finish bwrap_argv.sh
