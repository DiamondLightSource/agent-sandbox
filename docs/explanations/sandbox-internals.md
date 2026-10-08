# Sandbox internals

[Architecture](architecture.md) describes the launch sequence. This page
explains the less obvious filesystem, namespace and interpreter choices.

## An interpreter the agent cannot redirect

A Python launcher can be steered by `PATH`, `PYTHONPATH`, `sitecustomize`,
`usercustomize` and `.pth` files, several of which an agent could write.
The sandbox closes each route:

- `/usr/local/bin/claude`, `codex` and `pi` are one root-owned, three-line
  Bash shim. It execs `/usr/libexec/claude-sandbox/venv/bin/python -I -m
  claude_sandbox _shadow NAME -- ARGS`. The `--` keeps the agent's own
  arguments, such as `claude --resume`, from being read as the wrapper's.
- The interpreter is named by absolute path, never found through `PATH`.
  In the published image `PATH` holds `/opt/venv/bin`, which lives on the
  `/cache` volume that agents can write.
- `-I` (isolated mode) ignores `PYTHON*` variables, the user's
  site-packages and the current directory.
- The interpreter and venv are installed root-owned under
  `/usr/libexec/claude-sandbox/`, never run from uv's cache: `~/.cache` is
  bound read-write into the jail.
- The package imports only the standard library and has no runtime
  dependencies, so no third-party Python code is trusted.
- The launch path runs `script`, `bwrap`, `git`, `unshare`, `pasta`, `ip`,
  `ss` and `socat` from a fixed list of system directories (`/usr/bin`,
  `/bin`, `/usr/sbin`, `/sbin`), whatever `PATH` the caller has.

A test plants each of these redirections, launches, and checks that none
of them ran.

## One source of mounts and environment

`bwrap.py` builds the whole bwrap command line as a pure function of the
agent profile, the configuration and the launching environment. No other
module adds a mount or an environment variable to it. bubblewrap applies its
operations in order, so the order in that file is part of the security
model: a mask must follow the bind it covers, and an `allow-write` bind must
follow the masks it reaches through.

## The XDG split: data bulk-bound, config strict-allowlist

The wrapper covers `$HOME` with an empty tmpfs, then restores selected paths.
Under `.config`, only the forge stores `gh` and `glab-cli` are restored;
`no-forge` omits those too. New credential stores under `.config` therefore
remain hidden without changes to the wrapper.

`.local/share` and `.cache` are bound as whole directories so plugins, package
registries and tool downloads work without per-tool configuration. Credentials
stored there are exposed. Two `.local/share` directories are masked again:

- `applications`: keeps Claude's desktop URL-handler registration temporary.
- `claude`: keeps its versioned binary cache separate from the outer install.

Other top-level credential directories, such as `.ssh`, `.aws`, `.kube` and
`.gnupg`, remain behind the home mask. See the
[exposure table](../reference/deliberately-exposed.md) for agent state and skills.

## uv bind discipline

Only `uv` and `uvx` are bound back from `~/.local/bin`; the directory itself
stays temporary. The real Claude binary is also bound at
`~/.local/bin/claude` for its native-install checks.

`~/.local/bin` is appended to PATH, after system directories. A binary planted
there cannot take precedence over a system command or the agent wrapper.

## The outer PATH and the entry-point guard

The published image appends the venv to `PATH` for outer shells too
(`PATH=$PATH:/opt/venv/bin`), so an executable a session leaves in it cannot
shadow a system command ({ref}`ADR 28 <adr-review-what-a-session-leaves>`).

Where a writable directory does come before `/usr/local/bin`, the
**entry-point mount guard** protects the sandbox's own names: for each such
directory that exists at launch, `bwrap.py` read-only binds `/dev/null` over
`claude`, `codex`, `pi` and `claude-sandbox`. The wrapper refuses to launch
when one of them is anything else, `claude-sandbox doctor` warns about it,
and verification check 22 asserts the binds.

## gitconfig defence-in-depth

The wrapper sets `GIT_CONFIG_GLOBAL=/etc/claude-gitconfig` and
`GIT_CONFIG_SYSTEM=/dev/null`. The curated config supplies Git identity,
HTTPS rewrites and forge credential helpers.

The outer `/etc/gitconfig` remains readable. Tools such as pre-commit that
scrub `GIT_*` variables can still use it; masking it broke those tools.
The user's home gitconfig remains hidden by the home mask.

## Network-identity disclosure

With the jail enabled, `pasta` mirrors the outer address, gateway and DNS
resolvers into a private namespace. Those addresses remain visible, but the
outer container's complete interface and routing view does not.

Disabling the jail gives the agent the outer container's network view and
reach, including local services. Configured loopback relays and `allow-ip`
exceptions also grant service access while the jail is enabled.

## The procfs view

All agents use `--unshare-pid` and retain the read-only outer `/proc`;
its process IDs differ from sandbox-local IDs. Verification check 07 uses
the `NSpid` nesting information to check PID namespace isolation.

## Egress-jail mechanism: holder netns + pasta-attach

The wrapper (`jail.py`) creates an `unshare -rn` holder with a new user and
network namespace. The holder re-enters the package with the same
root-owned interpreter, in isolated mode
(`python -I -m claude_sandbox _jail_holder -- COMMAND`), so no shell or
`PATH` lookup sits between the two. `pasta` attaches from outside, where it
has internet access. The holder brings up loopback and locks the routing
rules before starting bubblewrap, which inherits that network namespace.
The holder keeps the terminal as its standard input and stays in the
terminal's foreground process group, so Ctrl-C and window resizes reach the
agent.

The routes block private, CGNAT, connected and link-local networks and
Azure's WireServer, with exceptions for the gateway, DNS and `allow-ip`
destinations. pasta copies every outer route into the namespace, so the
holder flushes the main table and builds the allowlist from nothing, each
route leaving from the interface's own address, checks that IPv6 holds
nothing beyond link-local (switching it off where it can), then
reads back the main, local and other tables and the policy rules and
refuses the launch if anything differs. The ordering is
essential: create namespace, attach pasta, restrict routes, then launch agent.
Each of those steps fails closed: the agent does not start. Only the DNS
forwarder route, `allow-ip` routes and callback relays fail with a warning,
because losing one loses reachability, not containment. On exit, or on an
interrupt, terminate or hang-up signal, the wrapper stops the relays and the
holder and removes its temporary files.

The network namespace belongs to an ancestor user namespace, so sandboxed
processes cannot change its routes. Bubblewrap drops effective capabilities
(`CapEff=0`), although the nested namespace can retain a full capability
bounding set. Checks 19–20 inspect the routes and representative destinations;
they report a disabled jail as a pass with a note.

The private namespace has no LAN broadcast. EPICS clients need a unicast
`EPICS_CA_ADDR_LIST` and corresponding `allow-ip` entries. Ordinary container
shells retain their existing networking. See
[network configuration](../how-to/network-egress-jail.md) and
{ref}`adr-network-egress-jail`.
