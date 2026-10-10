# What's installed

`uv tool install claude-sandbox` installs the host launcher. The selected
image contains the sandbox below; `uvx claude-sandbox install` installs it
into your own devcontainer.

## Container files

| Path | Purpose |
|---|---|
| `/usr/local/bin/{claude,codex,pi}` | The same three-line wrapper. It runs the sandbox's own interpreter, which selects an agent profile by command name |
| `/usr/libexec/claude-sandbox/python/` | Pinned CPython for the sandbox, pruned to about 60 MB, root-owned and byte-compiled. In the published image it is also the projects' Python: project venvs link to it but cannot change it |
| `/usr/libexec/claude-sandbox/venv/` | The `claude_sandbox` package and nothing else: it has no runtime dependencies |
| `/usr/libexec/claude-sandbox/uv/` | The pinned uv that installed that CPython, kept so a reinstall need not fetch it again. The published image drops it after installation and keeps one uv, its base image's, for projects |
| `/usr/libexec/claude-sandbox/claude` | Claude binary, relocated off PATH |
| `/usr/libexec/claude-sandbox/codex-dist/` | Codex release, including its bundled helpers; read-only inside the sandbox |
| `/usr/libexec/claude-sandbox/pi-dist/` | Standalone Pi executable and assets; read-only inside the sandbox |
| `/usr/libexec/claude-sandbox/pi-run` | Pi launch-marker check; see [its limits](../how-to/use-pi.md#verify-the-sandbox) |
| `/usr/libexec/claude-sandbox/pi-system.md` | System-prompt note that tells Pi about the sandbox it runs in |
| `/usr/libexec/claude-sandbox/codex-launch` | Codex's in-jail launch wrapper |
| `/usr/libexec/claude-sandbox/bin/gh` | The sandbox's `gh`, first on PATH inside the sandbox: runs gh with the [named GitHub token](../how-to/authenticate-with-forges.md#use-several-github-tokens) chosen for the repository |
| `/usr/local/bin/claude-sandbox` | The `claude-sandbox` command, run by the same interpreter: `gh-auth`, `glab-auth`, `update`, `verify`, `pi-local`, `doctor`, `version` |
| `/usr/libexec/claude-sandbox/verify-sandbox-battery.sh` | Installed isolation checks |
| `/usr/libexec/claude-sandbox/skills/` | Shipped skills, mounted read-only into each agent's discovery directory |
| `/usr/libexec/claude-sandbox/statusline-command.sh` | Recommended Claude status line |
| `/usr/libexec/claude-sandbox/pi-sandbox-tag.ts` | Pi footer showing the host and container tag |
| `/usr/libexec/claude-sandbox/version` | Installed release or checkout revision |
| `/usr/libexec/claude-sandbox/installer` | Records a wheel installation for update instructions |
| `/etc/claude-gitconfig` | Curated Git config, refreshed from your identity at agent launch |
| `/etc/claude-code/managed-settings.json` | Disables Claude's updater and makes the `claude-sandbox` plugin marketplace known (installs no plugin); preserves existing administrator settings and hooks |
| `/etc/codex/managed_config.toml` | Disables Codex startup update checks; an administrator-owned file is left unchanged with a warning |
| `/etc/claude-sandbox.conf` | [Sandbox configuration](configuration.md) |

The installer adds `passt` (providing `pasta`) for the network jail. Custom
devcontainers must supply `/dev/net/tun` through `runArgs`; the host launcher
does this automatically.

Wrappers are installed even if an optional agent download is skipped or fails.
They report a missing binary rather than falling through to an unwrapped
agent. Use `WITH_CODEX=0` or `WITH_PI=0` at installation to skip those downloads;
`PI_VERSION` pins Pi's release. Reinstalling preserves existing agent binaries.
See [Upgrade](../how-to/upgrade.md).

## Claude-only installation

`uvx claude-sandbox install --minimal` (or `CLAUDE_SANDBOX_MINIMAL=1`) installs
the sandbox for Claude alone. The VS Code extension installs this way.
It implies `WITH_CODEX=0 WITH_PI=0`, and it also leaves out:

- `codex-dist/`, `codex-launch`, `pi-dist/`, `pi-run`, `pi-system.md` and
  `pi-sandbox-tag.ts` under `/usr/libexec/claude-sandbox/`;
- `/etc/codex/managed_config.toml`;
- `~/.codex/` and `~/.pi/agent/`, and their links into the shared config;
- Debian's `nodejs`, which no agent needs. The shipped browser-testing skill
  does run `node` (with `npm`): install Node.js yourself if you use it;
- apt entirely, `apt-get update` included, when every package it needs and
  `glab` are already installed. Packages already present are then not
  upgraded. Otherwise apt runs as in a full installation, which also tries
  to install `glab` where the distribution has it.

Claude's side is the same as a full installation's, file for file: the same
wrappers under all three names, interpreter, managed settings, configuration and
isolation checks. `codex` and `pi` still reach the wrapper, which reports that
the agent is not installed. `claude-sandbox doctor` does not count the missing
Pi footer as a problem.

A later installation without `--minimal` adds Codex and Pi. A `--minimal`
reinstallation over a full one keeps the installed agents and their files
current; it removes nothing.

Measured on a fast network (one run each, 2026-10-10; the installer's own steps
after the interpreter is provisioned, apt downloads only):

| Step | Full | `--minimal` |
|---|---|---|
| apt packages to fetch on a bare Ubuntu 26.04 base | 65 debs, 66 MB | 42 debs, 35 MB |
| Claude download | 10–14 s | 10–11 s |
| Codex download and copy | 4.5 s | skipped |
| Pi download | 1.2–1.6 s | skipped |
| Installer total | 18–22 s | 11.5–13.5 s |
| Reinstall with packages present: apt | `update` and `install` (about 2 s) | skipped |

The Claude download dominates what remains, and every installation needs it.

## User state

Each agent sees only its own state, plus the shared skills and forge stores.
With `/user-terminal-config` mounted, agent state and shared skills persist
across rebuilds; forge tokens remain container-local.

| Path | Installer behaviour |
|---|---|
| `~/.claude/`, `~/.claude.json` | Preserve login, settings and hooks; seed a status line only if absent |
| `~/.codex/` | Create if absent; preserve configuration, credentials and sessions |
| `~/.pi/agent/` | Preserve Pi settings, credentials, extensions and sessions |
| `~/.agents/skills/` | Shared writable skills; created if absent, with Claude discovery through symlinks |

`claude-sandbox doctor --fix` replaces the Claude status line with the
recommended version, backing up changed files. See
[container tags](../how-to/use-the-container-image.md#tell-containers-apart) and
[shared skills](../how-to/share-skills-between-agents.md).

## Optional tools

The published image includes Python, uv, Node.js, npm and Vim. Custom
containers keep their own toolchain choices.

The image's Python is the sandbox's own pinned interpreter, and uv there
never downloads another on its own (`UV_PYTHON_DOWNLOADS=manual`). For a
project that needs a different version, run `uv python install 3.12` (for
example) from `claude-sandbox shell`, then recreate the project's venv; an
agent session cannot install one. Don't run `uv python uninstall 3.13` from
that shell: it removes the sandbox's interpreter and every agent launch
fails. `uvx claude-sandbox --recreate` recovers it.

Shipped skills provide installers for optional tools, run outside the agent:

- [Browser automation](../how-to/browser-automation.md): Playwright and Chromium.
- [VS Code automation](../how-to/vscode-automation.md): VS Code, Xvfb and a UI driver.

Browser downloads persist under `/cache/ms-playwright`. After setup,
`chromium` also works from an outer container terminal with a display;
its profile lives under `/cache/chromium-home`.
