# Getting started

Install the launcher from PyPI, then run it in the project you want Claude
Code to work on. The launcher creates the container and sandbox for you.

Already use a project devcontainer? [Installing into it](#already-use-a-devcontainer)
is often preferable because the agent gets your project's existing tooling.

## 1. Install on your host

You need Linux, [uv](https://docs.astral.sh/uv/getting-started/installation/),
rootless Podman, `/dev/net/tun`, and unprivileged user namespaces.
Check that Podman runs rootless:

```bash
podman info --format '{{.Host.Security.Rootless}}'
```

It should print `true`. Rootless Docker is untested; use Podman.

In a host terminal, outside any container:

:::{note} DLS workstations
If uv is not on PATH, run `module load uv` in the host terminal first.
For DLS policy and forge access, see [Claude Code at DLS](../dls/claude-at-dls.md).
:::

```bash
uv tool install claude-sandbox
```

If uv reports that its executable directory is missing from `PATH`, run
`uv tool update-shell` and open a new terminal.

## 2. Run Claude

```bash
cd ~/src/my-project
claude-sandbox
```

The first run pulls the image for your installed package version and creates
a project container. Claude runs inside its sandbox with the project writable.
Log in when prompted, using the code-paste flow if needed.

Ask Claude to inspect the project or make a small change. Review the diff as
you normally would: the sandbox limits access, but the agent can still edit
or delete files in the project.

## 3. Check the sandbox

Exit Claude and run from the same project directory:

```bash
claude-sandbox verify
```

This runs the checks inside the sandbox without starting an agent or needing
a login. The same command works in your own devcontainer terminal.
See [Verify the sandbox](../how-to/verify-the-sandbox.md) to interpret results.

## Keep working

Run `claude-sandbox` from the same project to start another session in its
existing container. Agent login and memory are shared through
`~/.config/terminal-config`; forge tokens stay in the project container.

- To use another agent: `claude-sandbox codex` or `claude-sandbox pi`.
- To enable push access: [Authenticate with forges](../how-to/authenticate-with-forges.md).
- To update: [Upgrade claude-sandbox](../how-to/upgrade.md).
- For mounts, configuration and toolchains: [Use the container image](../how-to/use-the-container-image.md).

## Already use a devcontainer?

This keeps the agent in your project's own tooling environment. Install inside
your existing Debian/Ubuntu devcontainer as root:

```bash
uvx claude-sandbox install
claude
```

Add `"--device=/dev/net/tun"` to its `runArgs` and rebuild before launching
an agent. Inside the container, `claude`, `codex` and `pi` launch sandboxed
agents; `claude-sandbox` runs the helpers, such as `verify` and `gh-auth`.

Throughout these guides, `claude-sandbox shell` just opens a container terminal.
If you are already in your devcontainer terminal, skip that step and the matching
`exit`. The commands between them are identical and run outside an agent session.

For the full setup, including installation on rebuild and login persistence,
see [Set up a devcontainer](set-up-a-devcontainer.md) and
[Sandbox a team devcontainer](../how-to/sandbox-a-team-devcontainer.md).
If uv is unavailable, see [Install without uv](../how-to/install-without-uv.md).
