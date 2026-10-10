#!/usr/bin/env bash
# The orchestrate plugin (plugins/orchestrate/scripts): the goal lifecycle
# (goal.sh start/resume/pause/list/close), hooks that are silent unless the
# pointer names a goal, the session runs in its launch directory and owns
# the goal (claimed by goal.sh start/resume, handed on by /clear), the
# right injection and nudges inside the mode, the lost-agent check, log
# rotation and the scratch report. Hermetic: a temp HOME, throwaway git repos and synthetic
# transcripts; no claude binary. Needs bash, git and jq.
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
source "$HERE/lib.sh"
S="$HERE/../plugins/orchestrate/scripts"

TMP="$(mktemp -d)"
register_cleanup "$TMP"
TMP="$(cd "$TMP" && pwd -P)"
export HOME="$TMP/home"
export GIT_CONFIG_GLOBAL="$TMP/gitconfig" GIT_CONFIG_NOSYSTEM=1
git config --global user.name t && git config --global user.email t@t
git config --global init.defaultBranch main
mkdir -p "$HOME/.claude"

PROJ="$TMP/my.proj"
git init -q "$PROJ" && git -C "$PROJ" commit -q --allow-empty -m one
SLUG="$(printf '%s' "$PROJ" | sed 's|[^A-Za-z0-9]|-|g')"
TDIR="$HOME/.claude/projects/$SLUG"
ROOT="$HOME/.claude/orchestrate"
G="$ROOT/goal"
mkdir -p "$TDIR"
TR="$TDIR/now.jsonl"

goal() { OUT="$(cd "${CWD:-$PROJ}" && bash "$S/goal.sh" "$@" 2>&1)"; RC=$?; }

# hook NAME [JSON-FIELDS]: run a hook with a hook-input object on stdin.
hook() {
    local name="$1" extra="${2:-}"
    OUT="$(printf '{"session_id":"%s","transcript_path":"%s","cwd":"%s"%s}' \
        "${SID:-s1}" "$TR" "${CWD:-$PROJ}" "$extra" | bash "$S/$name.sh" 2>&1)"
}
ctx() { jq -r '.hookSpecificOutput.additionalContext' <<<"$OUT"; }

# transcript: a user prompt, then one assistant turn whose tool calls are the
# given JSON objects ({"name":..,"input":{..}}).
transcript() {
    {
        printf '{"type":"user","message":{"content":"go"},"timestamp":"2026-10-08T10:00:00Z"}\n'
        local c
        for c in "$@"; do
            jq -c -n --argjson c "$c" \
                '{type:"assistant",timestamp:"2026-10-08T10:00:01Z",message:{content:[{type:"tool_use",id:"x"} + $c]}}'
        done
    } > "$TR"
}

# bash_call COMMAND: a Bash tool call as a transcript() argument.
bash_call() { jq -c -n --arg c "$1" '{name:"Bash",input:{command:$c}}'; }
# claim: a turn of session ${SID:-s1} that ran goal.sh resume; its Stop
# hook makes that session the owner.
claim() { transcript "$(bash_call "bash $S/goal.sh resume goal")"; hook stop; }
owner() { sed -n 's/^session=//p' "$ROOT/active"; }

EDIT_SRC='{"name":"Edit","input":{"file_path":"'"$PROJ"'/src.py"}}'
READ_SRC='{"name":"Read","input":{"file_path":"'"$PROJ"'/src.py"}}'

# ---- no pointer: every hook is silent and writes nothing --------------------
transcript "$EDIT_SRC"
for h in session-start stop session-end; do
    hook "$h" ',"source":"startup","reason":"clear"'
    assert_eq "inactive $h prints nothing" "" "$OUT"
done
[ -e "$ROOT" ] && fail "inactive hooks created the state root" || pass

