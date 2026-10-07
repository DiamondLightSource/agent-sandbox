# Two consumers, one file, one base:
#
#   developer      — the repo's own devcontainer (devcontainer.json builds
#                    `target: developer`). Intentionally a bare FROM: the
#                    DLS ubuntu-devcontainer image already ships the
#                    dev-tooling baseline (git, curl, ca-certificates, jq,
#                    sudo) the installer's bootstrap needs; everything else
#                    (bubblewrap, nodejs, gh) is apt-installed by the
#                    installer (`./install --here`) at postCreate.
#   claude-sandbox — the PUBLISHED image (ghcr.io/diamondlightsource/claude-sandbox,
#                    built by .github/workflows/container.yml): sandboxed
#                    Claude Code for hosts WITHOUT a devcontainer workflow;
#                    rootless podman/docker + `uvx claude-sandbox` (the
#                    launcher) is all a host needs. It builds FROM the
#                    developer stage and is installed by the same installer
#                    the devcontainer runs — dogfood ≈ guest ≈ image, one
#                    installer, one audit surface.
# Global-scope ARG: `COPY --from` cannot expand a stage-scoped one, so the
# Node source is a named stage (used by the claude-sandbox stage below).
ARG NODE_VERSION=22
FROM node:${NODE_VERSION}-slim AS node

FROM ghcr.io/diamondlightsource/ubuntu-devcontainer:noble AS developer

FROM developer AS claude-sandbox

# The launcher version (the `uvx claude-sandbox` wheel) this image was built
# and tested with. CI passes the release tag, or the version hatch-vcs
# derives between tags; the launcher reads the label from the pulled image
# to warn when the user's copy is out of date.
ARG LAUNCHER_VERSION=""
LABEL io.diamondlightsource.claude-sandbox.launcher-version="${LAUNCHER_VERSION}"

# What `claude-sandbox version` reports inside the image. .dockerignore
# excludes .git, so the installer can't run `git describe` at build —
# CI passes the ref name (tag on releases, `main` otherwise) instead.
ARG CLAUDE_SANDBOX_VERSION=""

# Whether to fetch OpenAI's Codex CLI at build time (the installer's own
# WITH_CODEX knob, exposed here). The codex SHADOW is installed either way — Invariant 1 says the shadow must own the name
# on $PATH regardless — this only controls the best-effort curl fetch of
# the real binary. Default on, matching the installer; set to 0 to build an
# image that never reaches chatgpt.com (e.g. an offline/air-gapped build,
# or to keep the published image's Codex support pinned to a build done
# with known network access rather than whichever runner happened to build
# a given tag).
ARG WITH_CODEX=1
ARG WITH_PI=1
ARG PI_VERSION=latest
COPY . /opt/claude-sandbox
WORKDIR /opt/claude-sandbox

