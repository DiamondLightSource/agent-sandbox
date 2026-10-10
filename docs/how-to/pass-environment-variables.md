# Pass environment variables in

The sandbox clears the environment and forwards a small built-in allowlist.
A variable set in your container terminal is not automatically available to
the agent.

## Forward a variable

Add names to the sandbox config:

```ini
pass-env = MY_SERVICES_PATH, MY_FIXTURE_DIR
```

With the PyPI launcher, use the host's `~/.config/claude-sandbox.conf`;
see [container configuration](use-the-container-image.md#configure-the-sandbox)
for mounting it. In your own devcontainer, edit `/etc/claude-sandbox.conf`
outside the agent.

These are names, not assignments. Values must exist in the environment that
launches the agent **inside the container**. The host launcher forwards
`CLAUDE_SANDBOX_*` variables at container creation; it does not forward
arbitrary host variables. To supply one interactively:

In your devcontainer terminal, skip the first line and run the second directly.

```bash
claude-sandbox shell        # Skip if already in your devcontainer terminal
MY_FIXTURE_DIR=/workspaces/fixtures CLAUDE_SANDBOX_PASS_ENV=MY_FIXTURE_DIR claude
```

Exit the shell when finished if you opened it with the host launcher.
Unset variables are skipped.

## What crosses the boundary

Every forwarded value is readable by the agent and its tools. Avoid secrets
and inspect values before allowing them. A variable naming a socket does
not make that socket safe to expose; see
[Make extra paths writable](configure-workspace-scope.md#sockets-grant-access-to-services).

The sandbox rejects overrides for its own environment and loader or shell
startup hooks, including `PATH`, `HOME`, `IS_SANDBOX`, `LD_*` and
`BASH_ENV`. See the [deny-list](../reference/configuration.md#pass-env-deny-list).

`TMPDIR` is not on the deny-list. Forwarding it replaces the agent's own
`/var/tmp/<agent>-agent` temp root, which stays bound; the value you forward
must name a path the agent can write, such as the workspace.

Config changes apply on the next agent launch. Devcontainer reinstalls restore
the shipped config, so reapply custom settings in
[postCreate](sandbox-a-team-devcontainer.md).
