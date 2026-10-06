[![License](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](https://www.apache.org/licenses/LICENSE-2.0)
[![Docs](https://img.shields.io/badge/docs-online-blue.svg)](https://diamondlightsource.github.io/claude-sandbox/)

# claude-sandbox

Run Claude Code, Codex or Pi in a container with a bubblewrap sandbox.
The sandbox isolates host credentials and IDE sockets, limits writable paths,
and blocks access to internal networks except explicitly allowed devices.

## Install and run

On a Linux host with [uv](https://docs.astral.sh/uv/getting-started/installation/)
and rootless Podman:

```bash
uv tool install claude-sandbox
cd ~/src/my-project
claude-sandbox
```

The launcher pulls the matching prebuilt image and starts sandboxed Claude
Code in your project. Log in when prompted. Later runs reuse the project's
container; agent logins and memory persist across container recreation.
No repository clone or devcontainer setup is needed.

Choose another agent with `claude-sandbox codex` or `claude-sandbox pi`.
The host must provide `/dev/net/tun` and support unprivileged user namespaces.
Rootless Docker is untested; rootless Podman is the supported runtime.

[Getting started](https://diamondlightsource.github.io/claude-sandbox/tutorials/getting-started.html)
covers first login and verification.
[Use the container image](https://diamondlightsource.github.io/claude-sandbox/how-to/use-the-container-image.html)
covers configuration, persistence and toolchains.

## Update

On the host:

```bash
uv tool upgrade claude-sandbox
claude-sandbox --recreate
```

Run the second command in each project you want to update. Recreation removes
container-local packages and forge logins; project files and shared agent
settings remain. See [Upgrade](https://diamondlightsource.github.io/claude-sandbox/how-to/upgrade.html).

## Already use a devcontainer?

Inside a Debian/Ubuntu devcontainer, as root:

```bash
uvx claude-sandbox install
claude
```

The container needs `--device=/dev/net/tun`. For automatic installation on
rebuild, follow [Sandbox a team devcontainer](https://diamondlightsource.github.io/claude-sandbox/how-to/sandbox-a-team-devcontainer.html).
[Install without uv](https://diamondlightsource.github.io/claude-sandbox/how-to/install-without-uv.html)
covers the clone fallback.

## What the sandbox protects

Agent tools can be steered by malicious content or make mistakes. The sandbox
limits the damage: the project is writable, host secrets are masked, and the
network jail blocks lateral access to internal hosts. Run `claude-sandbox verify` to check the installed isolation.

The project files and credentials you explicitly share remain accessible to
the agent, and internet access stays open. Read the
[threat model](https://diamondlightsource.github.io/claude-sandbox/explanations/threat-model.html)
for the boundaries and
[architecture](https://diamondlightsource.github.io/claude-sandbox/explanations/architecture.html)
for the implementation.

## Documentation and development

[Documentation](https://diamondlightsource.github.io/claude-sandbox/) includes
guides, configuration reference and design decisions.
[DLS users start here](https://diamondlightsource.github.io/claude-sandbox/dls/claude-at-dls.html).

The sandbox implementation is Bash, migrating to Python (ADR 26); the PyPI
package bundles its launcher and installer. See the [contributing guide](https://diamondlightsource.github.io/claude-sandbox/how-to/contribute.html)
for tests and the isolated documentation toolchain.

## License

See [LICENSE](LICENSE).
