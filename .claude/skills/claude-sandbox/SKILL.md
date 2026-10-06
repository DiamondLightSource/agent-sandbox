---
name: claude-sandbox
description: Architecture invariants, refuse-lists, and walked-back paths for this repo's bwrap sandbox core (shadow, installer, integrity guard). Surface before editing `.devcontainer/claude-sandbox/*`, `install`, `tests/`, `.github/workflows/ci.yml`, or `skills/verify-sandbox/SKILL.md` — or before any Python change that could cross the ADR 26 guardrails (a runtime dependency or third-party import in the package, an interpreter found via PATH or run without -I, a bwrap bind/env outside bwrap.py, spreading the audit core) or any suggestion to revert to bash-only, persist gh/glab PATs, auto-edit devcontainer.json, read conf from the workspace, move the integrity guard out of managed-settings, re-enable the auto-updater, expose a host container-engine socket, or pass-env secrets. Container-image/launcher topics: claude-sandbox-container skill. Network/egress topics: claude-sandbox-networking skill.
---

# claude-sandbox

Project-specific architecture decisions. The code documents *what*;
this skill documents *why* and *what regressions to refuse*. Threat
model: [threat model](https://diamondlightsource.github.io/claude-sandbox/explanations/threat-model.html); live verification: `/verify-sandbox`
(the shipped skill `skills/verify-sandbox/SKILL.md` defines the audit;
its `references/checks.md` documents the *why* of each check;
the phase-1 PASS/FAIL battery is the committed
script `.devcontainer/claude-sandbox/verify-sandbox-battery.sh`, run by
absolute path from `/usr/libexec/claude-sandbox`).

**Why the 22-check battery is a committed script, not inline in the
command markdown** (refuse a "simplify it back inline" request): slash
commands substitute `$1`…`$9` as positional args, so the awk field refs
the checks need were silently blanked when injected from the .md —
07/10/17/20 false-failed on awk syntax errors before any shell ran. A
file on disk dodges that, the shebang pins bash (no zsh `nomatch` glob
abort), and `/usr/libexec` placement makes it ro inside the sandbox so a
compromised session can't rewrite the verifier to print PASS. install.sh
places it via `install_guard_scripts`; the smoke test asserts
placement/mode and that it runs-to-format-and-exits-nonzero outside a
sandbox.

## Invariant 0 — ONE shadow file, many agents, dispatching on `argv[0]`

The sandbox wraps Claude Code **and** OpenAI's Codex CLI (the client
for GPT-6 Astra). `install.sh` places the *same* `claude-shadow` at
`/usr/local/bin/claude` and `/usr/local/bin/codex`; `detect_agent`
picks the profile from the name it was invoked as, and
`agent_profile` holds everything that differs — the real binary, the
`$HOME` login paths, the injected flags (`--no-chrome` is Claude-only
and would abort codex), and the per-agent `--setenv` list. See
{ref}`ADR 18 <adr-multi-agent-shadow>`.

**Refuse as regressions:**
- A separate `codex-shadow` (or any per-agent copy of the bwrap argv
  builder). Duplicating ~800 security-critical lines rebuilds exactly
  the surface Reversal 1 walked back from, and the copies drift
  silently — a hardening fix lands in one and quietly not the other.
