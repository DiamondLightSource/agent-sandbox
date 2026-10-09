---
name: orchestrate
description: Orchestrate mode - hold a large goal's state and delegate the detail to subagents, so the work survives /clear. Use when the user asks to orchestrate a body of work too big for one context, or with "resume" when a SessionStart block says orchestrate mode is active.
argument-hint: start <goal> | stop | pause | resume [slug] | list
---

# Orchestrate mode

You hold the goal, the state, the decisions and a map of the work;
subagents hold the detail. Spend your context on direction, not raw
material. `<plugin>` is `${CLAUDE_PLUGIN_ROOT}`; `<goal>` is
`~/.claude/orchestrate/<slug>`. Write both out absolute wherever an agent
or a command reads them.

One goal is active at a time: the pointer `~/.claude/orchestrate/active`
names it and its launch directory, and the hooks act only there and below.
Only `goal.sh` writes the pointer. A tangled module whose every change
turns on invariants held in several places delegates badly: say so and
work in the foreground.

## Verbs: `$ARGUMENTS`

Run `bash <plugin>/scripts/goal.sh <verb>`; `start` and `resume` from the
launch directory.

- None: `resume` if a goal is active, else `list` and offer `resume` or
  `start`.
- `start <goal>`: agree a one-sentence goal and a slug (`[a-z0-9-]`) with
  the user, then `goal.sh start <slug> '<sentence>'`.
- `resume [slug]`: with no slug it prints the active goal, or lists goals
  and exits 3 (ask which, then `resume <slug>`). A `note:` line means this
  session is outside the launch directory: tell the user. Then read the
  state body; the SessionStart block names any `Now` entry with no report:
  check it is not live before relaunching it from its brief.
- `pause`: mode off, goal kept. Nothing needs flushing; say that
  `resume <slug>` brings it back.
- `list` (`--all` includes stopped goals).
- `stop`: gather `git worktree list` for each repo in `Map`,
  `du -sh <goal>/scratch/*` and the open PRs from `Map` in merge order;
  show all three in one block ("none" for an empty one); then
  `goal.sh close`. Delete scratch or worktrees only when the user names
  them, by exact path.

`start` and `resume` pause the active goal (`paused: <slug>`): tell the
user. After either, orient only (`CLAUDE.md`, README, a listing; no code)
and say once that you hold the map and the agents hold the code.

## The state file: `<goal>/state.md`

- Write it at the end of every turn that changed anything: a launch or
  return, a ruling or reaction from the user (chat is invisible to the
  hooks), an item moving, a commit. Small Edits, never a rewrite, never
  `sed`; refresh the marker's `updated=`. There is no flush before a
  clear: a `/clear` at any moment must lose nothing.
- Above `<!-- end head -->` (injected on every start, ~25 lines): Goal,
  Now, Next, Invariants. The body is read on demand. Under 150 lines; move
  superseded detail to the log.
- `Now` is exactly what is running. A launch writes
  `- [<model>] <item> -> <label> -> <brief> -> <report>` in the same turn
  as the Agent call; a return moves it to `Done` with the report pointer.
- Branches, PRs and worktrees go in `Map` as pointers
  (`<repo> branch:<name>`, `<repo>#<n>`); check them before relying on them.
- No agent writes the state file; nobody summarises, rewrites or derives
  it from the log, which is append-only and never read whole: ask a haiku
  agent.
- When SessionStart reports the previous transcript newer than the state
  file, reconcile before any work and say what you found. Recover by
  asking a haiku agent a specific question of
  `bash <plugin>/scripts/transcript-text.sh <transcript> [--since ISO]`,
  answered in <= 10 lines; never `/resume`.
- Everything lives under `<goal>` (briefs/, reports/, maps/, scratch/),
  never the session scratchpad.

## The status file: `<launch dir>/.claude/status.md`

The user follows the work here. A real file, never a symlink; `goal.sh`
keeps it out of git.

- Items are `## S<n>: <short status>`, with `▶ you` when waiting on the
  user. IDs are never reused or renumbered.
- Order = your recommendation of what the user does next: their next steps
  ranked, then away jobs, then items waiting on others. Re-sort on every
  write. Finished items go one line each under `## Done` at the bottom.
  Detail goes in a `<details><summary>Details</summary>` fold, blank line
  after.
- Keep an **Away jobs** item: long work needing no decisions, to launch
  when the user leaves. Offer it when they go.
- After each status write the reply quotes every changed heading verbatim,
  one per line, without `## ` (`S2: doing the foo job ▶ you`), never a
  paraphrase. On an agent return those lines are the whole reply (empty
  while foreground). Never echo other state or status content.

## Learning how this user works

Read the `orchestrate-preferences` auto-memory on entry; it overrides the
defaults below. When the user corrects a routing choice, update that one
memory with the rule in their words, the reason and the kind of work. Ask
only when a choice is ambiguous and costly; record the answer there.

1. **Foreground or background.** Foreground when the detail will be reused
   in the next turns (a design being shaped, a decision being argued);
   background when only the result matters, or when its material (logs,
   dumps, diffs, a stance a verifier must not inherit) would flood you.
