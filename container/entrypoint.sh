#!/usr/bin/env bash
# Entrypoint for the published claude-sandbox image (podman/docker run —
# no devcontainer). The image bakes a full install at build time
# (container/Dockerfile); this re-runs only the launch-time steps that
# depend on runtime mounts, then execs the requested command (default:
# claude, i.e. the shadow on $PATH).
#
# Sourcing install.sh (its source guard keeps main() from running) reuses
# the exact functions the devcontainer path runs via postCreate — one
# installer, one audit surface.
set -euo pipefail

# shellcheck disable=SC1091
source /opt/claude-sandbox/.devcontainer/claude-sandbox/install.sh

# Keep Git's normal global config writable for gh/glab authentication.
# Import only identity from the separate read-only host mount.
# shellcheck disable=SC1091
source /opt/claude-sandbox/container/git-config.sh
configure_container_git "${HOME:-/root}/.gitconfig-host" "${HOME:-/root}/.gitconfig"

# An image built with CLAUDE_SANDBOX_IMPL=python (the Python installer, issue
# #72) redoes its share of the install with the same Python installer, from
# its root-owned venv: the image's /usr/local/bin/claude is then the shim.
PY_INSTALLER=()
if cmp -s /usr/local/bin/claude /opt/claude-sandbox/.devcontainer/claude-sandbox/claude-shim; then
    PY_INSTALLER=(/usr/libexec/claude-sandbox/venv/bin/python -I -m claude_sandbox.installer
        --source /opt/claude-sandbox)
fi

# Persist Claude login/memory/settings across containers when the
# launcher mounts a shared host dir at /user-terminal-config. No-op when
# absent — but then ~/.claude dies with the container, and the shadow's
# persistence check warns loudly about exactly that. The Python installer
# also re-stamps the conf here, unless a conf is mounted over it.
if [ "${#PY_INSTALLER[@]}" -gt 0 ]; then
    "${PY_INSTALLER[@]}" --container-start
else
    link_terminal_config
    ensure_cred_dirs
fi

# First use of an empty share leaves ~/.claude.json as a zero-length
# file (via link_terminal_config's seed + ensure_cred_dirs' touch), and
# Claude Code rejects zero-length as corrupted JSON ("Unexpected EOF").
# Seed the empty object so first launch starts clean; a populated file
# is left untouched.
claude_json="${HOME:-/root}/.claude.json"
if [ ! -s "$claude_json" ]; then
    echo '{}' > "$claude_json"
fi

# Re-stamp /etc/claude-sandbox.conf from the baked clone — unless the
# operator mounted their own conf over it (a read-only bind, which must
# win and would EROFS the copy anyway). A mounted conf still satisfies
# Invariant 4: it sits at /etc, read-only, outside the sandbox rw set.
if [ "${#PY_INSTALLER[@]}" -eq 0 ] && ! _is_mount /etc/claude-sandbox.conf; then
    install_conf
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
if [ "${#PY_INSTALLER[@]}" -gt 0 ]; then
    "${PY_INSTALLER[@]}" --probe-userns
else
    probe_userns_or_refuse
fi

exec "$@"