# Install the same files as a devcontainer: install.sh fetches a pinned uv,
# provisions the root-owned interpreter and venv under
# /usr/libexec/claude-sandbox and hands over to the Python installer.
# Runtime mounts and the namespace probe are handled by the entrypoint when
# the image starts on its host.
# The pinned uv is dropped in the same layer: it has done its one job
# (provisioning), the base image already ships a uv for projects, and a
# reinstall in a container fetches and checks it again. One uv in the image,
# and the installer never has to trust the base image's unpinned one.
RUN bash .devcontainer/claude-sandbox/install.sh --image-build \
    && rm -rf /usr/libexec/claude-sandbox/uv \
    && apt-get install -y --no-install-recommends vim \
    && rm -rf /var/lib/apt/lists/*

# Python for the agent — IMAGE-ONLY by design. The devcontainer stage and
# clone+install guests get none of this (the agent's venv is the guest
# project's business). Here there is no project
# devcontainer to supply one, so the image does: the sandbox's own pinned
# CPython, which the installer above provisioned root-owned and outside
# every allow-write under /usr/libexec/claude-sandbox/python, is also the
# projects' interpreter (one Python in the image, issue #85). uv finds it
# as a managed Python through UV_PYTHON_INSTALL_DIR; a project venv only
# links to it, so sharing it gives the jail no write access to it, and the
# sandbox's own venv stays separate and runs with -I. A build that cannot
# find it fails here rather than downloading a second copy. Root outside
# the jail can still `uv python install` another version beside it; a
# reinstall of the sandbox removes such extras.
# The venv, the uv cache and the tool dir live
# under /cache, which the shipped conf already binds rw (allow-write =
# /cache). The launcher puts /cache on a named volume shared by every
# project container, laid out as the DLS python-copier devcontainer does:
# the venv lives on the SAME filesystem as the uv cache, at
# /cache/venv-for<project path>, so uv hardlinks wheels in instead of
# copying; the entrypoint (re)creates it on a fresh container (postCreate's
# `uv venv --clear`) and points /opt/venv — container-local, on PATH
# below — at it, so two projects sharing the volume never share a venv.
# Without the launcher (plain `podman run`) the defaults here apply: the
# venv at /cache/venv in the container layer, wiped with the container.
# UV_PROJECT_ENVIRONMENT keeps
# `uv sync`/`uv add` out of the workspace: the host's own .venv there is
# never touched, and the container's interpreter path never leaks into
# it. The shadow passes VIRTUAL_ENV and the UV_* vars through --clearenv
# and appends $VIRTUAL_ENV/bin to the jail PATH (never prepends —
# Invariant 1). Home stays ephemeral on purpose.
# Keep requires-python in the root pyproject.toml <= the installer's
# PYTHON_VERSION (installer/provision.py).
ENV UV_PYTHON_INSTALL_DIR=/usr/libexec/claude-sandbox/python \
    UV_PROJECT_ENVIRONMENT=/cache/venv \
    VIRTUAL_ENV=/cache/venv \
    UV_CACHE_DIR=/cache/uv \
    UV_TOOL_DIR=/cache/uv-tools \
    PATH=/opt/venv/bin:$PATH
RUN uv venv --no-python-downloads --managed-python /cache/venv \
    && grep -q '^home = /usr/libexec/claude-sandbox/python/cpython-' /cache/venv/pyvenv.cfg \
    && ln -s /cache/venv /opt/venv \
    && uv cache clean --quiet

# Node.js LTS for the agent — IMAGE-ONLY, same reasoning as the Python
# block. Copied from the official image like uv is from astral's: the
# noble base has no npm, and its apt `npm` is npm 9 on the end-of-life
# Node 18 (Playwright's package wants >= 20) behind ~360 dependency
# packages. This gives `pi install npm:...` (extensions land on the shared
# ~/.pi, so they persist across containers), npx, and a current node.
# /usr/local/bin precedes /usr/bin on the jail PATH, so the installer's
# apt nodejs 18 is shadowed, not removed (guests still rely on it).
COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && ln -s ../lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx \
    # Lifecycle scripts off by default: `pi install npm:...` and `npx` then
    # run no postinstall inside the jail (pi passes no --ignore-scripts).
    # This is npm's global config for the copied npm, ro in-session — a
    # default the agent can override per project (~/.npmrc, .npmrc), so a
    # brake on drive-by packages, not a wall. Native modules needing
    # node-gyp fail loudly with it; the agent should ask, not work around.
    && printf 'ignore-scripts=true\n' > /usr/local/etc/npmrc \
    && node --version && npm --version && npx --version

# No USER directive, deliberately (the DLS base-image pattern): the
# supported runtime is a ROOTLESS engine, where in-container root maps
# to the unprivileged invoking host user via user namespaces — root in
# here is not root on the host. Under a ROOTFUL engine Claude really
# would run as host UID 0; that path is unsupported (and the egress
# jail's pasta attach is denied there anyway — see the how-to's
# troubleshooting note).
#
# The entrypoint re-runs the launch-time installer steps that depend on
# runtime mounts (shared ~/.claude and ~/.codex, /etc conf), probes userns,
# then execs the command — default: claude, i.e. the shadow on $PATH. Run the
# image with `codex` as the command to get a sandboxed Codex session from the
# same image (the same shadow, dispatching on argv[0]).
ENTRYPOINT ["/opt/claude-sandbox/container/entrypoint.sh"]
CMD ["claude"]