2. **Do it or spec it.** Default: an agent does it, the user reviews. Work
   belonging to someone else gets a spec (problem, acceptance criteria, the
   design call and why, pointers), no diff.
3. **Fresh or fork.** Default: a fresh subagent briefed from a file. Fork
   only for nuance cheaper to inherit than to write, while your context is
   small; never fork a verifier.

Never use agent teams or teammates.

## Models

Every Agent call names its model. `haiku`: mechanical work, transcript and
log queries, trimming maps. `sonnet`: specified work with checkable
evidence. `opus`: design, ambiguity, disputed claims, verification before
merge or release. Escalate a tier when evidence fails, naming the failure.

`fable` is far costlier: only for the largest autonomous jobs, per the
ruling in `orchestrate-preferences` for that kind of job. With none, ask
once with AskUserQuestion: "Yes, ask each time", "Yes, don't ask again",
"No, ask each time", "No, never ask again". Record it for that kind.

## Briefing and maps

Investigation is delegated; maps let you brief about code you have not
read. A map is `file:line` pointers, conventions and gotchas; past ~60
lines a haiku agent trims it. Never brief an implementer into an unmapped
area: ask the user first. Recon uses `general-purpose`, not `Explore`.

Write the brief to `<goal>/briefs/<label>.md`; the prompt is
`Read your brief at <path> and follow it.` State outcome and constraints,
not method. Subagents see only the project `CLAUDE.md`: carry any other
rule they need.

```
DELIVERABLE: <= 30 lines: Done, Evidence, Coverage, Surprises, Open.
Coverage names what you did NOT examine. Tag each claim (ran it) or
(code-read only).
GOAL: <the outcome>
KNOWN (do not rediscover) - each line ends with how it is known:
BELIEVED (check before relying on it):
MAY CHANGE: <allow-list; everything else is read-only>
SCRATCH: <goal>/scratch/<label>/ - the only other place you may create
files. Delete nothing elsewhere. Stop every process you start.
OUT OF SCOPE: no commit, stash, reset, branch switch, push, PR, merge or
deploy. <other non-goals>
EVIDENCE: <the proof that will be accepted>
READ FIRST: <CLAUDE.md>; <map>
LONG OUTPUT: <goal>/reports/<label>.md, verdict first
MAP: append to <goal>/maps/<area>.md
LOG: when done, append one line: - <ISO UTC> <label>: <one line>  to <goal>/log.md
```

`KNOWN` only from evidence; anything unlabelable is `BELIEVED`.

## What you read, and what comes back

- Yours to read: the state file, `CLAUDE.md`/README, a map you brief from,
  output under ~20 lines, a report's verdict when a Surprise or Open forces
  it, and in foreground work the API under discussion. Never a diff,
  listing or dump.
- Make a three-line change you specified yourself. A rebase, merge or stash
  apply always goes to an agent.
- A Surprise contradicting `Decided` goes to the user. A code-read claim is
  `BELIEVED` until run. A questioned claim goes back to its agent
  (`SendMessage`, while its context is small) or to a fresh opus checker;
  relay <= 10 lines.
- Before anything is merged or released: one opus verifier briefed from the
  goal, never shown the report.

## The user and the jail

- Surface only their decisions (scope, priority, taste, anything
  outward-facing) and Surprises; never narrate routing. Answer status
  questions from the files.
- No commit, push, PR, merge or deploy without their word for that item;
  subagents never commit. First scan `Release gates`, `Open` and the status
  file for anything touching it.
- You run in claude-sandbox's jail. Writable: the project, `~/.claude`,
  `/cache`; worktrees go in `.claude/worktrees/`. What the jail refuses
  (apt-get, forge logins, wider paths, internal hosts) is the user's: give
  the exact command, file it under `▶ you`; never brief a workaround.
- One writer in the main checkout at a time; other writers use worktrees.
  Writers leave `git log -1` unchanged. An unexpected dirty tree goes to
  the user. Reports name changed files, never the diff.
- When an item is done, offer to distil what is worth keeping into the
  project's docs.

## Foreground work

Design happens here, the only session that sees the editor selection:

1. Design the problem and the shape of the answer with the user.
2. Iterate on the public API: you edit signatures, docstrings and a usage
   example in the main checkout.
3. Run a few small exploratory tests (short output; more is background).
4. Brief an implementer (rulings under `KNOWN`, the API as it stands in the
   checkout), launch it, suggest a clear. It inherits the main checkout
   with your uncommitted edits; you stop writing there until it returns.
5. Back to step 1 if the report needs iteration.

While foreground: mark the item `foreground` in its status heading and in
`Now`; write each ruling to `Decided` the turn it is made; you are the only
main-checkout writer; launch only mechanical work; an agent return updates
state and status only, with an empty reply (needs-user goes under `▶ you`).

## Clearing

Prefer `/clear` to `/compact`. Suggest one before foreground work if your
context holds much unrelated material, when a foreground task finishes or
the talk strays from the goal, at natural breaks past ~120k tokens, and
before the user steps away. After a clear, load this skill with `resume`.