# ---- start ------------------------------------------------------------------
goal start 'Bad_Slug' 'x'
assert_eq "start refuses a bad slug" 1 "$RC"
goal start goal
assert_eq "start needs a goal sentence" 2 "$RC"
goal start goal 'Ship the widget | across repos'
assert_eq "start succeeds" 0 "$RC"
for d in briefs reports maps scratch; do
    [ -d "$G/$d" ] && pass || fail "start made no $d/"
done
head -1 "$G/state.md" | grep -Eq '^<!-- state: updated=20[0-9-]+T[0-9:]+Z status=active -->$' \
    && pass || fail "state.md marker: $(head -1 "$G/state.md")"
grep -qx '# Goal: Ship the widget | across repos' "$G/state.md" && pass || fail "goal line not filled"
grep -q 'heads=' "$G/state.md" && fail "marker still has heads=" || pass
grep -qF '<!-- end head -->' "$G/state.md" && pass || fail "template has no end-of-head line"
assert_eq "pointer" "slug=goal
dir=$PROJ" "$(cat "$ROOT/active")"
grep -qx '/.claude/status.md' "$PROJ/.git/info/exclude" && pass || fail "status.md not excluded"
mkdir -p "$PROJ/.claude" && echo s > "$PROJ/.claude/status.md"
assert_eq "git ignores status.md" "" "$(git -C "$PROJ" status --porcelain)"
grep -Eq '^- 20.*Z goal:goal started in ' "$G/log.md" && pass || fail "start not logged"
goal start other 'Another goal'
assert_eq "start while another is active pauses it" 0 "$RC"
grep -q '^paused:   goal$' <<<"$OUT" && pass || fail "start does not say it paused goal: $OUT"
assert_eq "pointer moved to the new goal" "slug=other
dir=$PROJ" "$(cat "$ROOT/active")"
grep -q 'status=paused' <<<"$(head -1 "$G/state.md")" && pass || fail "paused goal marker: $(head -1 "$G/state.md")"
grep -Eq 'Z goal:goal paused$' "$G/log.md" && pass || fail "pause not logged"
goal list
grep -Eq '^\* other +active ' <<<"$OUT" && pass || fail "list: active goal not starred: $OUT"
grep -Eq '^  goal +paused ' <<<"$OUT" && pass || fail "list: paused goal not shown: $OUT"
grep -q 'active: other' <<<"$OUT" && pass || fail "list: no active line"
goal start goal 'dup'
assert_eq "a refused start pauses nothing" 1 "$RC"
assert_eq "pointer unchanged after a refused start" "slug=other" "$(sed -n 1p "$ROOT/active")"
goal resume nosuch
assert_eq "a refused resume pauses nothing" 1 "$RC"
assert_eq "pointer unchanged after a refused resume" "slug=other" "$(sed -n 1p "$ROOT/active")"
goal resume goal
assert_eq "resume pauses the active goal" 0 "$RC"
grep -q '^paused:   other$' <<<"$OUT" && pass || fail "resume does not say it paused other: $OUT"
assert_eq "pointer back on goal" "slug=goal" "$(sed -n 1p "$ROOT/active")"
grep -q 'status=active' <<<"$(head -1 "$G/state.md")" && pass || fail "resumed goal not active"
grep -q 'status=paused' <<<"$(head -1 "$ROOT/other/state.md")" && pass || fail "other not paused"
goal pause
assert_eq "pause succeeds" 0 "$RC"
[ -e "$ROOT/active" ] && fail "pause left the pointer" || pass
grep -q 'status=paused' <<<"$(head -1 "$G/state.md")" && pass || fail "pause did not mark goal"
transcript "$EDIT_SRC"
hook stop
assert_eq "hooks silent while paused" "" "$OUT"
goal pause
assert_eq "pause with no active goal refuses" 1 "$RC"
goal resume goal
assert_eq "a paused goal resumes" 0 "$RC"
grep -q 'status=active' <<<"$(head -1 "$G/state.md")" && pass || fail "paused goal not reactivated"
goal resume goal
assert_eq "resume of the active goal is allowed" 0 "$RC"
assert_eq "exclude line not duplicated" 1 "$(grep -c 'status.md' "$PROJ/.git/info/exclude")"

