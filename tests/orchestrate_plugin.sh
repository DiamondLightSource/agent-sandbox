#!/usr/bin/env bash
# The orchestrate plugin (plugins/orchestrate/scripts): the goal lifecycle
# (goal.sh start/resume/pause/list/close), hooks that are silent unless the
# pointer names a goal and the session runs in its launch directory, the
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
    OUT="$(printf '{"session_id":"s1","transcript_path":"%s","cwd":"%s"%s}' \
        "$TR" "${CWD:-$PROJ}" "$extra" | bash "$S/$name.sh" 2>&1)"
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
ctx | grep -q "without updating the goal folder" && pass || fail "stop: no nudge after an unrecorded edit"
CWD="$NOGIT" hook stop
assert_eq "stop silent in another directory" "" "$OUT"
hook stop ',"stop_hook_active":true'
assert_eq "stop never loops" "" "$OUT"
transcript "$EDIT_SRC" '{"name":"Edit","input":{"file_path":"'"$G"'/state.md"}}'
hook stop
assert_eq "stop quiet when the state file was touched" "" "$OUT"
transcript "$EDIT_SRC" '{"name":"Bash","input":{"command":"cat >> ~/.claude/orchestrate/goal/maps/x.md"}}'
hook stop
assert_eq "stop quiet when a command names the goal folder as ~/" "" "$OUT"
transcript "$EDIT_SRC" '{"name":"Bash","input":{"command":"ls ~/.claude/orchestrate/goal-other/"}}'
hook stop
ctx | grep -q "without updating" && pass || fail "a sibling goal folder counted as touched"
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
