---
name: claude-sandbox-shipped-skills
description: How agent skills ship to claude-sandbox users (ADR 24). The top-level `skills/` tree is installed root-owned under /usr/libexec and ro-bound per skill into each agent's own skills dir INSIDE the jail; `.claude/skills/` is for developing this repo and never ships. Surface before adding, moving or removing a skill, before touching `installer/steps.py` `plan_skills`, `profiles.py` `SHIPPED_SKILLS_DIR` or a profile's `skills_rel`, the skill binds in `bwrap.py`, or before any proposal to copy skills into the user's ~/.claude, install them via a marketplace, or make the bind writable.
---

# claude-sandbox-shipped-skills

Skills that sandboxed agents should have in every workspace (currently
`vscode-headless`, `browser-testing`, `claude-sandbox-user` and `verify-sandbox`) reach users through the **sandbox itself**, not through
their `~/.claude`. This skill records the pattern; the `claude-sandbox`
skill records the invariants it rests on (4 and 5: trust anchors live in
`/etc` and `/usr/libexec`, ro in-session, never in a host-shared or
sandbox-rw path).

## Two trees, one rule

| Tree | Purpose | Ships? |
|---|---|---|
| `skills/<name>/` | Skills every sandboxed agent gets | **Yes** |
| `.claude/skills/<name>/` | Skills for developing this repo (invariants, container, networking, triage...) | No |

The path alone tells a reviewer whether a skill reaches users. There is no
manifest or opt-in list to keep in step. A repo-dev skill that a user wants
anyway is theirs to copy into their own `~/.claude/skills` by hand; the
sandbox never does that for them.

`.claude/skills/vscode-headless` is a **symlink** to `../../skills/vscode-headless`
so the skill still loads while developing here. Add the same symlink for any
new shipped skill only if it is useful for repo work; most will not need it.

## How a shipped skill reaches the session

1. The installer's `plan_skills` step (`src/claude_sandbox/installer/steps.py`,
   reported as `install_shipped_skills`) copies `skills/*/` (dirs with a
   `SKILL.md`) to `/usr/libexec/claude-sandbox/skills/`, root-owned,
   world-readable. The tree is **replaced**, not merged, so a skill deleted
   from the repo stops shipping on the next install.
2. `bwrap.py` emits one `--ro-bind` **per skill** from that tree
   (`profiles.SHIPPED_SKILLS_DIR`) onto `$HOME/<skills_rel>/<name>`.
   `skills_rel` is a profile field (`profiles.py`): `.claude/skills`
   (Claude), `.codex/skills` (Codex), `.pi/agent/skills` (Pi).
3. The shadow (`shadow.py`) pre-creates `~/<skills_rel>` on the host when
   anything ships (the one deliberate host write; `~/.claude` always exists,
   either in the container or as the shared mount) and warns when a host
   skill of the same name is masked for the session
   (`prepare_shipped_skills`). bwrap leaves an empty mount point per shipped
   skill in that host dir. The warning therefore ignores an empty dir, or
   every launch after the first would warn.
4. The wheel force-includes `skills/` into its bundled tree (root
   `pyproject.toml`) and the image build runs the same installer, so clone,
   wheel and image all ship the same tree. CI byte-diffs the wheel's copy against the checkout.

Why per skill and not per tree: Claude Code, Codex and Pi all discover
`<skills dir>/<name>/SKILL.md` exactly one level deep, so binding the tree
would bury every skill one level down. Per-skill binds also leave the user's
own skills in the same directory visible beside the shipped ones.

Why an ro bind and not a copy into `~/.claude`: the user's `~/.claude` is
host-shared across devcontainers (Invariant 2) and rw in-session. A copy
there contaminates a folder the user owns, drifts from the installed
version, and hands a compromised session a writable copy of the skill's
scripts. Under `/usr/libexec` the scripts are exactly what `install` placed.

Why not a managed setting or plugin marketplace: Claude Code has no
system-wide skills directory and no managed key that adds a skills path
(checked 2026-09-15 against the skills and managed-settings docs). Managed
`extraKnownMarketplaces` still installs into the user's plugin cache, needs
network at first use, and is Claude-only. The bind is harness-agnostic.

Shipped 2026-09-15 as 4.2.0 (PR #49). Verified live in all three agents,
check 03 of the battery, and the wheel path from a branch. The full
`verify-sandbox` audit now uses this same skill mechanism; the former
repository-only `.claude/commands/verify-sandbox.md` has been removed.

## Adding a shipped skill

- Put it at `skills/<name>/SKILL.md` (+ `scripts/` if needed). Scripts are
  called by absolute path from inside the session; they run inside the jail
  with the session's privileges, nothing more.
- A skill's scripts that need host-side setup (apt installs, as
  `install-vscode-driver-deps.sh` does) must tell the agent to ask the user
  to run them **outside** the sandbox and must not be extended beyond that.
  Give the user the installed script path under
  `/usr/libexec/claude-sandbox/skills/<name>/scripts/`, which exists in the
  outer container and is read-only inside the sandbox. No copy is needed.
  Never use the agent's `~/.claude/skills` mount path for outer commands;
  that mount exists only inside the jail.
- Prefix names distinctively enough that they will not collide with a user's
  own skill: a collision is masked for the session, with a warning.
- Tests: `tests/python/test_argv.py`
  (`test_shipped_skills_are_bound_read_only_per_skill` at each agent's path,
  the builder stays pure; `test_no_shipped_skills`,
  `test_skills_come_in_code_point_order`); `tests/python/test_shadow.py`
  (`test_shipped_skills_warn_only_when_they_mask_something`: an empty mount
  point does not warn); `tests/python/test_installer_steps.py` (the tree is
  replaced, only dirs with a `SKILL.md` ship); `tests/smoke.sh` (installed tree byte-equals `skills/`, nothing written under the test home's
  `~/.claude/skills`). CI's wheel job diffs `tree/skills` against `skills/`.

## Refuse as regressions

- Copying shipped skills into the user's `~/.claude` (host-shared, rw)
  or any path in the sandbox rw set. The source of a bind stays root-owned
  under `/usr/libexec`.
- Turning the per-skill `--ro-bind` into `--bind`, or binding the whole
  tree onto the skills dir (hides the user's own skills, wrong depth).
- Making `SHIPPED_SKILLS_DIR` an env or conf seam: a bind source chosen
  from outside the trust boundary is a new way into the jail. Tests pass a
  directory as an argument to `bwrap_argv` (and the shadow's host paths),
  never through the environment.
- Merging instead of replacing the installed tree (stale skills keep
  shipping with no source to audit).
- Shipping anything under `.claude/skills/` by widening the copy glob.
  Move the skill to `skills/` instead, deliberately.
- A skill whose scripts run unsandboxed on the user's behalf beyond a
  documented package install.
