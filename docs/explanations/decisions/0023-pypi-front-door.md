(adr-pypi-front-door)=

# 23. A PyPI wheel as the front door: `uvx claude-sandbox`

Date: 2026-09-13

## Status

Accepted

Amended by {ref}`ADR 26 <adr-python-implementation>` (2026-10-06): the
sandbox is being reimplemented in Python, so the wheel will stop bundling
bash verbatim and become the implementation once that migration finishes
(issue #72). The packaging moved from `packaging/pypi/` to a root
`pyproject.toml` and `src/claude_sandbox/`, with a development lockfile and
pytest, which "The bash-only boundary holds" below ruled out. Until the
switch-over the wheel still ships the bash and execs it, as described here.

Documentation update (2026-09-14): the recommended host workflow is
`uv tool install claude-sandbox`, then `claude-sandbox`. The `uvx` launcher
remains available for one-off use and `uvx claude-sandbox install` remains
the devcontainer installation recipe. The decision below records the original
distribution design.

Refines {ref}`ADR 8 <adr-bash-only>` (bash-only) and {ref}`ADR 17
<adr-remove-promote>` (reference at a pin, never copy).

## Context

Two consumers of the sandbox were paying a distribution tax that the code
itself does not justify.

The **guest devcontainer** is the most common way the sandbox is used: a
project's own devcontainer, with the sandbox injected at `postCreate`. ADR
17 made that a reference at a pinned tag, which is right, but the reference
was twelve lines of git plumbing per repository (clone, fetch, checkout,
`install --here`), and each repository carried its own copy of the recipe.

The **host launcher** for the published image, `container/claude-container`,
was fetched with `curl` and copied onto `PATH`. A copied script has no
package manager: it went stale silently, and the notify-only versioning of
{ref}`ADR 17 <adr-remove-promote>`'s companion image design deliberately
refuses a self-updater, so staleness was structural. Its version also had
nothing to do with the image tag it pulled (`:latest`), so a fresh image and
an old launcher, or the reverse, was the normal state.

Two alternatives were considered and set aside. A **devcontainer feature**
would inject the sandbox with one JSON line and no runtime dependency, but
features run at image build and the team found them slow to start; the
launcher is host-side, where a feature does not help at all. A **bootstrap
`curl | bash`** one-liner removes lines but not the staleness, and adds a
second fetch path to audit.

## Decision

Publish one wheel, `claude-sandbox`, on PyPI, and make `uvx claude-sandbox`
the front door for both consumers.

The wheel holds **no sandbox logic**. It bundles, verbatim, the launcher
script, the `install` shim, `.devcontainer/claude-sandbox/`, the shipped conf
and the statusline script, in the repository's own layout, plus a one-module
Python entry point that locates them and execs bash. The bash is what runs;
the Python sets two environment variables and calls `execvpe`.

- `uvx claude-sandbox [OPTIONS] [claude|codex|pi|shell] [AGENT_ARGS...]` is
  the launcher. The verbs replace `--agent NAME` and `--shell`. The helper
  verbs of the in-container CLI (`gh-auth`, `glab-auth`, `verify`,
  `pi-local`, `version`, `update`) are forwarded into the container, so
  `uvx claude-sandbox verify` means the same on the host as inside.
- `uvx claude-sandbox install` runs the installer inside a devcontainer. The
  entry point refuses it outside a container (`CLAUDE_SANDBOX_HOST_INSTALL=1`
  overrides), because a one-word command can be typed on a host by accident
  where the twelve-line recipe could not.
- The launcher refuses to run inside a container: an installed sandbox is
  started by the agents' own names, and a bare container wants `install`.
  `CLAUDE_SANDBOX_NESTED=1` overrides, for an engine inside a container.

**One release number: the git tag.** The wheel version comes from the tag
by `hatch-vcs` (the same setuptools_scm mechanism the DLS python-copier
template uses; `_dist.yml`, `_pypi.yml` and `_release.yml` are copied from
it, and `ci.yml` wires them in the same way). PyPI normalises a prerelease
tag such as `4.0.0-beta.1` to `4.0.0b1`; image tags are the raw tag, so the
entry point maps the wheel version back before pinning
`CLAUDE_SANDBOX_IMAGE` (a wheel built between tags has no image and falls
back to `:latest`). The hyphen in the tag is what keeps the install shim's
newest-stable filter from choosing a beta. CI bakes the tag into the image
label on a tag build, and the entry point passes the same value to the
launcher as `CLAUDE_SANDBOX_LAUNCHER_VERSION`, so `uvx claude-sandbox==4.0.0`
runs that launcher against that image and `uvx claude-sandbox@latest` moves
both together (`CLAUDE_SANDBOX_IMAGE` still overrides). A copied script keeps
its own `VERSION=` literal, `:latest` and the label comparison, which is
what serves that path.

**Host networking by default.** The launcher now creates the container with
`--network=host`; `--bridge` opts out. Pi's relay to a local model needs the
host's loopback, and the repository's own devcontainer already runs
host-net. The agents' posture is unchanged: the egress jail is built inside
the container and only restricts, so RFC1918 stays blackholed and loopback
crosses only through the configured relay ports whatever the container's
network mode ({ref}`ADR 15 <adr-network-egress-jail>`).

**The bash-only boundary holds.** The packaging lives under
`packaging/pypi/` only: no root `pyproject.toml`, no lockfile, no `src/`,
no test framework. It is the third and last Python exception in
`CLAUDE.md`, alongside the docs toolchain and the socket fixture, with the
same rule: it may not grow logic. Published as a wheel only; an sdist would
be a second copy of the tree, which ADR 17 forbids.

## Consequences

- The guest recipe is one line, `uvx claude-sandbox==X.Y.Z install`, and the
  pin is the version. The clone recipe stays documented for images without
  `uv`; both run the same installer.
- `claude-sandbox update` after a wheel install points at `uvx` instead of
  cloning past the pin. The installer stamps `installer` next to `version`
  under `/usr/libexec/claude-sandbox/` so the CLI can tell.
- PyPI is a second trust root beside the GitHub tag. Trusted publishing
  (OIDC from the release workflow) means no token exists to leak; the
  `pypi` environment on the repository must be created once, and the PyPI
  project's publisher set to it, before the first tag publishes.
- The launcher's pre-4.0 flags (`--agent`, `--shell`, `--host-net`) exit
  with the new spelling rather than fall through to the agent's argv. No
  aliases: an unknown option would otherwise fall through to the agent and fail obscurely.
- `uvx` caches the wheel: a plain `uvx claude-sandbox` does not move to a
  new release on its own. That is the deliberate-update property the
  notify-only design wanted, now provided by the package manager.
- Releases are git tags from 4.0.0 on: the wheel, the image and the
  launcher report the tag, with nothing to bump in the tree.
