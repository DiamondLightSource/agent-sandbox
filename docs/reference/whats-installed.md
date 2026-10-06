# What's installed

`uv tool install claude-sandbox` installs the host launcher. The selected
image contains the sandbox below; `uvx claude-sandbox install` installs it
into your own devcontainer.

## Container files

| Path | Purpose |
|---|---|
| `/usr/local/bin/{claude,codex,pi}` | The same three-line wrapper. It runs the sandbox's own interpreter, which selects an agent profile by command name |
| `/usr/libexec/claude-sandbox/python/` | Pinned CPython for the sandbox, pruned to about 55 MB, root-owned and byte-compiled |
| `/usr/libexec/claude-sandbox/venv/` | The `claude_sandbox` package and nothing else: it has no runtime dependencies |
| `/usr/libexec/claude-sandbox/claude` | Claude binary, relocated off PATH |
| `/usr/libexec/claude-sandbox/codex-dist/` | Codex release, including its bundled helpers; read-only inside the sandbox |
| `/usr/libexec/claude-sandbox/pi-dist/` | Standalone Pi executable and assets; read-only inside the sandbox |
| `/usr/libexec/claude-sandbox/pi-run` | Pi launch-marker check; see [its limits](../how-to/use-pi.md#verify-the-sandbox) |
| `/usr/local/bin/claude-sandbox` | The `claude-sandbox` command, run by the same interpreter: `gh-auth`, `glab-auth`, `update`, `verify`, `pi-local`, `doctor`, `alerts`, `version` |
| `/usr/libexec/claude-sandbox/verify-sandbox-battery.sh` | Installed isolation checks |
| `/usr/libexec/claude-sandbox/skills/` | Shipped skills, mounted read-only into each agent's discovery directory |
| `/usr/libexec/claude-sandbox/statusline-command.sh` | Recommended Claude status line |
| `/usr/libexec/claude-sandbox/pi-sandbox-tag.ts` | Pi footer showing the host and container tag |
| `/usr/libexec/claude-sandbox/version` | Installed release or checkout revision |
| `/usr/libexec/claude-sandbox/installer` | Records a wheel installation for update instructions |
| `/etc/claude-gitconfig` | Curated Git config, refreshed from your identity at agent launch |
| `/etc/claude-code/managed-settings.json` | Disables Claude's updater; preserves existing administrator settings and hooks |
| `/etc/codex/managed_config.toml` | Disables Codex startup update checks; an administrator-owned file is left unchanged with a warning |
| `/etc/claude-sandbox.conf` | [Sandbox configuration](configuration.md) |
| `/etc/profile.d/claude-sandbox-alerts.sh` | Warns at outer shell prompts about what the PATH watcher quarantined; sourced from `/etc/bash.bashrc` and `/etc/zsh/zshrc` ({ref}`ADR 27 <adr-outer-path-guard>`) |
| `/run/claude-sandbox/` | The PATH watcher's alerts and baselines, hidden inside the sandbox; `claude-sandbox alerts` lists them |

<!-- TODO(phase5): confirm the claude-sandbox entry point, passt, WITH_CODEX, WITH_PI and PI_VERSION against the wired-in Python installer -->

The installer adds `passt` (providing `pasta`) for the network jail. Custom
devcontainers must supply `/dev/net/tun` through `runArgs`; the host launcher
does this automatically.

Wrappers are installed even if an optional agent download is skipped or fails.
They report a missing binary rather than falling through to an unwrapped
agent. Use `WITH_CODEX=0` or `WITH_PI=0` at installation to skip those downloads;
`PI_VERSION` pins Pi's release. Reinstalling preserves existing agent binaries.
See [Upgrade](../how-to/upgrade.md).

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

Shipped skills provide installers for optional tools, run outside the agent:

- [Browser automation](../how-to/browser-automation.md): Playwright and Chromium.
- [VS Code automation](../how-to/vscode-automation.md): VS Code, Xvfb and a UI driver.

Browser downloads persist under `/cache/ms-playwright`. After setup,
`chromium` also works from an outer container terminal with a display;
its profile lives under `/cache/chromium-home`.
