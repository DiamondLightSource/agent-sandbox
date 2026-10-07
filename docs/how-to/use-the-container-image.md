# Use the prebuilt container image

The host launcher runs Claude, Codex or Pi in
`ghcr.io/diamondlightsource/claude-sandbox`. Use it when you do not need a
project-specific devcontainer.

## Install and run

On a Linux host with [uv](https://docs.astral.sh/uv/getting-started/installation/),
rootless Podman, `/dev/net/tun` and unprivileged user namespaces:

```bash
uv tool install claude-sandbox
cd ~/src/my-project
claude-sandbox
```

The first run pulls the image matching the package version and creates a
project container. Later sessions reuse it; it stops when the last session
exits. [Getting started](../tutorials/getting-started.md) covers login and verification.

```bash
claude-sandbox codex
claude-sandbox pi
claude-sandbox shell
```

`shell` opens an **unsandboxed container terminal** for package installation
and administration. From there, `claude`, `codex` and `pi` start sandboxed
agents. In your own devcontainer, use its normal terminal instead.

The shell follows your launching shell, with bash as fallback; override with
`CLAUDE_SANDBOX_SHELL`. It loads your shared terminal configuration. X11 apps
can use your display if `DISPLAY` was set when the container was created.

Helpers such as `claude-sandbox verify` and `claude-sandbox gh-auth` also work
directly from the host, in the project directory.

## Versions and updates

Exit active sessions, then run in each project you want to update:

```bash
uv tool upgrade claude-sandbox
claude-sandbox --recreate
```

Recreation removes container-local packages, forge logins and the project
venv. Project files, shared agent settings and cached downloads survive.
See [Upgrade](upgrade.md) for other installation routes and version pins.

To remove old project containers:

```bash
claude-sandbox clean                    # stopped containers only
claude-sandbox clean --force            # also ends running sessions
claude-sandbox clean --force --images   # also removes unused sandbox images
```

Cleanup also prunes venvs for removed containers. The next launch creates a
container from the installed package's image version.

## Authentication and persistence

Agent logins, memory and settings persist in the host's
`~/.config/terminal-config`, mounted at `/user-terminal-config`. Set
`CLAUDE_SANDBOX_SHARED_CONFIG` at container creation to use another directory.
Each agent sees its own credential store.

Forge tokens stay in the project container. Authenticate from that project's
directory and repeat after recreation:

```bash
claude-sandbox gh-auth
claude-sandbox glab-auth
```

Use [project-scoped tokens](authenticate-with-forges.md). Git uses HTTPS
credential helpers; only your name and email are imported from the host Git
config, not its credentials or SSH settings.

## Tell containers apart

Each project has its own container and forge logins. To show its tag in Claude,
Pi and shell prompts:

```bash
claude-sandbox doctor        # inspect setup
claude-sandbox doctor --fix  # apply it, backing up changed files
```

The tag looks like `myproj-3f2a` and lives in `/etc/claude-sandbox-tag`.
`--fix` updates the Claude status line, Pi footer and marked prompt blocks in
shared `bashrc`/`zshrc`. Remove the `claude-sandbox prompt tag` block to undo a
shell prompt change. Older untagged containers need recreation.

`doctor` also reports a `warn` line when a command
named `claude`, `codex`, `pi` or `claude-sandbox` comes before the sandbox on
`PATH`, or when `claude-sandbox alerts` lists anything the PATH watcher
quarantined ({ref}`ADR 27 <adr-outer-path-guard>`). `--fix` does not touch
either. `doctor` exits 1 when anything is to do or any warning is shown, so
scripts can rely on its status.

## Configure the sandbox

Create `~/.config/claude-sandbox.conf` on the host, or set
`CLAUDE_SANDBOX_CONF` to another file. It replaces the image's config and is
mounted read-only at `/etc/claude-sandbox.conf`. Retain defaults you need:

```ini
allow-write = /cache
allow-ip = 172.23.142.119   # Diamond GitLab
local-model-port = 1920
```

Recreate an existing container when first adding the file. In-place edits
apply on the next agent launch; recreate if your editor replaces the mounted
file. See [Configuration](../reference/configuration.md) for all keys and
[network access](network-egress-jail.md) for IP and port exceptions.

Mounts, devices, network mode and forwarded `CLAUDE_SANDBOX_*` variables are
fixed at container creation. Include `--recreate` when changing them.

## Extra paths

The project is mounted read-write; each agent's writable root is the directory
it starts in. Add specific paths from the host:

```bash
claude-sandbox --mount ~/src/shared-lib       # read-only
claude-sandbox --mount-rw ~/src/other-repo    # writable in container and sandbox
claude-sandbox --recreate --peers            # mount the project's parent
```

`--peers` lets you enter sibling projects from a container shell and launch
agents there. Each agent can read the siblings, including their secrets,
but its writable root remains its launch directory. The parent mount is
skipped when it would expose your home directory.

Peers are off by default. Containers created by 4.4.0 retain its old default
parent mount until recreated; `--no-peers` explicitly selects the new default.
These options do not change mounts in your own devcontainer.

`workspace-root` can widen the sandbox's writable area only within paths the
container already has writable. Automounted project trees such as `/dls_sw`
use slave propagation so host automounter mounts appear inside.

## Devices

The `--gpu` option exists in 4.7.0 but is not yet functional and is under
development.

To expose a device:

```bash
claude-sandbox --device /dev/ttyUSB0
```

Add `--recreate` for an existing container. Paths must name existing character
or block devices under `/dev`; symlinks resolve to their canonical path.
Directories and `host:container:permissions` mappings are not accepted.
The host user must have device access. Podman uses `--group-add keep-groups`
for supplementary group access, which requires crun.

In your own devcontainer, configure runtime device access and add
repeatable `allow-device = /dev/…` entries to `/etc/claude-sandbox.conf`.

**Every process in the agent session can use exposed devices.** Driver exploits
can cross the sandbox boundary; device resource exhaustion and side
effects are not contained. Raw disks can bypass filesystem protections.
Choose only the devices needed. See the [threat model](../explanations/threat-model.md#device-access).

## Toolchains

The image includes Python, uv, Node.js and npm. The launcher selects a
per-project venv at `/cache/venv-for<project path>`, available through
`/opt/venv`. The mounted project's `.venv` is not used by default.

The `claude-sandbox-cache` volume holds venvs, uv downloads and pre-commit
caches across projects. Recreation makes a fresh venv but retains downloads.
Set `CLAUDE_SANDBOX_CACHE` to another volume name, or empty to disable it.

npm lifecycle scripts are disabled by default, though project configuration
can override that. Install system dependencies in a container shell. For
site-specific toolchains, use [your own devcontainer](sandbox-a-team-devcontainer.md).

## Networking and platform limits

Host networking lets configured loopback relays reach host services while
agents retain their private network jail. `--bridge` changes container
networking and prevents its loopback from reaching host services. For OAuth
relay conflicts, see [browser logins](network-egress-jail.md#let-a-browser-login-reach-the-agent).

Rootless Podman on Linux is supported. Rootless Docker
(`CLAUDE_SANDBOX_ENGINE=docker`) and macOS with a Linux VM are untested;
rootful Docker cannot host the default jail.

## Without uv

The launcher is an ordinary Python package; [install it with pipx or
pip](install-without-uv.md#host-launcher) instead.
