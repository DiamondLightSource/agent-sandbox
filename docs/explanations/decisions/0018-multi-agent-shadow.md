(adr-multi-agent-shadow)=

# 18. One shadow, many agents: wrap Codex the way we wrap Claude

Date: 2026-09-07

## Status

Accepted

Extends {ref}`adr-shadow-on-path` (ADR 9) and
{ref}`adr-managed-settings-guard` (ADR 13) to a second agent.

## Context

Claude Code is no longer the only terminal coding agent people here run
against a checkout. OpenAI's **Codex CLI** — the client for **GPT-6 Astra**,
released 2026-09-03 — is a peer: it reads the repository, executes shell
commands, calls the network, and is steered by text it did not write. Every
premise of the [threat model](../threat-model.md) applies to it unchanged. A prompt injection
that reaches Codex reaches whatever Codex can reach.

Today `claude` is wrapped and `codex` is not. That is not a partial defence,
it is an open door beside a locked one: a user who has both installed gets
full host credentials the moment they type the unwrapped name, and the
sandbox's own guard says nothing, because the guard only knows about Claude.

The vendors' surfaces turn out to be near-isomorphic, which is what makes a
shared implementation honest rather than a forced fit:

| Concern | Claude Code | Codex CLI |
|---|---|---|
| Real binary lands at | `~/.local/bin/claude`, rc prepended | `~/.local/bin`, rc prepended |
| Login / config state | `~/.claude`, `~/.claude.json` | `~/.codex` (`CODEX_HOME`): `config.toml`, `auth.json` |
| Admin-controlled policy | `/etc/claude-code/managed-settings.json` | `/etc/codex/requirements.toml` (hard), `managed_config.toml` (soft) |
| Guard hook points | `SessionStart`, `UserPromptSubmit` | `SessionStart`, `UserPromptSubmit` |
| Blocking a turn | exit 2 on `UserPromptSubmit` | exit 2 on `UserPromptSubmit` |
| Update-driven bypass | auto-updater re-creates the binary | self-update re-creates the binary |

So the same bwrap argv, the same egress jail, the same `script(1)` pty wrap,
and the same two guard scripts are correct for both. Only four things differ:
which binary to exec, which `$HOME` paths carry the login, which flags to
inject (`--no-chrome` is Claude-only and would abort Codex), and which env the
sandbox sets for itself.

## Decision

**One shadow file, installed under both names, dispatching on `argv[0]`.**

`install.sh` places the *same* `claude-shadow` at `/usr/local/bin/claude` and
`/usr/local/bin/codex`. The script resolves an **agent profile** from the name
it was invoked as; everything security-critical is shared code below that
point. A closed-set `CLAUDE_SANDBOX_AGENT` override exists for tests and
renamed symlinks, and can only ever select a hard-coded profile — never an
arbitrary binary.

The rejected alternative is a second `codex-shadow`. Duplicating ~800 lines of
bwrap argv construction would rebuild precisely the "surface too big to audit
in one read" that the bash-only rewrite walked back from — and the copies
would drift silently, so a hardening fix landed in one would quietly not exist
in the other. A profile table is the smallest thing that expresses "these two
are the same sandbox".

**The guard is delivered through Codex's managed layer, not its user config.**
`/etc/codex/requirements.toml` carries the `SessionStart` verifier and the
`UserPromptSubmit` gate, pointing at the same root-owned
`/usr/libexec/claude-sandbox/*.sh` scripts. This is the same reasoning as
{ref}`adr-managed-settings-guard`: the entries live in `/etc` (not removable by
editing a user file), and the scripts live off-PATH and read-only inside the
sandbox (not rewritable to `exit 0` from inside the jail). It matters
particularly for Codex, whose *project*-scoped `.codex/config.toml` sits inside
the read-write workspace and is therefore attacker-writable from inside a
compromised session — the hard tier is above it.

We do **not** set `allow_managed_hooks_only`, for the same reason we do not set
Claude's `allowManagedHooksOnly`: it would silence the owner's own hooks.

**The `codex` shadow is installed even when the Codex binary is not.** The
shadow has to own the name on `$PATH` *before* the vendor's installer can claim
it. An unbacked shadow loud-fails with instructions; an unshadowed vendor
binary runs outside the jail. Fetching the binary is best-effort — a failed
download warns and continues, because it must not brick a Claude install.

**TOML is owned, not merged.** This repo is bash-only and has no TOML
equivalent of `jq`. Rather than half-parse TOML, the installer writes files it
marks as its own and refuses to touch a `requirements.toml` it did not write,
warning with the exact snippet to paste. Bricking a site's real Codex policy
would be worse than not wiring the guard — the same call the non-JSON
managed-settings path already makes.

## Consequences

- `claude` behaviour is bit-for-bit unchanged. The claude profile reproduces
  the previous hard-coded values exactly, which the existing 138 argv
  assertions confirm unmodified.
- Each agent sees only its own credentials: a Codex session gets no
  `~/.claude`/`~/.claude.json` bind, and vice versa. Compromising one does not
  hand over the other's login.
- `~/.codex` joins `~/.claude` in the cross-container share, on the
  {ref}`adr-container-scoped-credentials` reading: it is one vendor login, not
  a repo-scoped forge PAT. Forge PATs stay container-scoped. The share is
  fail-soft — a read-only shared store costs a re-login, never the install.
