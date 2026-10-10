# State file template

`goal.sh start` writes `<goal>/state.md` from the block below, filling the
marker and the goal line. The rules for keeping it are in `SKILL.md`;
`scripts/state-lint.sh` checks them. `goal.sh now` and `goal.sh land` edit
`Now` and append to `Done`, which stays the last section.

```markdown
<!-- state: updated=<ISO UTC> status=active -->
# Goal: <one sentence; rewrite it when it sharpens, and say so>

## Now
- (nothing running)

## Next
- <the exact next step: a command, an edit, or what you were about to say>

## Invariants
- <only rules whose breach does damage before you would think to look>
<!-- end head -->

## Awaiting user
## Queue
## Release gates
## Decided
- <ruling> - <why> (<who>, <YYYY-MM-DD>)
## Open
- (user) <question>
- (me) <question>
## Map
- <repo or system>: <goal>/maps/<area>.md; branch:<name>, <repo>#<n>, worktree <path>
## Done
- <YYYY-MM-DD HH:MMZ> <one line per finished item, with the report pointer>
```
