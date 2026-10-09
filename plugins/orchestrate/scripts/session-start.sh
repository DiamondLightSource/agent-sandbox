#!/usr/bin/env bash
# SessionStart hook - fires on startup, resume, clear and compact.
#
# Only in orchestrate mode (the pointer names a goal and this session runs
# in its launch directory): re-seeds a cold context with a small block - the
# goal's paths, a crash check against the previous transcript, and the state
# file's HEAD only. The body is read on demand.
#
# Outside the mode: silent.
#
# Never blocks a session: every unexpected condition exits 0 in silence.

set -uo pipefail
# shellcheck source=lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

hook_active || exit 0

state_rotate_log "$GOAL_LOG"
scratch_mb="$(state_scratch_report)"
total="$(wc -l < "$STATE_FILE")"

# ---- crash check ---------------------------------------------------------
# SessionEnd does not run on a crash or container restart, so the previous
# transcript is found here rather than trusted to a pointer written at exit.
prev="$(state_prev_transcript)"
crash=""
if [ -n "$prev" ]; then
    prev_m="$(stat -c %Y "$prev" 2>/dev/null || echo 0)"
    state_m="$(stat -c %Y "$STATE_FILE" 2>/dev/null || echo 0)"
    if [ "$prev_m" -gt "$((state_m + 120))" ]; then
        crash="Previous transcript $prev was last written $(date -u -d "@$prev_m" +%Y-%m-%dT%H:%M:%SZ),
AFTER the state file ($(date -u -d "@$state_m" +%H:%M:%SZ)), so it may hold work the file lacks
(a crash, or a turn that ended unflushed). Reconcile before any work and say what you found:
ask a haiku agent a specific question of: bash $TRANSCRIPT_TEXT <transcript> --since <ISO>.
Never /resume it."
    fi
fi

# ---- lost-agent check ----------------------------------------------------
# Now entries are `- [<model>] <item> -> <label> -> <brief> -> <report>`; one
# whose report file does not exist is still running or lost.
lost=""
while IFS= read -r line; do
    case "$line" in "- ["*" -> "*" -> "*" -> "*) ;; *) continue ;; esac
    report="${line##* -> }"; rest="${line% -> *}"
    brief="${rest##* -> }"; rest="${rest% -> *}"
    label="${rest##* -> }"
    report="${report/#\~/$HOME}"; brief="${brief/#\~/$HOME}"
    # A relative path is relative to the goal folder (reports/<label>.md).
    case "$report" in /*) ;; *) report="$GOAL_DIR/$report" ;; esac
    [ -n "$report" ] && [ ! -e "$report" ] || continue
    lost+="Now entry $label has no report yet: still running or lost - check before relaunching from $brief."$'\n'
done < <(state_head "$STATE_FILE" 2>/dev/null | sed -n '/^## Now/,/^## /p')

# ---- assemble ------------------------------------------------------------
{
    printf '## Orchestrate: %s\n' "$SLUG"
    printf '(injected by the SessionStart hook; source=%s)\n\n' "${HOOK_SOURCE:-unknown}"
    printf 'Goal folder: %s\n' "$GOAL_DIR"
    printf 'State file:  %s (%s lines; the head is below, the body is not loaded)\n' "$STATE_FILE" "$total"
    printf 'Status file: %s\n' "$(status_path "$LAUNCH_DIR")"
    printf 'Orchestrate mode is active: load the /orchestrate skill with "resume" before anything else.\n'
    [ -n "$crash" ] && printf '\n%s\n' "$crash"
    [ -n "$lost" ] && printf '\n%s' "$lost"
    [ -n "$scratch_mb" ] && printf '\nScratch in %s/*/scratch totals %s MB (report threshold %s MB). Offer the user a cleanup: list each scratch dir with its size and whether its goal is stopped; delete only what they name.\n' "$ROOT" "$scratch_mb" "$SCRATCH_REPORT_MB"
    # On resume the transcript already carries the head; only re-inject it
    # into a context that has lost it.
    if [ "${HOOK_SOURCE:-}" != "resume" ]; then
        printf '\n--- head of %s ---\n' "$STATE_FILE"
        state_head "$STATE_FILE"
        printf -- '--- end head ---\n'
    fi
} | jq -n --rawfile ctx /dev/stdin --arg t "orchestrate: $SLUG" \
       '{hookSpecificOutput:{hookEventName:"SessionStart",additionalContext:$ctx,sessionTitle:$t}}' \
       2>/dev/null
exit 0