- Codex updates become deliberate, like Claude's: `check_for_update_on_startup
  = false` in the managed config, plus `CODEX_UPDATE_DISABLED=1` set inside the
  sandbox. For Codex the in-sandbox half is belt-and-braces rather than
  load-bearing — the binary it execs is read-only in the session (see the
  exec-in-place bullet below), so a self-update *cannot* rewrite it; what the
  variable buys is not spending a session's first seconds fetching a release it
  cannot install, and not leaving a half-unpacked tree in `CODEX_HOME`.
  Claude's path is the weaker one: its real binary is bind-mounted
  **read-write** at `$HOME/.local/bin/claude` (Invariant 1 wants the
  conventional path to exist), so there an in-session self-update genuinely
  would rewrite the host's relocated binary, and `DISABLE_AUTOUPDATER` is the
  only thing standing in the way. Making *that* bind read-only is worth
  considering separately.
- Neither agent's binary is upgraded by re-running `./install`:
  `install_claude_binary` and `install_codex_binary` both early-return once the
  relocated binary exists. With the in-container auto-updater deliberately off,
  the only way to move to a newer agent release today is to delete the
  relocated copy first. That is a pre-existing property of the Claude path
  which this ADR inherits rather than introduces, but "updates become a
  deliberate `./install`" is not yet literally true, and wiring an explicit
  re-fetch is left as follow-up work.
- **Codex's install layout needed handling that Claude's did not**, in four
  ways that only surfaced by reading the vendor's installer and then running
  it for real:
  - It unpacks a versioned release under
    `$CODEX_HOME/packages/standalone/releases/<version>/` and puts only a
    *symlink* at `~/.local/bin/codex`. Relocating the link would relocate
    nothing, so we resolve it first.
  - **Codex is a package, not a binary.** The release carries `bin/codex`
    alongside ripgrep (`codex-path/rg`) and Codex's own bwrap and zsh helpers
    (`codex-resources/`) — the vendor's own validity check requires them
    together. So the *whole release directory* is copied to
    `/usr/libexec/claude-sandbox/codex-dist/`, and codex is exec'd **in place**
    from there with no bind-back: `/usr/libexec` is already visible via
    `--ro-bind / /`, the package's internal layout stays intact, and the binary
    we exec is consequently **read-only** in the session. That was strictly
    better than Claude's bind-back, whose rw bind let a session rewrite the
    host's relocated binary; the Python shadow now binds Claude's back
    read-only too.
  - The real binary ships *inside* `~/.codex`, the directory we bind
    read-write because it is also `CODEX_HOME`. A writable copy of the agent's
    own binary inside its own session is a persistence foothold, so the shadow
    tmpfs-masks `~/.codex/packages` — the same treatment Claude's versioned
    binary cache at `~/.local/share/claude` gets. The mask is emitted after the
    bind it covers, because bwrap applies argv in order.
    The managed daemon reads `/proc/<pid>/stat` using sandbox-local
    PIDs, which do not match the outer procfs retained by the sandbox.
    Consequently, `codex-launch` runs inside bwrap and supervises a foreground
    app-server for `codex agents`, connecting the client with `--remote` over
    a private socket in the sandbox's `/tmp`. Configuration overrides are
    forwarded to the server, and `--cd` sets its working directory. Both fresh
    launches and nested spawns use this helper, with the binary path supplied
    by the shadow. The helper cleans up on client exit or a termination
    signal, escalating from TERM to KILL after two seconds. Unsupported agents
    options produce a warning before native startup. Explicit remote
    connections and other commands pass
    through. This server is scoped to one invocation and its workspace;
    sharing a persistent server across sandbox launches is not implemented.
    Foreground startup uses the relocated binary directly, so it requires
    neither the managed standalone installation path nor a package bind-back.
    **The Unix socket has no application-level authentication.** Its access
    boundary is the sandbox's private `/tmp` and the mode-0700 directory
    created by `mktemp`, not a token or a client identity check. Another
    process running as the same user inside that sandbox can discover the
    socket, connect to app-server, and exercise its capabilities with the
    server's credentials and workspace access. This is accepted: processes
    within one sandbox share a trust boundary; the socket does not isolate
    the interface from tools, hooks, or subagents in that sandbox. It must
    not be moved into shared `CODEX_HOME` or exposed over a network listener
    without revisiting authentication and cross-workspace access.
  - By the time `install_codex_binary` runs, `link_terminal_config` has usually
    already symlinked `~/.codex` into the **shared cross-container store** — so
    letting the vendor unpack there would push a versioned release tree, tens
    of MB per release and pruned by nothing, onto the host share that every
    other container mounts. The in-session tmpfs mask hides that tree but does
    not stop it accumulating. So the vendor installer is pointed at a
    **temporary `CODEX_HOME`**, and both it and any tree a future installer
    drops in the real one are deleted once the `/usr/libexec` copy is taken.
    Only the copy we exec survives the install.
- **Two vendor-installer behaviours make an unattended install fail**, both
  worked around where the script leaves a seam. It ends with a `Start Codex
  now? [y/N]` prompt read from `/dev/tty`, not stdin — so piping to `sh` does
  not make it unattended (`CODEX_NON_INTERACTIVE=1`). And its release tarball
  carries uid/gid 1001, which tar-as-root tries to restore; under a rootless
  container that chown is denied and tar exits non-zero, failing the whole
  install (`TAR_OPTIONS=--no-same-owner`).
- A site that already ships its own `/etc/codex/requirements.toml` gets a
  warning and no guard until someone merges the snippet by hand. That is the
  accepted cost of not owning other people's policy files.
- Adding a third agent is now a profile entry plus a managed-config writer,
  not another shadow.
