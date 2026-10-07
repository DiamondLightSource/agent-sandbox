#!/usr/bin/env bash
# Entrypoint for the published claude-sandbox image (podman/docker run —
# no devcontainer). The image bakes a full install at build time
# (the root Dockerfile); this re-runs only the launch-time steps that
# depend on runtime mounts, then execs the requested command (default:
# claude, i.e. the shadow on $PATH).
#
# Those steps are the installer's own (`--container-start`,
# `--probe-userns`), run from its root-owned venv with -I — the same code
# the devcontainer path runs via postCreate: one installer, one audit
# surface.
set -euo pipefail

INSTALLER=(/usr/libexec/claude-sandbox/venv/bin/python -I -m claude_sandbox.installer
    --source /opt/claude-sandbox)

# Keep Git's normal global config writable for gh/glab authentication.
# Import only identity from the separate read-only host mount.
# shellcheck disable=SC1091
source /opt/claude-sandbox/container/git-config.sh
configure_container_git "${HOME:-/root}/.gitconfig-host" "${HOME:-/root}/.gitconfig"

# Persist Claude login/memory/settings across containers when the
# launcher mounts a shared host dir at /user-terminal-config. No-op when
# absent — but then ~/.claude dies with the container, and the shadow's
# persistence check warns loudly about exactly that. Then the credential
# directories, and the conf: /etc/claude-sandbox.conf is re-stamped from the
# baked clone unless the operator mounted their own over it (a read-only
# bind, which must win and would EROFS the copy anyway). A mounted conf
# still satisfies Invariant 4: it sits at /etc, read-only, outside the
# sandbox rw set.
"${INSTALLER[@]}" --container-start

# First use of an empty share leaves ~/.claude.json as a zero-length
# file (via the shared-config seed and the credential directories' touch), and
# Claude Code rejects zero-length as corrupted JSON ("Unexpected EOF").
# Seed the empty object so first launch starts clean; a populated file
# is left untouched.
claude_json="${HOME:-/root}/.claude.json"
if [ ! -s "$claude_json" ]; then
    echo '{}' > "$claude_json"
fi

# The project venv. VIRTUAL_ENV is the image default (/cache/venv, in the
# container layer) or the launcher's per-project /cache/venv-for<path> on
# the shared cache volume. A fresh container gets a fresh venv — the DLS
# devcontainer's postCreate `uv venv --clear` — marked in the container
# layer so a restart keeps it; /opt/venv (container-local, on the image
# PATH) is pointed at it so shells and the jail find `python` without
# knowing the path. Best-effort: a venv failure must not stop the sandbox.
venv="${VIRTUAL_ENV:-/cache/venv}"
venv_mark=/var/lib/claude-sandbox/venv-created
if [ ! -e "$venv_mark" ] || [ ! -x "$venv/bin/python" ]; then
    if uv venv --clear --quiet "$venv" 2>&1; then
        mkdir -p "$(dirname "$venv_mark")" && touch "$venv_mark"
    else
        echo "claude-sandbox: could not create venv at $venv (uv above); continuing" >&2
    fi
fi
ln -sfn "$venv" /opt/venv

# The launcher's short container tag, for prompts and status lines. A file
# under /etc, not an env var: every agent jail clears the environment but
# sees /etc read-only. Absent when the container was not made by the launcher.
if [ -n "${CLAUDE_SANDBOX_TAG:-}" ]; then
    printf '%s\n' "$CLAUDE_SANDBOX_TAG" > /etc/claude-sandbox-tag
fi

# The image build skipped this probe deliberately (a builder that can
# nest namespaces proves nothing about this host — see the Dockerfile).
# Refuse HERE, at container start, if the runtime host cannot run
# unprivileged user namespaces: refusal-on-failure, never a sandbox that
# isn't one.
"${INSTALLER[@]}" --probe-userns

exec "$@"
