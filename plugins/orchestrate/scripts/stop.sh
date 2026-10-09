#!/usr/bin/env bash
# Stop hook - end of every turn, only in orchestrate mode: did this turn
# change something the goal folder has not recorded?
#
# A clear must cost nothing, so the state file has to be current at the end
# of every turn that changed anything. This hook catches the turns where that
# slipped: it reads the transcript since the last user prompt and nudges when
# the turn edited files, launched agents or ran a git/gh write but never
# touched the goal folder. A turn that only talked (a ruling given in chat)
# is invisible to it; the skill's per-turn rule covers that case.
#
# Uses hookSpecificOutput.additionalContext: non-error feedback, the
# conversation continues. Skipped while stop_hook_active is set, so it fires
# at most once per turn and can never loop.
#
# Never blocks: any unexpected condition exits 0 in silence.

set -uo pipefail
# shellcheck source=lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

hook_active || exit 0
[ "${HOOK_STOP_ACTIVE:-false}" = "true" ] && exit 0
[ -f "${HOOK_TRANSCRIPT:-}" ] || exit 0

# Tool calls since the last real user prompt (not a tool result).
calls="$(tail -n 3000 "$HOOK_TRANSCRIPT" | jq -R -s -c '
    [split("\n")[] | fromjson? ] as $r
    | [range(0; $r | length)
       | select($r[.].type == "user" and ($r[.].isMeta | not)
                and ($r[.].message.content
                     | (type == "string") or (type == "array" and any(.[]; .type == "text"))))]
    | (last // 0) as $i
    | [$r[$i:][] | select(.type == "assistant") | .message.content[]?
       | select(.type == "tool_use")
       | {n: .name, f: (.input.file_path // ""), c: (.input.command // "")}]
' 2>/dev/null)"
[ -n "$calls" ] || calls='[]'

# Bookkeeping (the mode's own folder, the status file) is not a change to record.
substantive="$(jq -r --arg root "$ROOT" '
    any(.[];
        ((.n | IN("Edit", "Write", "NotebookEdit", "Agent"))
         and (.f | startswith($root) | not) and (.f | endswith("/.claude/status.md") | not))
        or (.n == "Bash" and (.c | test("git +(commit|push|rebase|reset|checkout|switch|merge|cherry-pick|stash)|gh +(pr|issue|api|release)"))))
' <<<"$calls" 2>/dev/null)"

# The goal folder counts as touched when a call names it - absolutely, as
# ~/... or as $HOME/... - or a path under it, but not a sibling such as
# <slug>-other.
touched="$(jq -r --arg a "$GOAL_DIR" --arg t "~/.claude/orchestrate/$SLUG" --arg h "\$HOME/.claude/orchestrate/$SLUG" '
    def names($d): (.f | startswith($d + "/"))
        or (.c | split($d)[1:] | any(.[]; test("^[A-Za-z0-9_.-]") | not));
    any(.[]; names($a) or names($t) or names($h))' <<<"$calls" 2>/dev/null)"

[ "$substantive" = "true" ] && [ "$touched" != "true" ] || exit 0

msg="Orchestrate state check: this turn changed things (edits, agents or git/gh writes) without updating the goal folder.
Update $STATE_FILE with a small Edit - Now/Next and anything decided - and set the marker's updated=$(now_iso).
Do not repeat your previous reply; end the turn with at most one line."

jq -n --rawfile ctx /dev/stdin \
   '{hookSpecificOutput:{hookEventName:"Stop",additionalContext:$ctx}}' \
   <<<"$msg" 2>/dev/null || exit 0
exit 0