- Binding `~/.claude` into a codex session or `~/.codex` into a
  claude one. Each agent sees only its own credentials; that
  separation is the point. The ONE deliberate cross-agent path is
  `~/.agents/skills` (ADR 25, #52), rw in every session. Refuse widening
  it to all of `~/.agents` (Codex already reads `~/.agents/plugins`;
  contents are third-party-defined), putting shipped-skill ro binds
  inside it (they stay in each agent's own dir, ADR 24), or relaxing
  check 03's "`.agents` holds only `skills`".
- Dropping the `~/.codex/packages` tmpfs mask, or moving it above
  the `~/.codex` bind. The vendor unpacks the codex binary INSIDE
  `CODEX_HOME`, so without the mask a compromised session has a
  writable copy of its own binary to re-enter through.
- Relocating `bin/codex` alone. Codex is a PACKAGE — the vendor's own
  validity check requires `codex-package.json`, `bin/codex`,
  `bin/codex-code-mode-host`, `codex-path/rg` (ripgrep) and
  `codex-resources/bwrap` together — so `install.sh` copies the whole
  release dir to `/usr/libexec/claude-sandbox/codex-dist/` (resolving
  the vendor's symlink first; relocating the link relocates nothing)
  and the shadow execs it IN PLACE, no bind-back. Don't "simplify"
  that to a single-file bind: it strips codex of its search tool and
  sandbox helpers, and it would make the exec'd binary rw in-session.
- Removing `CODEX_NON_INTERACTIVE=1` or `TAR_OPTIONS=--no-same-owner`
  from the vendor-installer invocation. The first prompts on
  `/dev/tty` (piping to `sh` does NOT make it unattended); the second
  is why tar fails under a rootless container — the tarball carries
  uid/gid 1001 and tar-as-root cannot restore that ownership, so the
  whole install aborts. Both were found the hard way.
- Making `CLAUDE_SANDBOX_AGENT` accept anything outside the closed
  `claude|codex` set, or letting it name a binary path.
- Installing the `codex` shadow *conditionally* on the codex binary
  being present. The shadow must own the name on `$PATH` before the
  vendor's installer can claim it (Invariant 1 applies identically);
  an unbacked shadow loud-fails, an unshadowed binary escapes.
- Adding an agent by touching the argv builder rather than adding a
  profile entry.

Codex's guard is Invariant 5's twin, one tier over:
`/etc/codex/requirements.toml` (hard, admin-only) carries the same
two hooks; `/etc/codex/managed_config.toml` disables the update
check. **Do not** set `allow_managed_hooks_only` — same call as
`allowManagedHooksOnly`. TOML is *owned, not merged* (the bash
installer has no TOML parser; jq reads only JSON): a
`requirements.toml` we did not write is left alone with a warning,
never half-parsed.

`tests/bwrap_argv.sh` scenario 14 and the codex block at the end of
`tests/smoke.sh` guard all of this — including that the two installed
shadows are byte-identical.

## Invariant 1 — plain `claude` / `codex` MUST resolve to the shadow

Anthropic's `curl install.sh` drops the real binary at
`~/.local/bin/claude` AND prepends `$HOME/.local/bin` to the user's
shell rc. After the next shell, `which claude` resolves past the
bwrap shadow at `/usr/local/bin/claude` → **sandbox escape via
plain `claude`**. OpenAI's `chatgpt.com/codex/install.sh` behaves the
same way, so `install_codex_binary` performs the same relocation, to
`/usr/libexec/claude-sandbox/codex-dist/bin/codex` (the whole release
package, not a bare binary — see Invariant 0).

`install_claude_binary` fixes this by relocating the real binary to
`/usr/libexec/claude-sandbox/claude` (off the user's PATH). The
shadow binds it back to `~/.local/bin/claude` *inside* the sandbox
so Claude's `installMethod=native` self-check still sees the
conventional path. The Python shadow binds it **read-only**: the bash
bound it read-write, which let a session rewrite the binary every later
session runs.

**Refuse as regressions:**
- Any "simplification" that skips the relocate-after-curl step.
- Removing the unconditional bind-back of `~/.local/bin/claude`
  inside the sandbox — the dest is created on the in-sandbox tmpfs
  `$HOME`, so don't gate it on the host file existing.
- `tests/bwrap_argv.sh` scenarios 1 & 4a guard the bind pair; update
  both if you change the bind.
- Making the Python shadow's bind-back read-write again
  (`test_the_real_binary_is_bound_back_read_only`).
- Dropping the Python shadow's entry-point guard, or moving its binds
  above the read-write binds they sit inside. It protects the sandbox's
  entry-point names: a session cannot create a command named claude,
  codex, pi or claude-sandbox in a writable directory that precedes the
  shadow on PATH (`bwrap.py` ro-binds `/dev/null` over each name there;
  `shadow.check_entry_points` refuses a launch when one is anything but
  the empty mount point the bind leaves; battery check 22 asserts it).

**Acceptable swap:** if Anthropic adds `--no-modify-path`, drop the
relocate — provided plain `claude` still cannot resolve past
`/usr/local/bin/claude`.

## Invariant 2 — PATs are container-scoped; `claude-sandbox gh-auth` per rebuild is deliberate

The re-paste-on-rebuild ceremony for `gh` / `glab` PATs is the cost
of keeping blast radius small: fine-grained PATs typically cover
multiple repos, so any path mounted across devcontainers would let
a compromised session reach every repo the PAT touches.

`~/.claude` and `~/.claude.json` *are* cross-container (via
`link_terminal_config` symlinks) because they hold one Claude login,
not repo-scoped credentials. Don't conflate the two.

**Refuse as regressions:**
- New persistent-credential mounts (volume, bind, anywhere) for
  `gh` or `glab` PATs.
- Re-purposing the (currently deleted) `/cache` Docker volume for
  tokens. Restoring `/cache` for *caches* is fine; for tokens, not.

If a future request says "stop re-pasting the PAT" — surface this
tradeoff before implementing the shortcut.

## Invariant 3 — bwrap on Ubuntu 24.04 GitHub runners needs three workarounds

`ubuntu-latest` ships configured in ways that break bwrap. The
failure modes cascade in this order:

1. **`setting up uid map: Permission denied`** —
   `kernel.apparmor_restrict_unprivileged_userns=1` is the runner
   default. Relax the sysctl and install an unconfined AppArmor
   profile for `/usr/bin/bwrap`.
2. **`/run/secrets` doesn't exist** — sandbox does
   `--tmpfs /run/secrets`; `sudo mkdir -p /run/secrets` first.
3. **`$GITHUB_WORKSPACE` lives under `$HOME=/home/runner`** —
   path-positional checks that assert "$HOME contains only X" trip
   on the workspace bind. `export HOME=/tmp/sandbox-home` before
   the bwrap step (+ `mkdir -p "$HOME/.claude" "$HOME/.cache"`).

All three are required, in order. `.github/workflows/ci.yml` applies
them — five push-and-iterate cycles to land this; don't re-discover.

## Design principle — keep dogfood ≈ guest

The repo's own devcontainer (dogfood) and a `git clone + ./install`
inside any other devcontainer (guest) should go through the same
setup path. Prefer `install.sh` over `devcontainer.json` /
`postCreate.sh` / `initializeCommand.sh` when a fix can live in
either — guest devcontainers then get it for free, and the audit
surface stays single-track.

Sample: per-file binds for `/root/.claude{,.json}` were dropped once
`link_terminal_config` covered both paths uniformly; only the shared
`/user-terminal-config` bind remains in `devcontainer.json`.

**Refuse as regressions:** dogfood-only `postCreate` /
`initializeCommand` work, or `devcontainer.json` mounts that could
have been done in `install.sh`. Ask "would this work for a
clone+install inside an unrelated devcontainer?" — if not, push it
into `install.sh`.

## Design principle — never auto-edit `devcontainer.json`

Wiring a target's `postCreateCommand` is always a print-the-snippet,
user-pastes affair. `devcontainer.json` is JSONC in the wild and
comment-preserving structured edits need either ~50 lines of awk
(string/block-comment state-tracking) or a node/python lib dependency —
both rejected in PR #20, and the rationale outlives promote (ADR 0017).
The user knows whether they've wired the line or need to chain it.
"Strip and re-insert comments" isn't simpler either — re-insert needs
stable anchors that survive the edit. Print the snippet; trust them.

**Source-guard pattern**: `install.sh` ends with
`[ "${BASH_SOURCE[0]}" = "$0" ] && main "$@"` so the container image
build (Dockerfile) can `source install.sh` to reuse its functions
without re-running `main`. Don't remove the guard.

## Historical reversals — raise before re-treading

Three paths walked back. If a change suggests one of them, surface the
history and re-justify against the underlying principle — **the
sandbox's surface must stay small enough to audit in one read** —
before proceeding.

### Reversal 1 — Python orchestration (reversed again, deliberately)

History: embedded bash → standalone bash → Python package + typer CLI →
bash-only (`bf65407`, 2026-05-12, issue #14 / PR #15; ADR 8) → **Python
again (ADR 26, 2026-10-06, supersedes ADR 8; migration in issue #72)**.

Why bash-only was right then: the tool was one bash function building a
bwrap argv, two ~80-line files; the first Python package spread the
security-critical bits across many modules and was harder to audit.

Why Python is right now: the bash grew to ~3,900 lines, mostly argv/list
building and config parsing done with namerefs, `${arr[@]+…}` guards,
`printf %q` re-quoting and `export -f` into `unshare … bash -c`. At that size
bash is the *less* auditable choice — the opposite of ADR 8's intent. Both
ADRs serve the same principle: the core must stay small enough to audit in
one read.

**Do not re-litigate either way.** Refuse "go back to bash-only / remove
pyproject, uv, pytest" (ADR 26 is accepted), and equally refuse Python that
crosses the guardrails. Until issue #72 phase 5 the bash stays the shipped
default; the root `pyproject.toml` wheel bundles it verbatim (ADR 23,
amended by ADR 26).

**Refuse without justification (the ADR 26 guardrails):**
- Runtime dependencies. The package is stdlib-only: no third-party import
  anywhere, the CLI included (argparse; ADR 26 amended 2026-10-06 to drop
  Typer, whose rich/pygments/click would sit in PAT-handling, root-run
  helpers). `__main__.py` dispatches `_shadow` before the CLI is imported.
- For anything that runs in or launches the jail (the shadow shim and
  everything it execs): an interpreter found through `PATH`
  (`#!/usr/bin/env python3`, bare `python3`), running without `-I`, or
  running from uv's cache (`~/.cache` is jail-writable). Always the
  absolute root-owned interpreter under `/usr/libexec/claude-sandbox/` —
  in the image `PATH` starts with `/opt/venv/bin` → `/cache`, which the
  jail can write. The host-side `uvx claude-sandbox` launcher/installer
  (ADR 23) is a console script run from uv's cache without `-I` by
  construction and is outside this rule: don't "fix" it, and don't refuse
  uvx on its account.
- A bind or environment variable added to the bwrap argv anywhere but
  `bwrap.py`.
- Spreading the audit core (`bwrap.py`, `jail.py`, `shadow.py`) across
  more modules or helpers — the `bf65407` failure mode. Profiles, config,
  host launcher, installer and helper CLI live around the core, not in it.

### Reversal 2 — extracted from python-copier-template

Originally embedded in `python-copier-template` as
`.devcontainer/claude-sandbox.sh` (`unshare -m` + tmpfs overlays).
Extracted because a security tool needs one canonical, audit-friendly
home — not a templated copy per project — and a standalone repo gets
the bwrap defences, versioned releases, its own CI, and
`/verify-sandbox` as a first-class command. The old copy in that repo
is prior art, **not** maintained.

**Refuse without justification:**
- Adding a `template/` directory or `copier.yml`.
- "Let's keep a copy synced into python-copier-template" — the
  template should *consume* this repo, not embed it.

### Reversal 3 — `just promote` (copy-by-value into targets)

`just promote` (PR #20, ADR 0010) copied the install machinery —
`install.sh`, `claude-shadow`, the guard scripts, the battery — by
value into target workspaces so they became self-sufficient hosts.
Removed 2026-07-24 (ADR 0017): frozen per-project copies of
security-critical code have no update channel (a fix here never
reaches a promoted target; nothing signals staleness), it
re-proliferated the single auditable home Reversal 2 was extracted
for, and ADR 0016 showed it ships the guard-restamp trust anchors
into every target's *writable* workspace. Replacements: a target's
`postCreate` clones this repo **at a pinned tag** and runs
`./install` (docs/how-to/sandbox-a-team-devcontainer.md), or the
container image. Pinning keeps ADR 0010's "what ran is what's at
this SHA" property; note this is *not* the mutable-shared-clone
variant ADR 0010 rightly declined (that ran whatever HEAD happened
to be checked out).

**Refuse without justification:**
- Re-adding any mechanism that copies `install.sh` / `claude-shadow` /
  the guard scripts into a consuming repo (promote by another name,
  vendoring recipes, "sync" scripts).
- Pointing a target's `postCreate` at a mutable shared clone's HEAD —
  pin a tag or SHA instead.

## Diagnostic discipline — silent in-sandbox check failures

When a check inside the sandbox fails silently (subprocess swallows
stdout/stderr), inject a debug `INNER` step that runs the same body
verbatim and prints its output *before* exec'ing the real verifier.
The original Check 03 silent failure was unsolvable until we printed
`extras` directly — `--bind-try /dev/null` masks themselves create
entries under `$HOME` (the spec hadn't whitelisted them). One
`printf` beats hours of guessing from outside.

## Diagnostic discipline — bind-mount vs runtime tmpfs write

When unexpected entries appear inside the sandbox (typically under
`$HOME` or `$HOME/.config`), **first determine whether they're a
host bind-mount leak or a sandboxed-process tmpfs write**. The
remediations are completely different.

```bash
# Bind from the host?
grep " /root/.config/<thing> " /proc/self/mountinfo
# stat -c '%D' compares device IDs — tmpfs entries share /root's dev.
stat -c '%n: dev=%D inode=%i' /root /root/.config/<thing>
```

No mountinfo entry + same `dev` as `/root` → tmpfs write by
sandboxed code (a feature self-registering). Fix upstream by
disabling the feature, not by widening the allow-list. Mountinfo
entry → genuine inversion leak; tighten the bwrap argv.

Concrete miss (2026-05): Chrome `NativeMessagingHosts` dirs under
`~/.config/` — initially flagged as a bind leak; mountinfo showed
no bind. It was Claude Code's startup write registering the browser
extension. Fix: `--no-chrome` injection in the shadow, check 03
stayed strict.

## Invariant 4 — config is host-global at `/etc`, never read from the workspace

`claude-shadow` reads its config from `/etc/claude-sandbox.conf`
(`CONFIG_PATH`), placed by `install.sh`'s `install_conf` from the clone's
`.devcontainer/claude-sandbox.conf` and re-stamped on every rebuild via
postCreate. It is NOT read from `$PWD/.devcontainer/claude-sandbox.conf`
anymore (that call site moved in PR for the global-conf change).

Two reasons, one load-bearing for the threat model:

- **Security (the real reason).** `$PWD` is the workspace, bound rw into
  the sandbox. A per-workspace conf is attacker-writable from inside the
  jail: a compromised session could write `allow-write = /` (or
  `workspace-root = /`) and the next launch would `--bind` it rw, a
  cross-session breakout. `/etc` is not in the rw bind set.
- **Ergonomics.** One global conf means `allow-write = /cache` (uv) and
  friends apply to `claude` in every workspace with nothing added to
  individual repos. The documented user flow (2026-07-24, post
  disposable-clone install) is editing `/etc/claude-sandbox.conf`
  directly from an unsandboxed root shell — same trust boundary, the
  shadow re-reads it at every launch. Edits are per-container and are
  reset by a rebuild / re-install / `claude-sandbox update`; persistence
  across rebuilds is an open follow-up. Teams bake persistent conf into
  the pinned clone before `./install` runs.

`parse_config` still takes the path as `$1` (tests pass a fixture); only
the launch-time call site is pinned to `CONFIG_PATH`. Env vars
(`CLAUDE_SANDBOX_*`) still override per session. A team ships custom
conf by writing it into the clone before `postCreate` runs `./install`
(see docs/how-to/sandbox-a-team-devcontainer.md).

**Refuse as regressions:**
- Any change that reads the conf from `$PWD`, the workspace, or any
  sandbox-rw path. The conf source must stay outside the jail's rw set.
  **One approved carve-out, not yet implemented — issue #81**: the
  container *launcher* (host code, runs before any jailed code) may
  source `$PWD/.claude-sandbox.conf` ONLY with the full gate design —
  host-side approved-hash store in `~/.config` (outside every mount),
  print-conf-and-approve on change, content frozen at container create
  (never a live bind), out-of-tree `allow-write`/`workspace-root`
  entries becoming create-time `-v` mounts. Anything short of that
  (a live bind, an ungated read, gate state on a sandbox-reachable
  path) stays refused — it reopens the cross-session bind escalation.
  The shadow itself still reads only `/etc`; check 18 unchanged.
- "Make allow-write per-repo again so projects can opt in" reopens the
  cross-session bind-escalation vector. Per-session env vars are the
  supported override; the global conf is the only file.
- Guards to keep: verify-sandbox check 18 (installed shadow reads
  `/etc`, no `$PWD/.devcontainer` read) and `tests/bwrap_argv.sh`
  scenario 8b (`$VIRTUAL_ENV/bin` appended — not prepended — to PATH;
  harness unsets the runner's `VIRTUAL_ENV`/`UV_*`).

## Invariant 5 — the integrity guard is GLOBAL via MANAGED settings (`/etc` + `/usr/libexec`), and the in-container auto-updater stays OFF

The guard that asserts "we are actually inside the shadow" is delivered
through Claude Code's **managed-settings** layer — the highest-precedence
settings tier, which a user **cannot override or remove** by editing
their own `~/.claude/settings.json`. Two hooks, wired by
`install_guard_scripts` + `wire_managed_settings`:

- `SessionStart` → `/usr/libexec/claude-sandbox/sandbox-verify.sh`: full
  integrity battery, warns loudly when unwrapped. **Cannot block**
  (SessionStart only injects messages/context — exit 2 does *not* abort).
- `UserPromptSubmit` → `/usr/libexec/claude-sandbox/sandbox-gate.sh`:
  lean fail-closed gate, `exit 2` (blocks the prompt) unless
  `IS_SANDBOX=1`. Escape hatch: the ROOT-OWNED flag
  `/etc/claude-code/allow-unwrapped` (stamped by `install.sh` when
  `DANGEROUSLY_ALLOW_CLAUDE_SANDBOX_UNWRAPPED=1`, or `sudo touch`). It is a flag under `/etc`, NOT an
  env var, because a confined Claude can write `~/.claude/settings.json`
  (host-shared) and Claude Code exports its `env` block into later
  sessions — so the old `CLAUDE_SANDBOX_ALLOW_UNWRAPPED=1` env hatch was
  forgeable from inside the jail and persistently neutralised the gate on
  a later unwrapped launch (deep-review H4). `/etc` is ro in the sandbox
  and not host-shared. Both hooks skip on `CLAUDE_CODE_REMOTE=true`.
  **Refuse as a regression:** re-introducing an env-var escape hatch for
  the gate (or any gate-bypass signal a sandbox-rw / host-shared path can
  set) — it reopens H4. The gate test in `tests/smoke.sh` asserts the
  retired env var no longer bypasses.

**Why managed-settings + `/usr/libexec`, not user-scope `~/.claude`**
(this is the tamper-resistance that makes the native devcontainer
safe-by-construction):
- Hook **entries** in `/etc/claude-code/managed-settings.json` are
  highest-precedence and un-removable from user-scope. Editing
  `~/.claude/settings.json` (the shared cross-container file) cannot
  disable the guard — only `root` editing `/etc` or a deliberate
  `./install` can. This closes the "user edits shared settings and drops
  the hooks" reopening of the silent-disable hole.
- Hook **scripts** in `/usr/libexec/claude-sandbox/` are root-owned,
  off-PATH, and **ro inside the sandbox** (`--ro-bind / /`) — exactly
  like the relocated real binary. Under `~/.claude` they'd be rw-bound
  and a compromised session could rewrite `sandbox-gate.sh` to `exit 0`.
- Same `/etc`-not-the-rw-workspace discipline as Invariant 4.

This also superseded an earlier per-repo design (project `.claude/`
hooks) — that left folders with no project `.claude/` unguarded — and an
intermediate user-scope-`~/.claude` design (removable by editing the
shared file). Cross-scope hooks are **additive/union** (verified), and
managed hooks fire *in addition to* user/project hooks; we deliberately
do **not** set `allowManagedHooksOnly` (that would block the owner's own
hooks). The user-scope `~/.claude/settings.json` now holds only the
statusline preference, and `wire_user_statusline` **prunes** any guard
hooks an earlier install left there (single authoritative home).

**Why the auto-updater is hard-disabled** (`env.DISABLE_AUTOUPDATER=1`
+ `autoUpdates:false`, in managed settings): root-cause removal of the
bypass re-arm. Updates become a deliberate `./install`, which
re-relocates the current binary and re-asserts the shadow.
`autoUpdatesChannel:"stable"` only *slows* updates — it would NOT fix
this.

**Refuse as regressions:**
- Moving the guard back into per-repo project `.claude/` or into
  user-scope `~/.claude` (removable). It must stay in managed settings.
- Putting the guard scripts under `~/.claude` or anywhere in the sandbox
  rw set — they must stay in `/usr/libexec` (off-PATH, ro in sandbox).
- Setting `allowManagedHooksOnly` (would silence the owner's own hooks).
- Re-enabling the in-container auto-updater, or relying on
  `autoUpdatesChannel` instead of `DISABLE_AUTOUPDATER`.
- A hard-fail (vs warn-and-skip) on a non-JSON managed/user settings
  file — bricking install over a file we don't exclusively own is worse
  than skipping the merge with a loud warning.
- `tests/smoke.sh` covers all of the above (managed-merge, updater keys,
  prune migration, gate/escape-hatch behaviour) via the
  `INSTALL_PREFIX`/`INSTALL_USER_HOME` tmpdir seams — the suite never
  touches the real `/etc` or `~/.claude`. Keep those seams.

## Boundary discipline — sockets and env vars crossing the jail (#73/#74)

`allow-write` binds unix sockets (#73) and `pass-env` forwards named env
vars (#74). Both were contributed by DLS (coretl) to reach a container
engine from inside the sandbox — and the obvious way to use them is the
dangerous one. The docs draw a deliberate line, agreed 2026-07-23:

- **A container-engine socket is a sandbox escape**: the engine mounts
  any path its account can read into a container the agent controls.
  The sandbox scopes which *socket file* is reachable; it cannot
  constrain the engine behind it. Same for any socket — it's an API
  crossing the boundary.
- **Host/daily-driver engine socket (incl. one mounted into the
  devcontainer, the common DLS pattern): never.** A dedicated,
  disposable engine (e.g. `podman system service` inside the
  devcontainer, holding nothing valuable): acceptable, and it's what
  the how-to's example demonstrates.
- **pass-env forwards pointer vars (`DOCKER_HOST`), never
  secret-bearing ones** — every forwarded var is disclosed to the agent
  and everything it runs.

Warnings live in `docs/how-to/configure-workspace-scope.md` ("Reach a
container-engine socket") and `docs/how-to/pass-environment-variables.md`.

**Refuse as regressions:** softening/removing those warning callouts;
adding a doc recipe or example that binds a *host* engine socket in
(users — DLS especially — will ask for exactly this); example conf lines
that `pass-env` a token/credential var. Mechanism stays; the docs must
keep saying what it costs.

## Policy — weakening switches exist but are never advertised

DLS-rollout decision (2026-07-24, PR #5): making the sandbox trustworthy
is the operator's job, not each user's. Switches that weaken the sandbox
stay supported for operators who know the risks, but must NOT be
surfaced in user-facing docs or in code messages:

- `CLAUDE_SANDBOX_EGRESS_JAIL=0` / conf `egress-jail = 0`: never named
  in how-tos, the tutorial, README, shipped-conf comments, or the
  shadow's error/warning messages (`jail_fail` names only the real fix).
  Reference pages say "an operator opt-out exists but is deliberately
  not documented"; explanations keep their *analytical* mentions — the
  fail-closed-plus-hatch design is a fact auditors need.
- `DANGEROUSLY_ALLOW_CLAUDE_SANDBOX_UNWRAPPED=1` (install-time seam;
  renamed 2026-07-24 from `ALLOW_UNWRAPPED`, whose old name is dead —
  smoke asserts it no longer stamps the flag; the flag path
  `/etc/claude-code/allow-unwrapped` is unchanged). Managing the flag is
  documented ONLY on `docs/how-to/enforce-org-wide.md`, the IT-operator
  page — and the DLS pages (`docs/dls/`) must NOT link that page, since
  linking it surfaces the hatch to every reader.
- Verification is not a user chore: quickstarts carry no verify step;
  the verify how-to is reachable as a Next-steps pointer.

**Refuse as regressions:** any doc or message change that re-advertises
a weakening switch (naming `EGRESS_JAIL=0` in an error, restoring a
"disable the jail" section, a conf comment showing the disable line,
linking enforce-org-wide from the DLS pages), and shortening the
`DANGEROUSLY_` name back to something comfortable.

Style note for `docs/dls/` pages: no em dashes — rewrite with colons,
parentheses, or semicolons (owner preference).

## `install` picks a RELEASE by default — `--here` installs the checkout

`install` (the root shim, not `install.sh`) resolves **which revision** to
install before exec'ing `install.sh`:

| invocation | installs |
|---|---|
| `install` | newest stable release **tag** |
| `install --here` | this working tree, as checked out |
| `install --release [REF]` | REF, or newest stable tag; retargets even a pinned/dirty clone |

Why the default is a tag, not the checkout: the documented one-liner clones
`main` (unreleased work) while `claude-sandbox update` has always installed
the newest tag — so a first install and every later update disagreed about
what "current" means. Release selection now lives in `install` only, and
`cmd_update` delegates to it: **one** implementation of "what is current".

Prereleases are excluded by an explicit filter. A beta sorts *above* the
release it precedes (`2.1.0-beta.1` > `2.0.0`), so "newest tag" without the
filter would hand betas to everyone running `update`.

**The refusal is the load-bearing part.** On a pinned (detached / non-default
branch) or locally-modified clone the default **refuses** instead of
retargeting. Teams pin a revision and bump it as a reviewed act (ADR 0017);
a silent bump would defeat the pin on every teammate's next rebuild with
nothing in the diff to show for it.

**Refuse as regressions:**

- Making the default retarget a pinned or dirty clone "for convenience".
- Dropping `--here` from `.devcontainer/postCreate.sh` (this repo's own
  devcontainer — without it a rebuild replaces the branch under test with
  the last release) or from
  `docs/how-to/sandbox-a-team-devcontainer.md`.
- Re-adding tag selection to `cmd_update` (two implementations that drift).
- Removing the prerelease filter, or the `bash -c` argv trick around the
  checkout — `git checkout` rewrites `install` while bash is still reading
  it, so everything after the checkout must live in argv, not in the file.
- Letting `CLAUDE_SANDBOX_SMOKE=1` retarget: a smoke run must test the
  checkout it was pointed at.

Coverage: `tests/install_ref.sh` (hermetic — builds its own origin+clone
with synthetic tags; needs no root or caps).

## Never `sudo` — we are root inside a rootless container

The devcontainer and the published image run as **root** (`uid 0`,
`remoteUser=root`) inside a **rootless** container, so there is nothing to
escalate to. `sudo` is **not installed**: prefixing a command with it fails
with command-not-found, which reads as "the command is broken" rather than
"drop the sudo". Write `./install`, `apt-get install passt`, `touch
/etc/claude-code/allow-unwrapped` — never `sudo ./install` etc.

The security model does not weaken as a result: root-in-a-rootless-container
maps to the *unprivileged* host user, which is exactly why the container can be
root inside and why `/etc` is still a meaningful trust boundary for the sandbox
(root outside the jail, read-only inside it).

**The one exception is GitHub Actions runners** — Invariant 3's
`sudo mkdir -p /run/secrets` and the AppArmor/sysctl steps are correct there,
because the runner user is *not* root. Don't "fix" those.

Known wart (unfixed): several shipped strings still say `sudo` at users who
will be root-in-container — `sandbox-gate.sh`'s BLOCKED message and
`docs/explanations/integrity-guard.md` (`sudo touch
/etc/claude-code/allow-unwrapped`), and the `diagnostics/probe-network-*.sh`
install hints (`sudo apt-get install passt`). They're aimed at "the host
operator", who in the DLS devcontainer flow is root already.

## glab / gh helper-CLI foot-guns (`claude-sandbox glab-auth`)

Verified against glab 1.36 / gh 2.45 while fixing #11-adjacent breakage:

- **`glab auth login` has NO `--git-protocol`** (gh does; glab does not). Passing
  it aborts the login with `unknown flag`, so no token is ever stored. Set the
  protocol as config *after* login instead: `glab config set -h <host>
  git_protocol https` (per-host correctly overrides the global default).
- **glab's shipped `git_protocol` is `ssh`; gh's is `https`.** That asymmetry is
  why `gh-auth` needed no protocol handling and `glab-auth` did.
- **`glab config set <k> <v>` without `--global` or `-h` writes the
  REPOSITORY-local `.git/glab-cli/config.yml`**, and *fails outright* outside a
  git repo (`not a git repository`) — which under the CLI's `set -euo pipefail`
  aborts the command right after a successful login. Always pass `--global` or
  `-h <host>`.
- **`glab auth login` wires the git credential helper itself**; `gh` needs an
  explicit `gh auth setup-git`. Don't add a setup-git equivalent for glab.
- Testing recipe: `GLAB_CONFIG_DIR=$(mktemp -d)` isolates a real login from the
  user's credentials — but say so loudly, because a user who keeps that prefix
  for the *real* login writes their token to a throwaway dir and the sandbox
  never sees it (happened).

## Running the test suites from inside a jailed session

`tests/smoke.sh` run as-is inside a jailed claude cascade-fails ~21
checks: `link_terminal_config` operates on the real `$HOME`, where the
jail's `~/.claude.json` file bind goes ESTALE after Claude Code's
rename-replace, and `_is_mount`'s `[ -e ]` misreads ESTALE as absent.
Workaround (verified 2026-07-24, 59/59):

```bash
HOME=$(mktemp -d) CLAUDE_SANDBOX_SMOKE=1 bash tests/smoke.sh
```

`tests/bwrap_argv.sh` runs fine in-jail. `tests/egress_jail.sh` cannot
(needs `unshare`, and namespaces don't nest here) — trust CI for it.

## Third consumer — the published container image

The image `ghcr.io/diamondlightsource/claude-sandbox` + `container/claude-container`
launcher (PR #78) is the third consumer after dogfood and guest. Its
design decisions (image build sources `install.sh`, entrypoint re-runs,
PAT scoping via named containers, ro-mounted conf, notify-only launcher
versioning, parked issues #79/#80/#81) live in the
**`claude-sandbox-container` skill** — split out so they load only on
image/launcher topics. Touch the root `Dockerfile`, `container/*`, or
`.github/workflows/container.yml` → read that skill first.

## Where things live

| Concern                       | File                                                |
|-------------------------------|-----------------------------------------------------|
| bwrap argv construction + agent profiles | `.devcontainer/claude-sandbox/claude-shadow` (installed as BOTH `claude` and `codex`) |
| Codex managed guard (`/etc/codex/*.toml`) | `wire_codex_managed` in `.devcontainer/claude-sandbox/install.sh` |
| Installer (relocate + wire)   | `.devcontainer/claude-sandbox/install.sh`           |
| Root-shim installer entry     | `install`                                           |
| bwrap argv unit tests         | `tests/bwrap_argv.sh`                               |
| End-to-end install smoke test | `tests/smoke.sh`                                    |
| CI workflow                   | `.github/workflows/ci.yml`                          |
| Container image / launcher design | `claude-sandbox-container` skill (root `Dockerfile`, `container/*`, `.github/workflows/container.yml`) |
| Live verification spec (why)  | `skills/verify-sandbox/SKILL.md`                |
| Phase-1 battery script (what) | `.devcontainer/claude-sandbox/verify-sandbox-battery.sh` |
| Global SessionStart verifier  | `.devcontainer/claude-sandbox/sandbox-verify.sh`    |
| Global UserPromptSubmit gate  | `.devcontainer/claude-sandbox/sandbox-gate.sh`      |
| Threat model + binds rationale| [sphinx docs](https://diamondlightsource.github.io/claude-sandbox/explanations/threat-model.html) |
| Helper CLI (gh-auth, glab-auth, update, verify, version) | `.devcontainer/claude-sandbox/claude-sandbox` |
| Shipped skills (ro-bound into every agent session) | `skills/` + `claude-sandbox-shipped-skills` skill |
| Network egress / firewall / lateral-movement design | `claude-sandbox-networking` skill (kept separate so it loads only on network topics) |

Touching any of these → re-read this skill first.
