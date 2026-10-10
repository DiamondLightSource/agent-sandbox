# Deliberately exposed and out of scope

Paths below are inside the container. The host launcher mounts the project,
shared agent state, Git identity and explicitly requested paths. `--peers`
also makes sibling checkouts readable to agents. See
[container mounts](../how-to/use-the-container-image.md#extra-paths).

## Deliberately exposed

`r` means read-only; `rw` means read-write. These paths are restored after the
home and runtime masks described in [Architecture](../explanations/architecture.md).

| Path or interface | Mode | Purpose and limits |
|---|---|---|
| Workspace | rw | Defaults to the agent's launch directory. `workspace-root` widens it; `allow-write` adds paths |
| `/etc/claude-sandbox.conf` | r | Launch policy outside the writable workspace |
| `/etc/claude-gitconfig` | r | Git identity, HTTPS rewrites and forge credential helpers |
| `/etc/gitconfig` | r | Outer system config remains readable; ordinary Git calls ignore it through `GIT_CONFIG_SYSTEM=/dev/null` |
| `~/.claude/`, `~/.claude.json` | rw | Claude's login, settings, hooks and memory; Claude sessions only |
| `~/.codex/` | rw | Codex's state; Codex sessions only. Its `packages` directory is masked |
| `~/.pi/` | rw | Pi's state, including all configured provider credentials; Pi sessions only |
| `~/.agents/skills/` | rw | Shared across agents and projects using the same terminal config; any agent can alter skills another later loads |
| `/var/tmp/claude-agent/` | rw | Claude's temp root (`CLAUDE_CODE_TMPDIR`), including its scratchpad; Claude sessions only. Container-local, so per project and visible from the outer shell. `/tmp` itself stays a private tmpfs |
| `~/.cache/` | rw | Tool caches, if present |
| `~/.config/{gh,glab-cli}/` | rw | Forge tokens, including named GitHub tokens and their repository cache under `gh/scoped/`; omitted with `no-forge`. Other `.config` directories remain hidden |
| `~/.local/share/` | rw | Tool data and plugins. `applications/` and `claude/` are masked to keep Claude's runtime writes temporary |
| `~/.local/bin/{uv,uvx}` | rw | Individual tool binaries; the rest of the directory stays temporary |
| `/usr/libexec/claude-sandbox/` | r | Agent binaries, the sandbox's Python interpreter and venv, and shipped skills. Claude is also bound at `~/.local/bin/claude` |
| Configured devices | rw | `allow-device` exposes hardware and its driver interface |
| Network | — | Internet, DNS, gateway, allowed IPs and configured loopback relays; private networks otherwise blocked by default |

Agent state and shared skills persist through `/user-terminal-config` when
mounted. Forge tokens stay in the project container. See
[configuration](configuration.md) for overrides and
[Sandbox internals](../explanations/sandbox-internals.md) for bind details.

## Out of scope

| Exposure | What to do |
|---|---|
| Secrets in the workspace or readable mounts | Keep secrets outside those paths; read-only access still permits disclosure |
| Overpowered forge tokens | Use short-lived, project-scoped tokens with only required permissions |
| Credentials stored in custom locations, `.local/share` or caches | Audit tool storage and container mounts |
| Internet exfiltration | Apply an external egress policy if needed; this jail does not filter domains |
| Host kernel or device-driver exploits | Keep the host patched and grant device access only to trusted workloads |
| Device resource exhaustion or device side effects | Limit which hardware the workload can access; raw disks bypass filesystem protections |
| Deliberate wrapper bypass by the operator | Apply separate organisational controls where required |

The supported setup is a root user inside rootless Podman on Linux.
Non-root devcontainers and rootful Docker are outside that setup. The
[threat model](../explanations/threat-model.md) explains these boundaries.
