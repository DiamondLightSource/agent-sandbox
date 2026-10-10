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
| `/clear` | The session the clear starts takes the goal over and is handed the state file's head and a crash check, and carries on. Nothing needs flushing first. |
| `/orchestrate pause` | Switches the mode off and keeps the goal, marked `paused`. The state is always current, so clear or quit whenever you like. |
| `/orchestrate resume [slug]` | Carries on the active goal in this session, taking it over from any other session. With no active goal, Claude lists the goals and asks which. Resuming a goal pauses any other active one. |
| `/orchestrate list` | Shows every goal and its status (`*` marks the active one). `list --all` includes stopped goals. |
| `/orchestrate stop` | Lists the worktrees of every repository in the goal's map, scratch dirs with their sizes and open PRs in merge order, then closes and archives the goal. Scratch is deleted only when you name it. |

One goal is active at a time, in one session: the session that ran
`/orchestrate start` or `/orchestrate resume`, in the directory it ran in
(and below it). Other sessions are untouched, including other sessions in
the same directory: they get no injected state and no reminders to update
it. A `/clear` in the orchestrating session passes the goal to the session
it starts; `/compact` and `claude --resume` keep it. `/fork` (or
`--fork-session`) leaves the goal with the original session, not the fork.
Switching to another conversation with `/resume` leaves the goal with the
conversation you left; it acts again when you resume that one. To carry on
in a new session (after quitting Claude, or a crash), run
`/orchestrate resume` there; it becomes the orchestrator, the old session
goes quiet, and it is told about a crash or a lost agent as a new session
would be. Start and resume a goal from that launch directory.

After upgrading from plugin version 0.2.0, run `/orchestrate resume` once in
the orchestrating session: a goal started under 0.2.0 has no owning session
recorded, so the hooks stay silent until it is resumed.

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

When a session takes the goal over (a `/clear`, or `/orchestrate resume` in a
new session) and an agent listed under `Now` has no report yet, the
orchestrator is told, so an agent lost to a crash is noticed rather than
assumed to be running.

## The state file

The goal's `state.md` is written by the orchestrator only, in one format:

- `## Now` lists what is running, one line per agent, and holds
  `- (nothing running)` when idle.
- `## Done` is the last section: one line per finished item, `- YYYY-MM-DD HH:MMZ <text>`,
  appended in time order. Agent returns and review rounds go to the log.
- `## Decided` holds rulings only, each ending `(<who>, YYYY-MM-DD)`.

The orchestrator makes the most common edits with `goal.sh` rather than by
hand: `goal.sh now '<entry>'` records a launch, `goal.sh land <label>
['<done line>']` records a return (and, with a done line, a finished item;
`land` drops the entry naming `-> <label> ->` or starting with `<label>`),
and `goal.sh touch` sets the `updated=` time in the first line after any
other edit. Nobody types that time.

The Stop and SessionStart hooks run `scripts/state-lint.sh` on the file. It
reports drift as `lint: <code>: <text>` lines: blank or empty `Now`, a `Now`
agent whose report already exists, items still pending after they merged or
closed, events or unattributed lines in `Decided`, `Done` out of order or
too long, a file over 10 KB or a head over the injected cap, a stale
`updated=`, and pending sections left unchanged while `Done` grew. The Stop
hook passes a set of findings on once; the SessionStart block always shows
them. You can run it yourself:
`bash <plugin>/scripts/state-lint.sh ~/.claude/orchestrate/<slug>/state.md`.

## Where things are

| Path | Holds |
|---|---|
| `~/.claude/orchestrate/active` | The active goal's slug, launch directory and owning session. |
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

With no active goal the hooks exit at once, reading nothing; in a session that does not own the active goal they print nothing. The skill's
description adds roughly 60 tokens to every session. Uninstall with
`/plugin uninstall orchestrate@claude-sandbox`.
