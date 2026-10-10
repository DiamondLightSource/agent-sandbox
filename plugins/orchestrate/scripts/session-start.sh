#!/usr/bin/env bash
# SessionStart hook - fires on startup, resume, clear and compact.
#
# Only in orchestrate mode (the pointer names a goal, this session runs in
# its launch directory and owns the goal): re-seeds a cold context with a
# small block - the goal's paths, a crash check against the previous
# transcript, the state file's lint findings, and its HEAD only. The body
# is read on demand.
#
# A /clear in the owning session starts a new session id; the owner's
# SessionEnd left session=cleared:<old id>, and this hook claims it when
# source=clear (lib.sh, Ownership).
#
# Outside the mode, and in any session that does not own the goal: silent.
#
# Never blocks a session: every unexpected condition exits 0 in silence.

set -uo pipefail
# shellcheck source=lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

hook_in_scope || exit 0
if ! hook_owner; then
    [ "${HOOK_SOURCE:-}" = clear ] && [ "${OWNER#cleared:}" != "$OWNER" ] \
        && [ -n "${HOOK_SESSION_ID:-}" ] || exit 0
    pointer_set_owner "$HOOK_SESSION_ID" || exit 0
fi

state_rotate_log "$GOAL_LOG"
# On resume the transcript already carries the head; only re-inject it
# into a context that has lost it.
head=yes; [ "${HOOK_SOURCE:-}" = resume ] && head=no
goal_context "the SessionStart hook; source=${HOOK_SOURCE:-unknown}" \
    'Orchestrate mode is active: load the /orchestrate skill with "resume" before anything else.' "$head" \
    | jq -n --rawfile ctx /dev/stdin --arg t "orchestrate: $SLUG" \
       '{hookSpecificOutput:{hookEventName:"SessionStart",additionalContext:$ctx,sessionTitle:$t}}' \
       2>/dev/null
exit 0
