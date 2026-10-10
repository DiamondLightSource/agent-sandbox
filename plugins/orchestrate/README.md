# orchestrate

Orchestrate mode for Claude Code: the main session holds the goal, a state
file and a map of the work, and delegates detailed work to subagents.
Install, usage and file layout:
[Use orchestrate mode](https://diamondlightsource.github.io/claude-sandbox/how-to/use-orchestrate-mode.html).

| Part | Path |
|---|---|
| Entry skill, its verbs and rules | `skills/orchestrate/SKILL.md`, `skills/orchestrate/references/state-file.md` |
| Goal lifecycle (start, resume, pause, list, close), the pointer, and the scripted state edits (touch, now, land) | `scripts/goal.sh` |
| State file lint, run by the Stop and SessionStart hooks | `scripts/state-lint.sh` |
| Hooks (active only for the pointer's goal, in its launch directory, in the session that owns it) | `hooks/hooks.json`, `scripts/session-start.sh`, `scripts/stop.sh`, `scripts/session-end.sh` |
| Shared helpers, log rotation, thresholds | `scripts/lib.sh` |
| Transcript to text, for recovery queries | `scripts/transcript-text.sh` |
| Quiet state writes (a mod, early-access API) | `hooks/quiet-state.tsx`, tested by `claude plugin test .` |

Needs bash, git, jq and GNU coreutils, all present in the claude-sandbox
image. Hook tests: `bash tests/orchestrate_plugin.sh` from the repository
root.
