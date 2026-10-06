# Architecture

The sandbox is one Python package, `claude_sandbox`, that builds argument
lists and runs `bwrap`, `unshare`, `pasta` and `script`. The same package
is the host launcher, the in-container helper and the agent wrapper, and it
uses the Python standard library only.

On the host, `claude-sandbox` starts a project container. Inside it, the
wrapper launches Claude, Codex or Pi in a bubblewrap jail. A custom
devcontainer uses the same wrapper.

## Launch sequence

1. The shell resolves `claude`, `codex` or `pi` to a three-line shim in
   `/usr/local/bin`. The vendor binaries live off PATH under
   `/usr/libexec/claude-sandbox`.
2. The shim runs the sandbox's own interpreter by absolute path, in
   isolated mode:
   `/usr/libexec/claude-sandbox/venv/bin/python -I -m claude_sandbox _shadow`.
   Isolated mode ignores `PYTHON*` variables, the user's site-packages and
   the current directory, so nothing the agent can write changes the code
   that launches it.
3. The wrapper reads `/etc/claude-sandbox.conf`, refreshes the curated Git
   config from your name and email, and selects the agent's profile.
4. It creates a private network namespace, attaches `pasta` for internet
   access, and installs the routing restrictions.
5. It starts bubblewrap with the filesystem mounts, scrubbed environment,
   dropped capabilities and separate PID, IPC and UTS namespaces.
6. The agent runs inside the jail. A `script(1)` pseudo-terminal separates
   its terminal input from the outer shell.

<!-- TODO(phase5): confirm against fix/entry-point-guard -->
While the session runs, a watcher outside the jail quarantines executables
the session adds ahead of system commands on PATH, and new Git hooks. It
warns in outer shells and when the session ends. See
[Sandbox internals](sandbox-internals.md#the-entry-point-guard).

Missing isolation prerequisites cause launch to fail. Nested agent calls use
`IS_SANDBOX=1` to avoid wrapping again; that marker alone is not proof of
isolation. See [verification](../how-to/verify-the-sandbox.md).

## Filesystem and credentials

The container filesystem starts read-only. Empty temporary filesystems cover
`$HOME`, `/tmp` and available runtime/secret directories. The wrapper then
binds back the workspace, the selected agent's state, shared skills, tool data
and configured extra paths.

Each agent sees its own login store. Forge tokens are deliberately available
unless `no-forge` is set. Other home-directory credentials, including SSH keys,
remain hidden. The exact paths are in
[Deliberately exposed](../reference/deliberately-exposed.md); the bind rationale
is in [Sandbox internals](sandbox-internals.md).

## Network and configuration

The default network jail blocks private networks, connected subnets and
link-local destinations, with exceptions for the gateway, DNS and configured
`allow-ip` addresses. Internet access remains available. See the
[threat model](threat-model.md#the-egress-jail-and-the-native-sandbox) for its limits.

Configuration lives under `/etc`, outside the writable workspace. An agent
cannot edit it to widen the next session's filesystem or network access.
Change it from a container terminal, or through the host launcher's mounted
[configuration file](../reference/configuration.md).

## One command in three places

`claude-sandbox` works out once whether it runs on the host, in a container
outside the jail, or inside an agent session. Each command declares where it
runs:

- On the host, it creates and enters the project container (`claude`,
  `codex`, `pi`, `shell`, `clean`).
- Helpers such as `verify`, `gh-auth` and `doctor` run in the container.
  Typed on the host, they are forwarded into the project container and run
  by the version installed there.
- Inside an agent session, helpers that would take a credential or change
  the installation refuse.

## Installation and updates

The PyPI package, the published image and custom devcontainers use the same
Python installer. It places a pinned CPython (about 55 MB after pruning)
and a venv holding only this package under `/usr/libexec/claude-sandbox/`,
root-owned and byte-compiled. The interpreter never runs from uv's cache,
which is writable from inside the jail. Projects reference the installer
rather than copying security code into their own repositories.
[Architecture decisions](decisions.md) record the history;
{ref}`ADR 26 <adr-python-implementation>` covers the move from Bash to Python.

Vendor auto-updaters are disabled to preserve the wrapper on PATH. Update
through the image or devcontainer installation; see
[Launch isolation and updates](launch-isolation.md).

## Where the code lives

| Path | Purpose |
|---|---|
| `src/claude_sandbox/bwrap.py` | The bwrap argv: every mount and environment variable the jail gets |
| `src/claude_sandbox/jail.py` | The network jail: namespace holder, `pasta`, DNS and loopback relays |
| `src/claude_sandbox/shadow.py` | One launch, top to bottom: checks, configuration, then the exec |
| `src/claude_sandbox/cli.py`, `context.py`, `host/`, `helpers/` | The `claude-sandbox` command on the host, in the container and in the jail |
| `src/claude_sandbox/installer/` | Agent installation, the interpreter, wrappers and configuration |
| `.devcontainer/claude-sandbox/claude-shim` | The three-line shim installed as `claude`, `codex` and `pi` |
| `.devcontainer/claude-sandbox/verify-sandbox-battery.sh` | Deterministic isolation checks (Bash, run inside the jail) |
| `skills/verify-sandbox/` | Adversarial audit instructions and check rationale |

The [contributor guide](../how-to/contribute.md#the-python-package) describes
the modules in more detail.
