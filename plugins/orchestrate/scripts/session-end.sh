#!/usr/bin/env bash
# SessionEnd hook - fires on clear, logout, prompt_input_exit and other.
#
# Only in orchestrate mode: one entry in the goal's log naming the transcript
# that just ended, so a later question about it knows where to look. The
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
exit 0
