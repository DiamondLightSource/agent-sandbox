# Configuration

The sandbox reads `/etc/claude-sandbox.conf` inside the container and
`CLAUDE_SANDBOX_*` environment variables. With the PyPI host launcher, create
`~/.config/claude-sandbox.conf` on the host; it is mounted read-only at that
container path. See [mounting the config](../how-to/use-the-container-image.md#configure-the-sandbox).

`claude-sandbox` is one command in every context. On the host it launches
project containers, and forwards helpers such as `verify`, `gh-auth` and
`version` into the project container. Inside the container it runs those
helpers. Inside an agent session, helpers that would take a credential or
change the installation refuse. Run `claude-sandbox --help` in each place
for the commands available there; on the host it also lists the launcher
options.

## `/etc/claude-sandbox.conf`

The wrapper reads this file at each launch. It is outside the writable
workspace, so an agent cannot change future sessions' access.

For your own devcontainer, edit it in a container terminal outside the agent.
Rebuilds and reinstalls restore shipped defaults; apply durable changes
through [team setup](../how-to/sandbox-a-team-devcontainer.md).

### Format

- One directive per line.
- `key = value`, or a bare `key` for boolean flags.
- Blank lines and `#` comments are ignored.
- Environment variables take precedence for scalar settings such as
  `workspace-root`. Repeatable lists such as `allow-write` and `local-port`
  append config entries to the environment values.

### Keys

| Key | Value | Effect |
|---|---|---|
| `workspace-root` | absolute path | Sets the rw bind-mount root if `CLAUDE_SANDBOX_WORKSPACE_ROOT` is not already set. Empty value ignored |
| `no-forge` | bare flag (no value) | Equivalent to `CLAUDE_SANDBOX_NO_FORGE=1`: skips the `gh`/`glab` token binds and removes the credential helpers from the generated gitconfig |
| `allow-write` | absolute path | Adds a writable path; repeatable. Empty or missing paths are skipped. Never expose host container-engine, session-bus or X11 sockets: they grant control beyond the sandbox. See [socket access](../how-to/configure-workspace-scope.md#sockets-grant-access-to-services) |
| `pass-env` | variable name(s) | Forwards named environment variables through the `--clearenv` scrub. Comma- or space-separated, and repeatable. Names only — the value is read from the launching environment. Unset, non-identifier and denied names are skipped |
| `allow-device` | absolute `/dev/…` path | Exposes one existing character or block device read-write to agents using a device bind. Repeatable; merged with the environment. Symlinks resolve to their canonical path. Missing or invalid devices fail launch. Container access must already be configured; the host launcher `--device PATH` does both steps |
| `local-model-port` | TCP port 1–65535; shipped default `1920`; `0` drops it; bare flag means `1920` | The port Pi discovers a model on at startup. Always part of the loopback relay set ({ref}`adr-local-port-all-agents`), so every agent reaches it on its own `127.0.0.1`. Environment `CLAUDE_SANDBOX_LOCAL_MODEL_PORT` takes precedence. See [Use Pi](../how-to/use-pi.md) |
| `local-port` | TCP port 1–65535 | Adds a port to the loopback relay set: the outer container's `127.0.0.1:<port>` becomes reachable at the same address inside the jail, for every agent. Repeatable — one port per line. Merged with `CLAUDE_SANDBOX_LOCAL_PORTS` from the environment and deduplicated. Each relayed service is exposed in full. Requires the network jail and `socat`. See [Reach services on the host's loopback](../how-to/network-egress-jail.md#reach-services-on-the-hosts-loopback) |
| `callback-port` | TCP port 1–65535; disabled by default (commented examples) | Reverse relay: exposes an agent's loopback listener on the outer container's loopback for browser OAuth. Repeatable; merged with `CLAUDE_SANDBOX_CALLBACK_PORTS`. Cannot overlap `local-port` or `local-model-port`. Occupied host ports are skipped with a warning. See [browser logins](../how-to/network-egress-jail.md#let-a-browser-login-reach-the-agent) |
| `egress-jail` | bare flag / `1` reaffirms on | Network isolation is on by default and refuses launch if prerequisites are missing. Environment override takes precedence; see the [threat model](../explanations/threat-model.md#the-egress-jail-and-the-native-sandbox) for disabling it |
| `allow-ip` | bare IP (no CIDR) | Allows an IP through the network jail; repeatable. Grants access to the whole device, not one service. No effect with the jail disabled |

```ini
# .devcontainer/claude-sandbox.conf  (installed to /etc/claude-sandbox.conf)
allow-write = /cache
allow-write = /workspaces/sibling-project
pass-env = DOCKER_HOST

# Keep these device IPs reachable past the RFC1918 blackhole (bare IP):
allow-ip = 172.23.142.119  # internal GitLab
```

### `pass-env` deny-list

These names are ignored, and the sandbox's own value always wins:

| Names | Why |
|---|---|
| `PATH`, `HOME`, `USER`, `IS_SANDBOX`, `GIT_CONFIG_GLOBAL`, `GIT_CONFIG_SYSTEM` | The sandbox sets each of these itself. Overrides could bypass wrapper controls |
| `CODEX_HOME`, `CLAUDE_CONFIG_DIR`, `CLAUDE_SANDBOX_AGENT`, `IS_SANDBOX_AGENT` | Agent configuration locations and profile selection remain controlled by the wrapper |
| `LD_*`, `BASH_ENV`, `ENV`, `SHELLOPTS`, `BASHOPTS`, `IFS` | Loader and shell startup hooks — they execute code in every process the session spawns |

## Environment variables

With the host launcher, `CLAUDE_SANDBOX_*` variables are forwarded at container
creation, apart from the launcher's own settings such as
`CLAUDE_SANDBOX_IMAGE` and `CLAUDE_SANDBOX_ENGINE`; recreate to change them.
Other host variables are not forwarded.
Inside your own devcontainer, set variables in the launching terminal or
`remoteEnv`. Config-file settings are usually simpler for durable host-launcher
configuration.

| Variable | Set by / read by | Meaning |
|---|---|---|
| `CLAUDE_SANDBOX_WORKSPACE_ROOT` | you (`remoteEnv`) → shadow | Explicit rw bind-mount root. Set an absolute path, such as `/workspaces`, to widen it. Default: `$PWD` |
| `CLAUDE_SANDBOX_NO_FORGE` | you (`remoteEnv`) → shadow | `1` skips the `gh`/`glab` token binds and drops the credential helpers from the generated gitconfig, so `git push` fails inside the sandbox by design |
| `CLAUDE_SANDBOX_EGRESS_JAIL` | you (env, per session) / conf `egress-jail` → shadow | Overrides `egress-jail`; enabled by default. See the key above for prerequisites and limits |
| `CLAUDE_SANDBOX_ALLOW_IP` | populated by `parse_config` from `allow-ip` lines | Newline-separated device IPs the jail keeps reachable past the RFC1918 blackhole |
| `CLAUDE_SANDBOX_LOCAL_PORTS` | you (env, per session) and `parse_config` from `local-port` lines | Extra outer-loopback TCP ports relayed into the jail, space-, comma- or newline-separated. Conf entries are appended to whatever the environment already holds, so `CLAUDE_SANDBOX_LOCAL_PORTS=8082 claude` adds one port for one session ({ref}`adr-local-port-all-agents`) |
| `CLAUDE_SANDBOX_LOCAL_MODEL_PORT` | you (env, per session) / conf `local-model-port` → shadow, and into the sandbox for Pi | The model port Pi discovers on; always in the relay set. `0` drops it. Env wins over conf |
| `CLAUDE_SANDBOX_CALLBACK_PORTS` | you (env, per session) and `parse_config` from `callback-port` lines | Outer-loopback TCP ports relayed into the jail for browser OAuth callbacks, space-, comma- or newline-separated. Conf entries are appended to the environment's, so `CLAUDE_SANDBOX_CALLBACK_PORTS=1455 codex` adds one port for one session ({ref}`adr-callback-port-relay`) |
| `IS_SANDBOX` | set by bwrap (`--setenv IS_SANDBOX 1`) | Marker preventing recursive wrapping of nested agent calls. It is not independent proof of isolation |
| `CLAUDE_SANDBOX_ALLOW_WRITE` | populated by `parse_config` from `allow-write` lines | Newline-separated extra writable paths bound in addition to the workspace |
| `CLAUDE_SANDBOX_ALLOW_DEVICES` | launcher `--device` / conf `allow-device` → shadow | Newline-separated device paths to expose inside the jail; setting this alone does not mount devices into the outer container |
| `CLAUDE_SANDBOX_PASS_ENV` | populated by `parse_config` from `pass-env` lines | Names of environment variables to forward into the sandbox. Set it directly to forward a variable for one session without editing the conf |
| `DISABLE_AUTOUPDATER` | set to `1` in managed settings by the installer | Disables Claude Code's in-container auto-updater (alongside `autoUpdates:false`) so a self-update can't re-arm the unwrapped-launch bypass |

`CLAUDE_SANDBOX_NO_FORGE` is documented as a task in
[run a no-push session](../how-to/authenticate-with-forges.md#run-without-push-access); workspace scope
is covered in [widen the writable workspace](../how-to/configure-workspace-scope.md);
forwarding variables is covered in
[pass environment variables in](../how-to/pass-environment-variables.md).