# A launch directory below a repo root, and one outside git.
SUB="$PROJ/sub"; mkdir -p "$SUB"
NOGIT="$TMP/nogit"; mkdir -p "$NOGIT"

# ---- ownership: only the session that ran goal.sh start/resume ------------------
assert_eq "goal.sh leaves the pointer unclaimed" "" "$(owner)"
hook session-start ',"source":"startup"'
assert_eq "an unclaimed goal: start silent" "" "$OUT"
transcript "$EDIT_SRC"
hook stop
assert_eq "an unclaimed goal: stop silent" "" "$OUT"
transcript "$(bash_call "goal.sh list")"
hook stop
assert_eq "goal.sh list claims nothing" "" "$(owner)"
for c in "grep -rn 'goal.sh resume' docs/" 'echo "goal.sh start"' "sed -n '1,20p' $S/goal.sh" \
         "cat README.md | grep goal.sh resume"; do
    transcript "$(bash_call "$c")"
    hook stop
    assert_eq "a command that only mentions goal.sh claims nothing: $c" "" "$(owner)"
done
claim
ctx | grep -q "without updating" && fail "the claiming turn was nudged" || pass
ctx | grep -q "injected by the Stop hook: this session now owns the goal" && pass || fail "the claiming turn got no context: $OUT"
assert_eq "stop after goal.sh resume claims the goal" "s1" "$(owner)"
SID=s2 hook session-start ',"source":"startup"'
assert_eq "another session in the launch dir: start silent" "" "$OUT"
transcript "$EDIT_SRC" '{"name":"Agent","input":{}}'
SID=s2 hook stop
assert_eq "another session in the launch dir: stop silent" "" "$OUT"
hook stop
ctx | grep -q "without updating the state file" && pass || fail "owner: no nudge after an unrecorded edit"
LOGN="$(wc -l < "$G/log.md")"
SID=s2 hook session-end ',"reason":"logout"'
assert_eq "another session: session-end silent" "" "$OUT"
assert_eq "another session: session-end logs nothing" "$LOGN" "$(wc -l < "$G/log.md")"
SID=s2 hook session-end ',"reason":"clear"'
assert_eq "another session's clear hands nothing on" "s1" "$(owner)"
# The takeover after a crash: s1's transcript is newer than the state file
# and a Now entry has no report. The new owner is told both on its claim.
sed -i 's|^- (nothing running)$|- [sonnet] item t -> tklab -> /x/brief.md -> /x/nope.md|' "$G/state.md"
touch -d '1 hour ago' "$G/state.md"
printf '{}\n' > "$TDIR/crashed.jsonl"
transcript "$(bash_call "bash \"\$CLAUDE_PLUGIN_ROOT/scripts/goal.sh\" resume")"
SID=s2 hook stop
assert_eq "goal.sh resume (quoted plugin root) in another session takes the goal over" "s2" "$(owner)"
ctx | grep -q "Now entry tklab has no report yet" && pass || fail "takeover: no lost-agent line: $OUT"
ctx | grep -q "Previous transcript $TDIR/crashed.jsonl" && pass || fail "takeover: no crash line: $OUT"
ctx | grep -q -- "--- head of" && fail "takeover injected the head" || pass
sed -i '/-> tklab ->/s|.*|- (nothing running)|' "$G/state.md"
rm "$TDIR/crashed.jsonl"
transcript "$EDIT_SRC"
hook stop
assert_eq "the old owner is silent after a takeover" "" "$OUT"
claim
assert_eq "and takes it back the same way" "s1" "$(owner)"

