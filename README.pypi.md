# claude-sandbox

Run Claude Code, Codex or Pi in a container with a bubblewrap sandbox that
isolates host credentials and limits access to internal networks.

On a Linux host with uv and rootless Podman:

```bash
uv tool install claude-sandbox
cd ~/src/my-project
claude-sandbox
```

Log in when prompted. Use `claude-sandbox codex` or `claude-sandbox pi`
to choose another agent. The host needs `/dev/net/tun` and unprivileged user
namespaces; no devcontainer setup is required.

Update with `uv tool upgrade claude-sandbox`, then
`claude-sandbox --recreate` in each project. Recreation removes
container-local packages and forge logins, but retains project files and
shared agent settings.

Already inside a Debian/Ubuntu devcontainer? As root, run
`uvx claude-sandbox install`, then `claude`. That container must expose
`/dev/net/tun`.

The package bundles the project's Bash launcher and installer; its version
selects the matching container image.
[Documentation](https://diamondlightsource.github.io/claude-sandbox/) ·
[Source](https://github.com/DiamondLightSource/claude-sandbox)
