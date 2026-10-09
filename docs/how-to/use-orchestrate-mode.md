# Use orchestrate mode

Orchestrate mode is an opt-in Claude Code plugin for work too large for one
context window. The main session becomes the orchestrator: it holds the
goal, a state file and a map of the work, and delegates the detailed work
to subagents. A goal's state lives in its own folder outside every
repository and is kept current every turn, so you can `/clear` at any
moment and lose nothing, and one goal can span several repositories.

## Install

The installer makes this repository's plugin marketplace, `claude-sandbox`,
known to Claude through the managed settings, so you do not need to add it.
Installing a plugin from it is your choice. In a sandboxed Claude session:

```text
/plugin install orchestrate@claude-sandbox
```

Then run `/reload-plugins` or restart Claude. The plugin lands in your
`~/.claude/plugins/`, so it persists across container recreation like the
rest of your Claude settings. The sandbox turns Claude's auto-updates off,
which also stops plugin auto-update: run
`/plugin marketplace update claude-sandbox` to pick up a newer version.

On an install made before the marketplace was added to the managed
settings, run `/plugin marketplace add DiamondLightSource/claude-sandbox`
first, or reinstall claude-sandbox.

## Use

| You type | What happens |
|---|---|
| `/orchestrate start <goal>` | Claude agrees a one-sentence goal and a slug with you, creates the goal folder and makes it the active goal for this directory. If another goal was active, it is paused. |
| `/orchestrate` | Carries on the active goal; with none, lists the goals and offers to resume one or start a new one. |
| `/clear` | The next session is handed the state file's head and a crash check, and carries on. Nothing needs flushing first. |
| `/orchestrate pause` | Switches the mode off and keeps the goal, marked `paused`. The state is always current, so clear or quit whenever you like. |
| `/orchestrate resume [slug]` | Carries on the active goal. With no active goal, Claude lists the goals and asks which. Resuming a goal pauses any other active one. |
| `/orchestrate list` | Shows every goal and its status (`*` marks the active one). `list --all` includes stopped goals. |
| `/orchestrate stop` | Lists the worktrees of every repository in the goal's map, scratch dirs with their sizes and open PRs in merge order, then closes and archives the goal. Scratch is deleted only when you name it. |

One goal is active at a time, and only in the directory you started it in
(and below it). Sessions elsewhere are untouched. Start and resume a goal
from that launch directory.

Follow the work in `.claude/status.md` in that directory, ordered as the
orchestrator's recommendation of what you do next. It is a real file there,
not a link, because editors and the claude.ai web view refuse files outside
the working directory. In a git repository it is added to
`.git/info/exclude`, so it is never committed. Each time Claude updates it,
the reply shows the changed headings, one line each. Writes to the goal
folder and the status file are drawn as one dim line instead of a diff.

Design work happens in the foreground, with the orchestrator itself, since
it is the session that sees your editor selection: discuss, shape the
public API together, run a few exploratory tests, then the orchestrator
briefs an agent to write the code and suggests a `/clear`. If the agent's
report raises something, the item comes back to the foreground. While you
are designing, background agents keep running and the status file keeps
updating, but nothing interrupts the conversation.

The orchestrator learns how you like work split - what you keep in the
foreground, what you want specified for someone else rather than done, when
an agent should see the conversation, whether a big job may use Fable - from
your corrections and answers, and keeps it in Claude's auto-memory.

When a new session starts and an agent listed under `Now` has no report yet,
the orchestrator is told, so an agent lost to a crash is noticed rather than
assumed to be running.

## Where things are

| Path | Holds |
|---|---|
| `~/.claude/orchestrate/active` | The active goal's slug and launch directory. |
| `~/.claude/orchestrate/<slug>/state.md` | The state file: an injected head and a body read on demand. |
| `~/.claude/orchestrate/<slug>/log.md` | Append-only history, one dated bullet per entry. Never injected; agents append to it directly. |
| `~/.claude/orchestrate/<slug>/briefs/`, `reports/`, `maps/` | Agent briefs, their long reports, and maps of the code. |
| `~/.claude/orchestrate/<slug>/scratch/` | Agents' clones, venvs and test runs. |
| `~/.claude/orchestrate/archive/<slug>/` | A stopped goal, everything but its scratch. |

Nothing is deleted automatically. A log past 64 KiB is moved into the
goal's `archive/` and the fresh log points at it. A stopped goal's scratch
stays where it is (it can hold git worktrees, which a move would break), and
once scratch passes 1 GiB in total the orchestrator offers you a cleanup.

## Cost when off

With no active goal the hooks exit at once, reading nothing. The skill's
description adds roughly 60 tokens to every session. Uninstall with
`/plugin uninstall orchestrate@claude-sandbox`.