# ---- inside the mode -----------------------------------------------------------
sed -i 's/^- (nothing running)$/- HEADMARK/; s/^## Decided$/## Decided\n- BODYMARK/' "$G/state.md"
hook session-start ',"source":"clear"'
C="$(ctx)"
grep -q 'Orchestrate mode is active: load the /orchestrate skill with "resume"' <<<"$C" && pass || fail "active start: no mode line"
grep -q "State file:  $G/state.md" <<<"$C" && pass || fail "active start: no state path"
grep -q "Status file: $PROJ/.claude/status.md" <<<"$C" && pass || fail "active start: no status path"
grep -q "HEADMARK" <<<"$C" && pass || fail "active start: head not injected"
grep -q "BODYMARK" <<<"$C" && fail "active start: body injected" || pass
grep -Eq "STALE|HEAD is now|git log|heads=" <<<"$C" && fail "active start still reports git: $C" || pass
assert_eq "session title" "orchestrate: goal" "$(jq -r '.hookSpecificOutput.sessionTitle' <<<"$OUT")"
hook session-start ',"source":"resume"'
ctx | grep -q "HEADMARK" && fail "resume re-injected the head" || pass
CWD="$SUB" hook session-start ',"source":"startup"'
ctx | grep -q "HEADMARK" && pass || fail "a cwd below the launch dir is not active"
CWD="$NOGIT" hook session-start ',"source":"startup"'
assert_eq "another directory: start silent" "" "$OUT"
CWD="$PROJ-x" ; mkdir -p "$CWD"; hook session-start ',"source":"startup"'; unset CWD
assert_eq "a sibling with the same prefix is not the launch dir" "" "$OUT"

transcript "$EDIT_SRC"
hook stop
ctx | grep -q "without updating the state file" && pass || fail "stop: no nudge after an unrecorded edit"
CWD="$NOGIT" hook stop
assert_eq "stop silent in another directory" "" "$OUT"
hook stop ',"stop_hook_active":true'
assert_eq "stop never loops" "" "$OUT"
transcript "$EDIT_SRC" '{"name":"Edit","input":{"file_path":"'"$G"'/state.md"}}'
hook stop
assert_eq "stop quiet when the state file was touched" "" "$OUT"
# Only a real write counts: a command that names state.md is a read.
# shellcheck disable=SC2016 # the literal $HOME, as a command names it
for c in "cat $G/state.md" "grep -n Now $G/state.md" 'sed -n 1,30p ~/.claude/orchestrate/goal/state.md' \
         'cat >> $HOME/.claude/orchestrate/goal/state.md'; do
    transcript "$EDIT_SRC" "$(bash_call "$c")"
    hook stop
    ctx | grep -q "without updating" && pass || fail "a command naming state.md counted as a state update: $c"
done
for v in "resume goal" touch "now x" "land x"; do
    transcript "$EDIT_SRC" "$(bash_call "bash $S/goal.sh $v")"
    hook stop
    assert_eq "stop quiet when the turn ran goal.sh $v" "" "$OUT"
done
transcript "$EDIT_SRC" "$(bash_call "bash $S/goal.sh list")"
hook stop
ctx | grep -q "without updating" && pass || fail "goal.sh list counted as a state update"
transcript "$EDIT_SRC" "$(bash_call 'cat >> ~/.claude/orchestrate/goal/maps/x.md')"
hook stop
ctx | grep -q "without updating" && pass || fail "a map write counted as a state update"
transcript "$EDIT_SRC" "$(bash_call 'cp x ~/.claude/orchestrate/goal/state.md.bak')"
hook stop
ctx | grep -q "without updating" && pass || fail "state.md.bak counted as a state update"
transcript "$EDIT_SRC" "$(bash_call 'ls ~/.claude/orchestrate/goal-other/state.md')"
hook stop
ctx | grep -q "without updating" && pass || fail "a sibling goal's state file counted as touched"
for f in "$G/briefs/x.md" "$G/log.md" "$PROJ/.claude/status.md" "$G/scratch/x/y"; do
    transcript '{"name":"Agent","input":{"prompt":"go"}}' '{"name":"Write","input":{"file_path":"'"$f"'"}}'
    hook stop
    ctx | grep -q "without updating the state file" && pass || fail "an agent launch plus a write to $f counted as a state update"
