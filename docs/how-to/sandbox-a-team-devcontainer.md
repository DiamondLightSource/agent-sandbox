# Sandbox a team devcontainer

Use this route when the agent needs your project's existing Debian/Ubuntu
toolchain. For a standalone agent container, use the
[PyPI quick start](../tutorials/getting-started.md).

The devcontainer must run as root under rootless Podman and have uv available.

## Install on rebuild

Add this to `.devcontainer/postCreate.sh`:

```bash
#!/usr/bin/env bash
set -euo pipefail
uvx claude-sandbox==5.0.0 install
```

Pin the release so upgrades are reviewed with the project.
`uvx` is appropriate here: it runs the packaged installer once. The
installer places the agent wrappers and the `claude-sandbox` command on the
container's PATH, with a root-owned Python interpreter of their own under
`/usr/libexec/claude-sandbox/`; the sandbox does not run from uv's cache.

Merge these settings into `.devcontainer/devcontainer.json`:

```json
"postCreateCommand": "bash .devcontainer/postCreate.sh",
"runArgs": ["--device=/dev/net/tun"]
```

Preserve existing commands and run arguments. Rebuild, then run `claude`,
`codex` or `pi` in a container terminal.
For login persistence, add the
[terminal-config mount](../tutorials/set-up-a-devcontainer.md#persist-agent-logins).
Without uv, use the [clone fallback](install-without-uv.md).

:::{note} DLS: install across your devcontainers
`python-copier-template` devcontainers source `/user-terminal-config/bashrc`.
For personal use, add `uvx claude-sandbox install` to its run-once section
to install on the first shell in each container. Each container still needs
`/dev/net/tun`. For a shared, reviewed release pin, use the project postCreate
recipe above.
:::

## Team configuration

Apply settings **after** the installer, which restores the shipped defaults.
For additions, append lines in `postCreate.sh`:

```bash
cat >> /etc/claude-sandbox.conf <<'EOF'
allow-ip = 192.168.1.50
EOF
```

For a complete replacement, install a reviewed team config after installation:

```bash
install -m 0644 .devcontainer/claude-sandbox.conf /etc/claude-sandbox.conf
```

Retain any shipped defaults your team needs. The runtime reads the copy under
`/etc`, which the agent cannot change. Review changes to the source config,
postCreate script and version pin before rebuilding: postCreate runs as root
outside the agent sandbox.

## Verify and update

From a container terminal:

```bash
claude-sandbox version
claude-sandbox verify
```

To upgrade, change the PyPI version in postCreate and rebuild.
See [Upgrade](upgrade.md) for the distinction between sandbox and agent updates.
