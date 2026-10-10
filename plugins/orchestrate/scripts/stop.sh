#!/usr/bin/env bash
# Stop hook - end of every turn, only in orchestrate mode: did this turn
# change something the state file has not recorded?
#
# A clear must cost nothing, so the state file has to be current at the end
# of every turn that changed anything. This hook catches the turns where that
# slipped: it reads the transcript since the last user prompt and nudges when
# the turn edited files, launched agents or ran a git/gh write but never
# wrote the state file. Writing a brief, the log, a map or the status file
# is not a state update. A turn that only talked (a ruling given in chat)
# is invisible to it; the skill's per-turn rule covers that case.
#
# Only the session that owns the goal is checked. A turn that ran
# `goal.sh start` or `goal.sh resume` claims the goal for its session first
# (lib.sh, Ownership) and is given the SessionStart checks it missed (crash,
# lost agents, scratch); any other session is silent.
#
# Uses hookSpecificOutput.additionalContext: non-error feedback, the
# conversation continues. Skipped while stop_hook_active is set, so it fires
# at most once per turn and can never loop.
#
# Never blocks: any unexpected condition exits 0 in silence.

set -uo pipefail
# shellcheck source=lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

hook_in_scope || exit 0
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

# jq helpers. Each Bash command is split into simple commands (at &&, ||,
# ;, |, newline, and the ( or { that opens $(...), a subshell or a group),
# so a read-only part of a compound command cannot hide a write, nor a
# write flag leak into a read. A verb counts only as the command word of
# its simple command (after VAR=value, a shell keyword such as do or if, or
# env, command, exec, nohup, time, timeout N), not inside an argument such
# as `grep "git commit"`; a git flag counts only outside quotes.
JQ_DEFS="$(cat <<'JQ'
def segs: [.c | splits("&&|\\|\\||[;|\n({]")];
def pre: "^\\s*(?:(?:[A-Za-z_][A-Za-z0-9_]*=\\S*|do|then|else|if|elif|while|until|time|env|command|exec|nohup|timeout\\s+\\S+)\\s+)*";
def goal_sh($verbs): test(pre + "(?:(?:ba)?sh\\s+)?[\"']?[^\\s\"']*goal\\.sh[\"']?\\s+(?:" + $verbs + ")\\b");
def unquoted: gsub("\"[^\"]*\"|'[^']*'"; "''");
def git_write: unquoted | test(pre + "git(?:\\s+(?:-[Cc]\\s+\\S+|--\\S+))*\\s+(?:commit|push|rebase|reset|checkout|switch|merge|cherry-pick|revert|restore|pull|stash|worktree\\s+(?:add|remove|move|prune)|branch\\b(?=.*\\s(?:-[dDmMcCf]|--delete|--move|--copy|--force)(?:\\s|$)))(?=[\\s)]|$)")
    and (test("(?:^|\\s)--dry-run(?:\\s|=|$)|\\bpush\\b.*\\s-n(?:\\s|$)|\\bstash\\s+(?:list|show)\\b") | not);
def gh_write($all): test(pre + "gh\\s+(?:pr\\s+(?:create|merge|edit|close|reopen|comment|review|ready|checkout)|issue\\s+(?:create|edit|close|reopen|comment|transfer|delete|lock|unlock|pin|unpin)|release\\s+(?:create|edit|delete|upload))\\b")
    or (test(pre + "gh\\s+api\\b") and
        (if test(pre + "gh\\s+api\\s+graphql\\b")
         then ($all | test("\\bmutation\\b")) or test("(?:-F|--field)[\\s=]*query=@")
         elif test("(?:-X\\s*|--method[\\s=]+)(?:GET|get)\\b") then false
         else test("(?:-X\\s*|--method[\\s=]+)[A-Za-z]+|(?:^|\\s)(?:-[fF]|--field|--raw-field|--input)(?:\\s|=|$)") end));
JQ
)"

# Did this turn run goal.sh start or resume? That makes this session the
# owner. (A claim needs the tool call to be in the transcript by the time
# Stop runs; Claude Code writes it before the turn's final message.)
ran_goal_sh="$(jq -r "$JQ_DEFS"'
    any(.[]; .n == "Bash" and (segs | any(.[]; goal_sh("start|resume"))))' <<<"$calls" 2>/dev/null)"
if ! hook_owner; then
    [ "$ran_goal_sh" = "true" ] && [ -n "${HOOK_SESSION_ID:-}" ] || exit 0
    pointer_set_owner "$HOOK_SESSION_ID" || exit 0
    # A session that takes the goal over (after a crash, a restart or a
    # quit) had no SessionStart block: give it the checks now.
    goal_context "the Stop hook: this session now owns the goal" \
        'This session took the goal over with goal.sh. Reconcile any crash or lost-agent lines below before other work.' no \
        | jq -n --rawfile ctx /dev/stdin \
            '{hookSpecificOutput:{hookEventName:"Stop",additionalContext:$ctx}}' 2>/dev/null
    exit 0
fi

# Bookkeeping (the mode's own folder, the status file) is not a change to
# record, and neither is a read: only git commands that change a ref, the
# index or the work tree, and gh commands that write to the forge.
substantive="$(jq -r --arg root "$ROOT" "$JQ_DEFS"'
    any(.[];
        ((.n | IN("Edit", "Write", "NotebookEdit", "Agent"))
         and (.f | startswith($root) | not) and (.f | endswith("/.claude/status.md") | not))
        or (.n == "Bash" and (.c as $all | segs | any(.[]; git_write or gh_write($all)))))
' <<<"$calls" 2>/dev/null)"

# The state file counts as written only by a real write: an Edit, Write or
# NotebookEdit of it, or a goal.sh verb that writes it. A command that only
# names it (a cat or grep) is a read, and a brief, the log, a map or the
# status file is not the state file.
touched="$(jq -r --arg s "$STATE_FILE" "$JQ_DEFS"'
    any(.[]; (.n | IN("Edit", "Write", "NotebookEdit")) and .f == $s)
    or any(.[]; .n == "Bash" and (segs | any(.[]; goal_sh("start|resume|touch|now|land"))))' \
    <<<"$calls" 2>/dev/null)"

[ "$substantive" = "true" ] && [ "$touched" != "true" ] || exit 0

msg="Orchestrate state check: this turn changed things (edits, agents or git/gh writes) without updating the state file.
Update $STATE_FILE with a small Edit - Now/Next and anything decided - and set the marker's updated=$(now_iso).
Do not repeat your previous reply; end the turn with at most one line."

jq -n --rawfile ctx /dev/stdin \
   '{hookSpecificOutput:{hookEventName:"Stop",additionalContext:$ctx}}' \
   <<<"$msg" 2>/dev/null || exit 0
exit 0
