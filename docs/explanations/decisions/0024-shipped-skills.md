(adr-shipped-skills)=

# 24. Ship agent skills by read-only bind, not into the user's config

Date: 2026-09-15

## Status

Accepted

Amended 2026-10-08: opt-in Claude Code plugins under `plugins/`, offered
through a plugin marketplace in this repository that the installer makes
known in the managed settings. The skill bind below stays the mechanism for
shipped skills; see the amendment at the end for why the marketplace's
costs, rejected here for shipped skills, are accepted for plugins.

Rests on {ref}`ADR 13 <adr-managed-settings-guard>`'s placement rule (trust
anchors under `/etc` and `/usr/libexec`, read-only inside the session) and
on the XDG home rebinds of {ref}`ADR 11 <adr-xdg-split>`.

## Context

The `vscode-headless` skill (PR #46) lets a sandboxed agent run and drive VS
Code on a virtual display. It only reached the agent when the agent's
workspace was this repository, because skills were discovered from the
repo's own `.claude/skills/`. Users of the wheel, the image, or a
clone-and-install into another devcontainer never saw it.

Claude Code discovers skills from `~/.claude/skills/<name>/SKILL.md` and
from the workspace's `.claude/skills/`; it has no system-wide skills
directory and no managed-settings key that adds one. Codex and Pi have the
same one-level shape under their own config directories.

Options considered:

- Copy skills into the user's `~/.claude/skills` at install. That directory
  is the user's own and is shared across every devcontainer that mounts the
  same terminal config; an installer writing there contaminates it, drifts
  from the installed version, and leaves a writable copy of the skill's
  scripts inside the session.
- Publish a plugin and enable it through managed settings. Still installs
  into the user's plugin cache, needs the network on first use, and covers
  Claude Code only.
- Pass a plugin directory on the command line. Launcher-only and Claude
  only; Codex and Pi would need separate mechanisms.

## Decision

Shippable skills live in a top-level `skills/` tree, separate from
`.claude/skills/` (which stays for developing this repository and never
ships). The installer copies that tree, replacing any previous copy, to
`/usr/libexec/claude-sandbox/skills/`, root-owned and world-readable. The
shadow binds each skill read-only onto the agent's own skills directory
inside the jail: `~/.claude/skills/<name>` for Claude, `~/.codex/skills/<name>`
for Codex, `~/.pi/agent/skills/<name>` for Pi. One bind per skill, so the
user's own skills in the same directory stay visible and the depth matches
what every agent discovers.

The launcher creates the agent's skills directory on the host before launch
when anything ships. That is the one deliberate host write, and it is an
empty directory the agent would create itself on first use. bwrap also
leaves one empty mount-point directory per shipped skill inside it, because
the agent's config directory is a read-write host bind.

The wheel and the image carry the same `skills/` tree through the existing
paths: the wheel's verbatim force-include and the Dockerfile's `install.sh`
function list.

## Consequences

- Every consumer (dogfood devcontainer, guest clone-and-install, wheel,
  image) gets the same skills, at the version that was installed, with no
  change to the user's `~/.claude` beyond an empty `skills/` directory and
  one empty directory per shipped skill inside it.
- A compromised session cannot edit a shipped skill's scripts: the bind is
  read-only and the source is outside the sandbox's writable set.
- A user skill with the same name as a shipped one is masked for the
  session; the launcher warns. An empty directory of that name is the mount
  point a previous session left, masks nothing and does not warn. Shipped
  skill names should be distinctive.
- Skills are visible to the agent only inside a sandboxed session. Running
  an agent unwrapped (with the gate hatch) does not see them, which is
  consistent: the skills are part of the sandbox.
- The pattern is harness-agnostic. Adding a fourth agent means adding its
  skills path to its profile, nothing else.
- Repo-development skills remain opt-in: a user who wants one copies it
  into their own `~/.claude/skills`.

## Amendment (2026-10-08): opt-in plugins from a managed marketplace

Some things a user may want are not skills: the `orchestrate` plugin
carries hooks and a mod, which a skill bind cannot deliver. They are also
ways of working a user chooses, not part of the isolation, so they should
not load for everyone.

These live as Claude Code plugins under `plugins/<name>/`, listed in a
marketplace at `.claude-plugin/marketplace.json` (name `claude-sandbox`).
The installer's managed-settings step adds that marketplace to
`extraKnownMarketplaces` in `/etc/claude-code/managed-settings.json`, so
Claude registers it at session start and the user only runs
`/plugin install <name>@claude-sandbox`. It never sets `enabledPlugins`:
installing a plugin stays the user's decision. An administrator's own
entry of the same name is kept.

The objections in the Context above still hold, and are accepted here
because they do not bite on an opt-in plugin:

- *Installs into the user's plugin cache.* A plugin the user chose lives
  where every other plugin they install lives. Nothing the isolation relies
  on is in it; its hooks run inside the jail with the session's privileges,
  like hooks in the user's own settings. A plugin script the user would run
  outside the jail is the exception: it would be writable from inside the
  session, so such a script must stay root-owned under `/usr/libexec`.
- *Needs the network on first use.* Claude Code itself needs it.
- *Claude only.* Hooks and mods are Claude-only anyway.

Rejected: loading plugins for every user with `--plugin-dir` in the claude
profile. It puts a workflow feature on the launch path, and charges every
session its skill descriptions and hook processes. Also rejected: having
the installer run `claude plugin marketplace add`. The installer runs as
root and execs nothing found through `PATH` (ADR 26), and the command writes
the invoking user's `~/.claude`; the managed key gets the same result from
a file the installer already owns.

Shipped skills are unchanged: `browser-testing`, `claude-sandbox-user`,
`verify-sandbox` and `vscode-headless` stay in `skills/` and are bound
read-only.
