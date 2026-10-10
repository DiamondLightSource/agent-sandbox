# Set up a devcontainer for your project

Use a custom devcontainer when the agent needs your project's toolchain.
For the simplest start, the [PyPI launcher](getting-started.md) supplies its
own container.

## Create the container configuration

Use rootless Podman. In VS Code, set `dev.containers.dockerPath` to
`podman` and install the Dev Containers extension.

Create `.devcontainer/devcontainer.json`:

```json
{
  "name": "my-project",
  "image": "mcr.microsoft.com/devcontainers/base:ubuntu",
  "remoteUser": "root",
  "runArgs": ["--device=/dev/net/tun"]
}
```

Open the project in VS Code and select **Dev Containers: Reopen in Container**.
The root user is inside a rootless container; `/dev/net/tun` is required
by the network jail, and the install warns if it is missing. Rootful Docker
is unsupported.

## Install the sandbox

Inside the container, make [uv](https://docs.astral.sh/uv/getting-started/installation/)
available if the image does not already provide it, then run:

```bash
uvx claude-sandbox install
claude
```

If you only use Claude, `uvx claude-sandbox install --minimal` leaves out
Codex and Pi and installs faster; see
[Claude-only installation](../reference/whats-installed.md#claude-only-installation).
For installation on every rebuild, follow
[Sandbox a team devcontainer](../how-to/sandbox-a-team-devcontainer.md).
Without uv, use the [clone fallback](../how-to/install-without-uv.md).

## Persist agent logins

The PyPI host launcher handles persistence automatically. For a custom
devcontainer, merge these settings into `devcontainer.json`:

```json
"initializeCommand": "mkdir -p \"$HOME/.config/terminal-config\"",
"mounts": [
  "source=${localEnv:HOME}/.config/terminal-config,target=/user-terminal-config,type=bind"
]
```

The initialize command creates the directory as your host user before the
container mounts it. Preserve any existing commands and mounts.
Rebuild and run `uvx claude-sandbox install` (or let postCreate do it).
The installer links agent state into the shared directory, so login and
memory survive rebuilds and follow you across containers using the same mount.
Forge tokens remain container-local.

For a different mount target, set `CLAUDE_SHARED_CONFIG` to that path before
running the installer.

:::{note} DLS python-copier-template projects
These devcontainers already create and mount the terminal-config directory.
No additional persistence configuration is needed.
:::

## Terminal access

The [devcontainer CLI](https://github.com/devcontainers/cli) can open the same
environment without VS Code:

```bash
devcontainer up --workspace-folder .
devcontainer exec --workspace-folder . bash
```

Inside the container, run `uvx claude-sandbox install` if postCreate has not
already installed it, then `claude`.

:::{note} DLS: install the CLI and select Podman
On the host, load Node if needed, install the CLI under your account, and
select Podman before running the commands above:

```bash
module load node
npm config set prefix ~/.local
npm install -g @devcontainers/cli
export DOCKER_PATH="$(command -v podman)"
```

Ensure `~/.local/bin` is on PATH. See also
[Using the devcontainer CLI](https://epics-containers.github.io/main/how-to/own_tools.html#using-the-devcontainer-cli).
:::

:::{warning} DLS: rootless Podman UID remapping
If container startup fails during UID remapping, add
`"updateRemoteUserUid": false` to `devcontainer.json`, then retry.
:::

After Dockerfile changes, rebuild with
`devcontainer up --workspace-folder . --remove-existing-container`.
There is no `devcontainer down`; use `podman ps` to find the container,
then `podman stop <name>` to stop it.
