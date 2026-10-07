# Launch isolation and updates

The sandbox is created by the wrapper at `/usr/local/bin/claude`,
`codex` or `pi`: a three-line shim that runs the sandbox's root-owned
Python interpreter in isolated mode. It builds the filesystem mounts,
enters the network jail and starts the selected agent. Missing isolation
prerequisites cause launch to fail.

The real agent binaries live off PATH under `/usr/libexec/claude-sandbox`.
Use the wrapper commands to run agents. Directly invoking a vendor binary
bypasses the wrapper; there is no managed prompt or session hook to stop it.
The environment marker `IS_SANDBOX=1` prevents recursive wrapping of nested
agent calls. It is a convention, not independent proof of isolation.

The wrapper names its interpreter by absolute path and runs it with `-I`, so
`PATH`, `PYTHONPATH` and files in the workspace or caches cannot change the
code that builds the sandbox. See
[Sandbox internals](sandbox-internals.md#an-interpreter-the-agent-cannot-redirect).

## Agent updates

Vendor auto-updaters can place an unwrapped binary ahead of the wrapper on
PATH. The installer disables Claude updates with
`env.DISABLE_AUTOUPDATER=1` and `autoUpdates=false` in
`/etc/claude-code/managed-settings.json`. Existing administrator policy is
preserved.

Codex receives `check_for_update_on_startup=false` in
`/etc/codex/managed_config.toml`; the wrapper also sets
`CODEX_UPDATE_DISABLED=1`. A managed file owned by another administrator is
left unchanged with a warning. Pi runs from a read-only standalone package
and skips version checks.

The
[entry-point guard](sandbox-internals.md#the-outer-path-and-the-entry-point-guard)
keeps the names `claude`, `codex`, `pi` and `claude-sandbox` read-only in any
writable directory ahead of the wrapper on PATH.

Update agents by upgrading the image and recreating the container, or by
creating a fresh devcontainer. Reinstalling the sandbox preserves existing
agent binaries. See [Upgrade](../how-to/upgrade.md).

## Verify isolation

Use [explicit verification](../how-to/verify-the-sandbox.md) after installation
or configuration changes. The battery checks the sandbox from inside its
filesystem and network boundaries and reports each result.

User and administrator hooks are their own policy. This installer does not
add, remove or modify them.
