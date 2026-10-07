## The 22 checks

### Check 01 — IS_SANDBOX sentinel

`IS_SANDBOX=1` is set inside the sandbox by `bwrap --setenv`. If
unset, Claude was launched against the real binary
(`<clone>/.runtime/claude`) directly, bypassing the sandbox entirely.
This is the fall-through sentinel.

### Check 02 — NO_NEW_PRIVS

bwrap sets `PR_SET_NO_NEW_PRIVS=1` before exec'ing the target, so
setuid binaries inside the sandbox cannot gain privileges. With
NO_NEW_PRIVS in effect, `/proc/self/status` reports `NoNewPrivs: 1`.
Without it, `sudo` / setuid-root binaries inside the sandbox could
elevate (in concert with a userns escape) and break the rest of
the threat model.

The earlier check 02 read `/proc/1/comm` and expected `bwrap|claude|
node`. That was a victim of the same procfs-leak failure mode the
new check 07 documents — on rootless nested-userns hosts procfs is
mounted in the outer pidns, so `/proc/1/comm` reads the devcontainer
init (`sh`) instead of the sandbox target. The "bwrap is in our
ancestry" property is already covered by check 01 (`IS_SANDBOX=1`
is only set by `bwrap --setenv`), so check 02 was redundant *and*
broken on the hosts we care about. Repurposed to cover NO_NEW_PRIVS,
which was previously listed as "Implicit" in the locked-down table with
no PASS/FAIL check of its own.

### Check 03 — strict-under-/root

