# Upgrade claude-sandbox

## Installed from PyPI on your host

```bash
uv tool upgrade claude-sandbox
cd ~/src/my-project
claude-sandbox --recreate
```

The package selects the matching image version. Existing project containers
keep their old image until recreated, so repeat the second step in each
project you want to update.

Recreation removes the container's local packages, caches and forge logins.
Project files and agent settings in `~/.config/terminal-config` survive.
Exit active sessions before recreating, then
[authenticate to forges](authenticate-with-forges.md) again if needed.

Check the installed launcher with `claude-sandbox --version`.
For a fixed version, install with `uv tool install claude-sandbox==5.0.0`;
change that constraint explicitly to move to another release.

If you use the one-off launcher instead of a tool install:

```bash
uvx claude-sandbox@latest --recreate
```

See [uv's tool guide](https://docs.astral.sh/uv/guides/tools/#upgrading-tools)
for package upgrade behaviour.

## Installed into your own devcontainer

Inside the container, as root:

```bash
uvx claude-sandbox@latest install
claude-sandbox version
```

For a team, update the version in
[postCreate](sandbox-a-team-devcontainer.md) and rebuild.
Reapply custom `/etc/claude-sandbox.conf` settings after installation.

The installer keeps existing agent binaries. Updating the sandbox does not
itself guarantee a newer agent; a fresh devcontainer installs the current
agents. The published image supplies the agents baked into that image.

For a clone installation, `claude-sandbox update` fetches and installs
the newest release. For a wheel installation it prints the PyPI update
instructions. In the published image it refuses: upgrade from the host.

Agent auto-updaters are disabled to preserve the sandbox wrapper.
See [Launch isolation and updates](../explanations/launch-isolation.md) for the rationale.

## Upgrading from 4.x

Release 5.0.0 replaces the Bash implementation with a Python one
({ref}`ADR 26 <adr-python-implementation>`). Commands, launcher options and
`/etc/claude-sandbox.conf` keep their meaning. Upgrade as above; in your own
devcontainer the installer also places a pinned Python interpreter, about
55 MB, under `/usr/libexec/claude-sandbox/`. One helper behaves
differently: `gh-auth` and `glab-auth` now refuse inside an agent session,
as `update` does. Run them from the host or a container terminal.

The `container/claude-container` script is gone. If you ran it from a
clone, install the launcher from PyPI instead, with uv or
[without it](install-without-uv.md#host-launcher).