done
transcript '{"name":"Agent","input":{"prompt":"go"}}' '{"name":"Write","input":{"file_path":"'"$G"'/briefs/x.md"}}' \
    '{"name":"Edit","input":{"file_path":"'"$G"'/state.md"}}'
hook stop
assert_eq "an agent launch with its brief and a state edit is quiet" "" "$OUT"

# Which commands are writes. Read-only git/gh calls are no change to record.
# shellcheck disable=SC2016 # literal $(...): commands as the model writes them
for c in 'gh api repos/o/r/pulls/102/files' 'gh api repos/o/r/pulls/1/files --jq ".[] | .filename"' \
         'gh api -X GET repos/o/r/issues -f state=open' 'gh api --method=GET repos/o/r' \
         'gh api graphql -f query="{ viewer { login } }"' 'gh pr view 3' 'gh pr list' 'gh issue view 107' \
         'gh pr checks 3' 'gh release view' 'git push --dry-run origin main' 'git push -n origin main' \
         'git status && git log' 'git stash list' 'git diff | head' 'git stash show -p' \
         'grep -n "git commit" README.md' 'echo "run git checkout main first"' 'git branch -a' 'git worktree list' \
         $'gh api graphql -f query=\'\n{ viewer { login } }\'' 'git merge-base main HEAD' \
         'git diff $(git merge-base main HEAD) --stat' 'git log $(git merge-base main HEAD)..HEAD'; do
    transcript "$(bash_call "$c")"
    hook stop
    assert_eq "read-only command is no nudge: $c" "" "$OUT"
done
# shellcheck disable=SC2016 # literal $(...) and $f: commands as the model writes them
for c in 'gh api -X PATCH repos/o/r/issues/1 -f state=closed' 'gh api --method POST repos/o/r/labels' \
         'gh api repos/o/r/issues/1/comments -f body=hi' 'gh api repos/o/r/issues -F title=x' \
         'gh api graphql -f query="mutation { x }"' 'gh pr create --fill' 'gh pr merge 3' \
         'gh issue comment 107 -b hi' 'gh release create v1' 'git commit -m x' 'git -C /x/y commit -m x' \
         'git status; git push origin main' 'git push --dry-run origin main && git push origin main' \
         'git stash' 'git switch -c b' 'git commit -m "mention --dry-run here"' 'GIT_X=1 git commit -m x' \
         'git --no-pager commit -m x' 'git worktree add ../wt b' 'git revert HEAD' 'git restore a.py' 'git pull' \
         'git branch -D old' 'gh pr checkout 3' 'gh api graphql -F query=@mut.graphql' \
         'gh api -XPATCH repos/o/r/issues/1' 'gh api repos/o/r/issues/1/comments --input body.json' \
         $'gh api graphql -f query=\'\nmutation { closeIssue(input:{id:"x"}) { clientMutationId } }\'' \
         $'git status\ngit commit -m x' 'url=$(gh pr create --fill)' 'url="$(gh pr create --fill)"' \
         'out=$(git commit -m x)' '(git commit -m x)' '{ git commit -m x; }' \
         'for f in a b; do git commit -m "$f"; done' 'if git push; then echo ok; fi' 'timeout 60 git push' \
         'env GIT_X=1 git push' 'command git push' 'nohup git push' 'x=$(git stash)'; do
    transcript "$(bash_call "$c")"
    hook stop
    ctx | grep -q "without updating the state file" && pass || fail "write command gave no nudge: $c"