`$HOME` (typically `/root`) is a tmpfs with the running agent's own
config bound back in from the host — `.claude` + `.claude.json`
(Claude Code's account state) for a `claude` session, `.codex`
(`CODEX_HOME`: `config.toml`, `auth.json`, sessions) for a `codex`
one — plus `.cache`, `.local/share`, and a `.config` intermediate
tmpfs that holds the `gh` / `glab-cli` credential binds.

Every agent also gets `.agents`, as the parent of the read-write
`~/.agents/skills` bind shared across agents (ADR 25). `.agents` may
contain **only** `skills`: the rest of the host's `~/.agents` is never
bound, so any other entry is either a leak or an in-session write to
the tmpfs, and FAILS here.

**The expected set is per-agent, and that makes this check stronger,
not laxer.** Each session binds only its own agent's config, so a
`.claude` appearing inside a codex session (or a `.codex` inside a
claude one) is a cross-agent credential leak and FAILS here. The
battery reads `IS_SANDBOX_AGENT` (set by the shadow) to know whose
session it is, defaulting to `claude` when unset. In a codex session
`.codex/packages` is additionally tmpfs-masked — the vendor unpacks
the codex binary there, inside the read-write `CODEX_HOME`, and a
writable copy of the agent's own binary is a persistence foothold. The `.local/share` bind is the
XDG-data bulk-mount (helm plugins, krew, uv-managed Python, etc.) —
see the [XDG split rationale](https://diamondlightsource.github.io/claude-sandbox/explanations/sandbox-internals.html). Under `.local/share`,
two sub-dirs stay tmpfs-masked: `applications/` (Claude Code's
`.desktop` URL handler, which we don't want registered on the host
desktop environment) and `claude/` (Claude Code's versioned binary
cache, ephemeral by design), and uv's Python store, `uv/python`, is
bound read-only over it when it exists at launch (ADR 27), so a session
cannot rewrite an interpreter a venv links to. Claude Code also writes `.local/bin/
claude` (the real-binary bind) and tmpfs-only entries under
`.local/state/claude`, so `.local` is expected as a top-level entry.
The defence-in-depth file masks (checks 14–15) also bind `/dev/null`
over `.netrc`, `.Xauthority`, and `.ICEauthority` — so those names
are expected to appear too, as size-zero entries (which checks 14–15
verify; `.ICEauthority` is masked without a dedicated check because
it shares the X11 cookie attack surface). Anything else under `$HOME`,
or anything besides `gh` / `glab-cli` under `$HOME/.config`, means the
strict-under-/root inversion regressed. `.gitconfig` is no longer
masked — it doesn't normally appear under the tmpfs `$HOME`, but the
allow-list still permits the name in case a tool drops one.

Claude Code, left to its own devices, would drop a Chrome native-
messaging-host manifest (`com.anthropic.claude_code_browser_extension.
json`) into each chromium-family browser's `NativeMessagingHosts`
directory on launch — `BraveSoftware`, `chromium`, `google-chrome`,
`microsoft-edge`, `opera`, `vivaldi`. That manifest registers the
in-sandbox Claude as an RPC target for any installed browser
extension, which is outside the threat model. The shadow injects
`--no-chrome` and strips user-supplied `--chrome` so the manifests
never get written, and check 03 enforces that: if any of those six
browser-named dirs reappears under `$HOME/.config`, the disable
regressed.

### Check 04 — env scrub: GH_TOKEN / OPENAI_API_KEY

With `--clearenv` and an explicit allow-list, `GH_TOKEN` from the
host shell must be empty inside the sandbox.

`OPENAI_API_KEY` is asserted alongside it, and for **both** agents
rather than only for codex sessions: an OpenAI key sitting in the host
environment is a credential the jail must not hand to any session,
whichever agent is running. The installed battery checks both variables.

### Check 05 — env scrub: DISPLAY

`DISPLAY` is deliberately not in the `--clearenv` allow-list — it
closes the X11 reachability path.

### Check 06 — cap_drop ALL

`--cap-drop ALL` empties the effective capability set. `CapEff` in
`/proc/self/status` reads all zeros.

### Check 07 — --unshare-pid (kernel pidns isolation)

`--unshare-pid` puts the sandbox in a nested PID namespace. The
kernel-level effect is what matters for the threat model: `kill()` /
`ptrace()` are scoped to the new pidns, so the sandbox cannot signal
or attach to host or devcontainer processes. We positively assert
the nesting via `/proc/self/status:NSpid:` — outside any sandbox
this has one entry; inside one nested pidns it has two.

The companion property (procfs *view* aligned with the new pidns) is
not checked here. On rootless devcontainer hosts bwrap's `--proc /proc`
mounts procfs against its outer pidns rather than the spawned child's,
so the shadow always read-only binds the host's `/proc` instead: the
process tree is visible even though kernel kill/ptrace scoping is
intact. Credential-bearing procfs
entries (`/proc/<pid>/environ`, `/maps`, `/fd`, `/mem`) stay gated by
`PTRACE_MODE_READ_FSCREDS` + YAMA `ptrace_scope=1`, so leaked
visibility does not become credential exfil — but see the [threat model](https://diamondlightsource.github.io/claude-sandbox/explanations/threat-model.html)
for the honest tally.

### Check 08 — --unshare-ipc

The SysV IPC namespace differs from the host's. Inside an unshared
ipcns, `/proc/self/ns/ipc` resolves to a different inode than the
un-namespaced kernel default. We can't sample the host inode from
inside, but we CAN assert `/proc/self/ns/ipc` exists and is a symlink
to a unique `ipc:[<inum>]`.

### Check 09 — --unshare-uts

The UTS namespace is unshared, so a hostname change inside doesn't
affect the host. We assert the namespace symlink exists with the
expected shape; the integration test exercises the behavioural property.

### Check 10 — private /dev (TIOCSTI blocked)

We dropped `--new-session` so SIGWINCH and job control reach the
sandbox. The TIOCSTI defence is now delivered by two coupled
mechanisms: the shadow wraps bwrap in `script(1)` (the in-sandbox
process inherits script's allocated pty as its controlling terminal,
not the host's), and the bwrap argv uses `--dev /dev` (a fresh
devtmpfs with a fresh devpts mount — the host's `/dev/pts/*` is
not visible). An ioctl(TIOCSTI) inside the sandbox can therefore
only inject into script's pty, whose contents script reads and
writes as *output bytes* to the host terminal — never as input to
the parent shell. The check asserts `/dev` is a fresh `tmpfs`/`devtmpfs`
mount (mountinfo fs-type field) rather than a bind of the host's `/dev`.

### Check 11 — /tmp is tmpfs and empty

The host's `/tmp` carries VS Code IPC sockets (`vscode-ipc-*.sock`,
`vscode-git-*.sock`). `--tmpfs /tmp` masks them. We assert no such
socket is visible.

### Check 12 — /run/user is tmpfs and empty

`--tmpfs /run/user` masks the user's runtime directory which can hold
DBus sockets and other IPC bridges.

### Check 13 — /run/secrets is tmpfs and empty

`--tmpfs /run/secrets` closes the Docker/Compose secrets path even
when the host has populated `/run/secrets/*`.

### Check 14 — file mask: .netrc empty

`--bind-try /dev/null /root/.netrc` masks any host `.netrc`
credentials.

### Check 15 — file mask: .Xauthority empty

`--bind-try /dev/null /root/.Xauthority` masks the X11 cookie that
would otherwise authenticate against a host X server.

### Check 16 — curated gitconfig active

`GIT_CONFIG_GLOBAL=/etc/claude-gitconfig` is exported and the file's
`user.email` is present. Verifies that the curated gitconfig is in
effect at every launch.

### Check 17 — workspace scoped to `$PWD`, not broad `/workspaces`

The default workspace bind is `$PWD` — only the current project
directory is writable inside the sandbox. The old behaviour (binding
all of `/workspaces`, making sibling devcontainer projects writable)
is restored by setting `CLAUDE_SANDBOX_WORKSPACE_ROOT=/workspaces` in
your devcontainer's `remoteEnv`. This check fails when the broad
`/workspaces` bind is active without that explicit opt-in.

The mountinfo parse is hardened: it reads the per-mount options field
(field 6) of the last matching `/workspaces` line and requires an
**exact** `rw` token, never the superblock options after the `-`
separator (which routinely end in `rw` even for a read-only bind). The
exact parse lives in the script; token equality means a superblock `rw`
can't produce a false positive.

### Check 18 — config read from `/etc`, not the workspace

The shadow reads its config from the host-global
`/etc/claude-sandbox.conf` (placed by the installer), **not** from
`$PWD/.devcontainer/claude-sandbox.conf`. The old per-workspace read
sat inside the rw-bound workspace, so a compromised session could
rewrite it (`allow-write = /`, `workspace-root = /`) and the next
launch would honour it — a cross-session bind-escalation. `/etc` is
not in the sandbox's rw set, closing that vector.

This inspects the installed shadow on `$PATH` (visible read-only via
`--ro-bind / /`). It must be the three-line shim that execs the root-owned
interpreter under `/usr/libexec/claude-sandbox` with `-I`; anything else
fails. The pins are then read from the package in that interpreter's venv:
`config.py` sets `CONFIG_PATH` to `/etc/...`, and `shadow.py` defaults the
conf path to it and passes that to `parse_config`, with no `parse_config`
call reading from `.devcontainer` (the old, attacker-writable call site).
The negative match is scoped to the `parse_config` call so a rationale
comment that names the source path doesn't trip it.

### Check 19 — egress jail active: netns isolated, RFC1918 blackholed

The per-process egress jail (ADR 0015, Design D) runs Claude inside a
dedicated network namespace whose routing table blackholes the RFC1918
ranges (`10/8`, `172.16/12`, `192.168/16`), the CGNAT range
(`100.64/10`, Tailscale et al.), and every connected subnet, punching
back only the gateway, DNS resolvers, and `allow-ip` devices. The netns
is IPv4-only (pasta `--ipv4-only`), so there is no v6 address family to
blackhole. This check asserts the netns is actually programmed: a default
route exists **and** all three RFC1918 blackhole routes **and** the CGNAT
blackhole are present. It catches a fail-*open* regression (the jail being
skipped while Claude still launches) and partial programming (e.g. only
`10/8` blackholed, or CGNAT dropped so Tailscale internal hosts leak).

The jail is fail-*closed* by design — the holder (`jail.holder_main`,
`lock_routes`) aborts, so Claude never starts, if any blackhole route
fails or the rebuilt route table is not exactly the allowlist — so a
running session is
either fully jailed or deliberately un-jailed. The intended-state env var
`CLAUDE_SANDBOX_EGRESS_JAIL` is **not** in the shadow's `--setenv`
allowlist, so it is invisible from inside; this check therefore keys off
the jail's *observable effect* (blackhole routes), not intent. When no
blackhole routes are present, the jail is treated as legitimately disabled
(`egress-jail = 0` in `/etc/claude-sandbox.conf`, or the
`CLAUDE_SANDBOX_EGRESS_JAIL=0` env escape hatch) and the check PASSES with
a "jail not active (disabled)" note rather than false-failing the opt-out.

### Check 20 — RFC1918 lateral egress blackholed for a non-allow-listed IP

Behavioural counterpart to check 19: instead of inspecting the route
table, it asks the kernel FIB to resolve representative non-allow-listed
RFC1918 destinations and asserts each is **unreachable**, while a
known-allowed destination (the default gateway) stays routable. The
gateway-routable half matters — it distinguishes a *surgical* lateral
blackhole from a network that is simply down, so a regression that breaks
all egress can't masquerade as "jail working".

The broad probe addresses (`10.255.255.254`, `172.31.255.254`,
`192.168.255.254`) sit deep in each RFC1918 block where a real gateway,
resolver, or `allow-ip` device is implausible; the jail punches only
specific `/32`s back through the blackhole, so these resolve to
`unreachable`. These exercise the generic RFC1918 blackholes — but the
jail *also* blackholes **every connected subnet** (the host's own LANs),
and that is the higher-value lateral-movement case: a neighbour one hop
away on the same wire. So the check additionally derives a connected
subnet and probes its network base address — guaranteed in-subnet, and
never one of the host `/32`s the jail punches back (gateway / resolver /
`allow-ip`), so it can't accidentally hit an allowed route. (The jail may
blackhole several connected subnets; probing the first is sufficient to
prove the mechanism.) As with check 19, when the jail is disabled (no
blackhole routes) the check PASSES with a note.

Reachability is classified by **route type**, not merely by `ip route
get`'s exit status. A destination counts as reachable (a lateral-egress
leak) only when the FIB returns a *forwarding* route — a plain unicast
route toward a peer or gateway. Non-forwarding results —
`blackhole` / `unreachable` / `prohibit` / `broadcast` / `local` /
`multicast` / `throw` — do not count. This matters specifically for the
connected-subnet probe: a subnet's network base address carries a
kernel-installed `broadcast` route in local table 255, so `ip route get
<base>` exits 0 (resolving to that broadcast route) even while every real
host in the subnet is blackholed. An earlier version tested exit status
alone, so the broadcast route false-"resolved" the base address and the
check FAILed on a fully-working jail. Filtering to forwarding routes
closes that false positive while still catching a genuine unicast route
to a non-allow-listed RFC1918 peer.

### Check 21 — agent binary mask: `~/.codex/packages` is an empty tmpfs

Codex ships as a package and its vendor installer unpacks the release —
including `bin/codex` itself — under `$CODEX_HOME/packages/standalone/
releases/<version>/`. `CODEX_HOME` is `~/.codex`, which the shadow binds
**read-write** because config and `auth.json` live there too. So without a
mask, a Codex session contains a writable copy of its own binary: a
compromised session rewrites it, the next launch re-executes it, and the
read-only `/usr/libexec/claude-sandbox/codex-dist` copy we actually exec is
bypassed entirely. Exactly the treatment Claude's versioned binary cache at
`~/.local/share/claude` already gets.

The mask is a `--tmpfs` emitted *after* the `~/.codex` bind (bwrap applies
argv in order, so a mask hoisted above its bind is silently lifted). This
check asserts it landed: the path is either absent, or a mountpoint whose
tmpfs is empty. Check 03 cannot cover it — 03 inspects only `$HOME`'s top
level, where `.codex` is legitimately allow-listed, so the writable tree
one level down would pass unnoticed.

Claude sessions have nothing to assert here and PASS with a note, so the
check count stays constant across agents.

### Check 22 — entry-point names guarded ahead of the shadow

Invariant 1 says a plain `claude` (or `codex`, `pi`, `claude-sandbox`)
reaches the shadow at `/usr/local/bin`. The shadow protects the sandbox's entry-point names: a session
cannot create a command named claude, codex, pi or claude-sandbox in a
writable directory that precedes the shadow on `PATH`. For each such
directory that exists at launch, `bwrap.py` read-only binds `/dev/null`
over each name, after the read-write binds, and lists the directories in
`CLAUDE_SANDBOX_ENTRY_GUARD` (the jail's own `PATH` is not the launching
one, so the battery could not work them out). This check asserts that each
name there is a mount point with the `ro` option (so it cannot be written,
replaced, renamed or removed; `test -w` cannot tell, since a device node on
a read-only mount still reports writable) and is not a symlink, a
directory or an executable.

When the variable is unset the check passes with a note: no writable
directory precedes the shadow. A directory that does not exist at launch
cannot be covered without creating it; the shadow's launch-time refusal
covers what a session leaves there.
