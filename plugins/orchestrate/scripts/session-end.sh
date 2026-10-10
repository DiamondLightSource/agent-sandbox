#!/usr/bin/env bash
# SessionEnd hook - fires on clear, logout, prompt_input_exit and other.
#
# Only in the session that owns the active goal: one entry in the goal's
# log naming the transcript that just ended, so a later question about it
# knows where to look. On /clear it also hands ownership to the session the
# clear starts: session=cleared:<id>, claimed by its SessionStart. The
# model is never invoked, so this costs zero tokens. It does not run on a
# crash or container restart; SessionStart finds the previous transcript
# itself.
#
# Never blocks: any unexpected condition exits 0 in silence.

set -uo pipefail
# shellcheck source=lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

hook_active || exit 0
state_rotate_log "$GOAL_LOG"
log_append "$GOAL_LOG" "session ended (${HOOK_REASON:-unknown}) goal:$SLUG
transcript: ${HOOK_TRANSCRIPT:-unknown}" 2>/dev/null
[ "${HOOK_REASON:-}" = clear ] && pointer_set_owner "cleared:$HOOK_SESSION_ID" 2>/dev/null
exit 0