done
transcript "$READ_SRC" '{"name":"Write","input":{"file_path":"'"$PROJ"'/.claude/status.md"}}'
hook stop
assert_eq "stop quiet for reads and status writes" "" "$OUT"
git -C "$PROJ" commit -q --allow-empty -m two
transcript "$READ_SRC"
hook stop
assert_eq "a moved HEAD alone is no nudge" "" "$OUT"

# crash check: a previous transcript written after the state file
touch -d '2 hours ago' "$G/state.md"
printf '{}\n' > "$TDIR/prev.jsonl"
hook session-start ',"source":"startup"'
ctx | grep -q "Previous transcript $TDIR/prev.jsonl" && pass || fail "crash check missing"
ctx | grep -q "transcript-text.sh" && pass || fail "crash check gives no query command"
touch "$G/state.md"
hook session-start ',"source":"startup"'
ctx | grep -q "Previous transcript" && fail "crash check fired for a current state file" || pass

hook session-end ',"reason":"clear"'
tail -2 "$G/log.md" | head -1 | grep -Eq '^- 20[0-9-]+T[0-9:]+Z session ended \(clear\) goal:goal$' \
    && pass || fail "session end not logged: $(tail -2 "$G/log.md")"
[ -e "$G/.auto" ] && fail "session-end wrote a git snapshot" || pass

# /clear hands the goal to the session it starts; nothing else picks it up.
assert_eq "a clear in the owner marks the hand-off" "cleared:s1" "$(owner)"
SID=s3 hook session-start ',"source":"startup"'
assert_eq "a new session does not take a cleared goal" "" "$OUT"
transcript "$EDIT_SRC"
SID=s3 hook stop
assert_eq "nor does another session's stop" "" "$OUT"
SID=s2 hook session-start ',"source":"clear"'
ctx | grep -q 'Orchestrate mode is active' && pass || fail "the session after the clear got no injection"
assert_eq "the session after the clear owns the goal" "s2" "$(owner)"
SID=s2 hook session-end ',"reason":"logout"'
assert_eq "an exit does not hand the goal on" "s2" "$(owner)"
SID=s2 hook session-end ',"reason":"clear"'
hook session-start ',"source":"clear"'
assert_eq "cleared back to s1" "s1" "$(owner)"

# ---- lost-agent check ---------------------------------------------------------
mkdir -p "$G/reports"; echo r > "$G/reports/done.md"
sed -i "s|^- HEADMARK\$|- HEADMARK\n- [sonnet] item a -> lostlab -> $G/briefs/lostlab.md -> $G/reports/lostlab.md\n- [sonnet] item b -> donelab -> $G/briefs/donelab.md -> $G/reports/done.md\n- malformed -> only -> two|" "$G/state.md"
hook session-start ',"source":"startup"'
assert_eq "hook exits 0 with Now entries" 0 "$?"
grep -qF "Now entry lostlab has no report yet: still running or lost - check before relaunching from $G/briefs/lostlab.md." <<<"$(ctx)" && pass || fail "lost agent not reported: $(ctx)"
ctx | grep -q "Now entry donelab" && fail "an entry with a report was flagged" || pass
ctx | grep -q "Now entry only" && fail "a malformed Now line was flagged" || pass
sed -i '/-> lostlab ->/d' "$G/state.md"
hook session-start ',"source":"startup"'
ctx | grep -q "Now entry" && fail "lost-agent line printed with no lost entry" || pass
sed -i '/-> donelab ->/d; /^- malformed/d' "$G/state.md"

# Agents append to the log directly; rotation still sees it.
# ---- rotation: nothing lost, pointer left -------------------------------------
for i in $(seq 1 40); do echo "- 2026-10-09T10:00:00Z entry $i $(printf 'x%.0s' $(seq 1 60))" >> "$G/log.md"; done
before="$(cat "$G/log.md")"
STATE_LOG_ROTATE_BYTES=1024 hook session-start ',"source":"startup"'
rotated="$(ls "$G"/archive/log.*.md 2>/dev/null)"
[ -n "$rotated" ] && pass || fail "log not rotated past the threshold"
assert_eq "rotated log kept every byte" "$before" "$(cat "$rotated" 2>/dev/null)"
assert_eq "fresh log is one pointer entry" 1 "$(wc -l < "$G/log.md")"
grep -Eq "^- 20.*Z rotated: earlier entries are in archive/$(basename "$rotated")$" "$G/log.md" \
    && pass || fail "fresh log has no pointer: $(cat "$G/log.md")"
ctx | grep -q "entry 1 " && fail "start injected the log" || pass

# ---- scratch report -------------------------------------------------------------
mkdir -p "$G/scratch/a"
head -c 3000000 /dev/zero > "$G/scratch/a/blob"
STATE_SCRATCH_REPORT_MB=1 hook session-start ',"source":"startup"'
ctx | grep -q "Scratch in $ROOT/\*/scratch totals [0-9]* MB" && pass || fail "scratch over threshold not reported"
hook session-start ',"source":"startup"'
ctx | grep -q "Scratch in" && fail "scratch under the default threshold reported" || pass

# ---- stop: close ------------------------------------------------------
mkdir -p "$G/briefs" && echo b > "$G/briefs/a.md"
goal close
assert_eq "close succeeds" 0 "$RC"
A="$ROOT/archive/goal"
for f in state.md log.md briefs/a.md reports maps archive; do
    [ -e "$A/$f" ] && pass || fail "archive/goal/$f missing"
    [ -e "$G/$f" ] && fail "$f left in the goal folder" || pass
done
grep -q 'status=closed' "$A/state.md" && pass || fail "archived state not marked closed"
[ -f "$G/scratch/a/blob" ] && pass || fail "scratch moved or deleted"
[ -e "$ROOT/active" ] && fail "close left the pointer" || pass
grep -q "kept:     $G/scratch" <<<"$OUT" && pass || fail "close does not say scratch was kept"
transcript "$EDIT_SRC"
for h in session-start stop session-end; do
    hook "$h" ',"source":"startup","reason":"clear"'
    assert_eq "after close: $h prints nothing" "" "$OUT"
done

# ---- resume with no pointer lists goals; resume <slug> moves the launch dir ---------
CWD="$NOGIT" goal start plain 'A goal outside git'
assert_eq "start outside git" 0 "$RC"
transcript "$(bash_call "bash $S/goal.sh start plain 'A goal outside git'")"
SID=s5 CWD="$NOGIT" hook stop
assert_eq "stop after goal.sh start claims the goal" "s5" "$(owner)"
CWD="$NOGIT" hook session-start ',"source":"startup"'
assert_eq "after that claim, another session is not the owner" "" "$OUT"
[ -e "$NOGIT/.git" ] && fail "start outside git made a repo" || pass
rm "$ROOT/active"
goal resume
assert_eq "resume without a pointer asks (exit 3)" 3 "$RC"
grep -q "plain .*active.*A goal outside git" <<<"$OUT" && pass || fail "resume does not list goals: $OUT"
grep -q "1 stopped goals" <<<"$OUT" && pass || fail "resume does not count stopped goals"
goal list --all
grep -Eq '^  archive/goal +closed ' <<<"$OUT" && pass || fail "list --all omits the archived goal: $OUT"
grep -q 'stopped goals in' <<<"$OUT" && fail "list --all still prints the count" || pass
goal resume goal
assert_eq "a stopped goal cannot be resumed" 1 "$RC"
goal resume plain
assert_eq "resume <slug>" 0 "$RC"
assert_eq "resume points at this directory" "dir=$PROJ" "$(sed -n 2p "$ROOT/active")"
rm "$ROOT/active"; goal start goal 'again'
grep -q 'archived' <<<"$OUT" && pass || fail "an archived slug was reused: $OUT"

finish orchestrate_plugin
