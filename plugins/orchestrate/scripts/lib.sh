#!/usr/bin/env bash
# Shared helpers for the orchestrate plugin's scripts and hooks.
#
# A goal is one folder, wherever its work happens (a goal may span several
# repositories, so nothing here is keyed by git):
#
#     ~/.claude/orchestrate/<slug>/state.md     head (injected) + body (read on demand)
#     ~/.claude/orchestrate/<slug>/log.md       append-only history, never injected
#     ~/.claude/orchestrate/<slug>/briefs/ reports/ maps/ scratch/
#     ~/.claude/orchestrate/<slug>/archive/     this goal's rotated logs
#     ~/.claude/orchestrate/archive/<slug>/     a stopped goal (all but its scratch)
#
# state.md's first line is a machine-readable marker:
#
#     <!-- state: updated=<ISO UTC> status=active -->
#
# One goal is active at a time. The pointer file ~/.claude/orchestrate/active
# names it, the directory the session was launched in, and the Claude
# session that owns it:
#
#     slug=<slug>
#     dir=<absolute launch directory>
#     session=<session_id>             absent until the owner is known
#
# The hooks act only while the pointer exists, the session's cwd is that
# directory (or below it: Claude Code's cwd follows a `cd` inside the
# project) and the hook's session_id is the owner. Without the pointer
# every hook exits before reading anything, so installing the plugin costs
# a session that never enters the mode one cheap process per hook event
# and no tokens.
#
# Ownership. goal.sh runs in the model's Bash tool and cannot see the
# session id, so it writes the pointer without one. The Stop hook of the
# turn that ran `goal.sh start` or `goal.sh resume` (as a command, not a
# mention) claims the goal for its session (hook input carries session_id)
# and gives it the checks of goal_context. Another session in the same
# directory is silent unless it runs one of those itself, which is an
# explicit takeover. The claim needs that tool call in the transcript when
# Stop runs. /clear starts a new session id: the owner's SessionEnd
# (reason=clear) leaves session=cleared:<old id>, and the SessionStart
# (source=clear) that follows claims it. /compact and --resume keep the id.

ROOT="${HOME:?}/.claude/orchestrate"
POINTER="$ROOT/active"
PLUGIN_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TEMPLATE="$PLUGIN_ROOT/skills/orchestrate/references/state-file.md"
TRANSCRIPT_TEXT="$PLUGIN_ROOT/scripts/transcript-text.sh"
STATE_HEAD_CAP_LINES=40
STATE_HEAD_CAP_BYTES=4000
HEAD_END='<!-- end head -->'

# Growth limits. Nothing is ever deleted by a script: logs rotate into the
# goal's archive/, and scratch is only reported. The environment may
# override them (the tests do).
LOG_ROTATE_BYTES="${STATE_LOG_ROTATE_BYTES:-65536}"    # rotate a log past this
SCRATCH_REPORT_MB="${STATE_SCRATCH_REPORT_MB:-1024}"   # report scratch past this

now_iso() { date -u +%Y-%m-%dT%H:%M:%SZ; }

valid_slug() { [[ "$1" =~ ^[a-z0-9][a-z0-9-]{0,62}$ ]] && [ "$1" != archive ] && [ "$1" != active ]; }

# Set GOAL_DIR and the paths inside it for a slug.
goal_paths() {
    SLUG="$1"
    GOAL_DIR="$ROOT/$SLUG"
    STATE_FILE="$GOAL_DIR/state.md"
    GOAL_LOG="$GOAL_DIR/log.md"
}

# Read the pointer: set ACTIVE_SLUG, LAUNCH_DIR and OWNER (empty while
# unclaimed). Fails when there is none.
pointer_read() {
    ACTIVE_SLUG=""
    LAUNCH_DIR=""
    OWNER=""
    [ -f "$POINTER" ] || return 1
    ACTIVE_SLUG="$(sed -n 's/^slug=//p' "$POINTER" | head -1)"
    LAUNCH_DIR="$(sed -n 's/^dir=//p' "$POINTER" | head -1)"
    OWNER="$(sed -n 's/^session=//p' "$POINTER" | head -1)"
    [ -n "$ACTIVE_SLUG" ] && [ -n "$LAUNCH_DIR" ]
}

pointer_write() { # SLUG DIR [SESSION]
    mkdir -p "$ROOT" || return 1
    {
        printf 'slug=%s\ndir=%s\n' "$1" "$2"
        [ -z "${3:-}" ] || printf 'session=%s\n' "$3"
    } > "$POINTER.tmp.$$" && mv -f "$POINTER.tmp.$$" "$POINTER"
}

# Record a new owner in the pointer read last (see Ownership above).
pointer_set_owner() { # SESSION
    pointer_write "$ACTIVE_SLUG" "$LAUNCH_DIR" "$1" && OWNER="$1"
}

# The status file the user follows, in the launch directory.
status_path() { printf '%s/.claude/status.md\n' "$1"; }

state_read_hook_input() {
    local raw
    raw="$(cat)"
    # One jq call: each field on its own line, newlines inside values escaped.
    {
        IFS= read -r HOOK_SESSION_ID
        IFS= read -r HOOK_TRANSCRIPT
        IFS= read -r HOOK_CWD
        IFS= read -r HOOK_SOURCE
        IFS= read -r HOOK_REASON
        IFS= read -r HOOK_STOP_ACTIVE
    } < <(jq -r '.session_id // "", .transcript_path // "", .cwd // "",
                 .source // "", .reason // "", (.stop_hook_active // false)
                 | tostring | gsub("\n"; " ")' <<<"$raw" 2>/dev/null)
}

# For a hook: succeed when a goal is active here - the pointer exists, its
# goal has a state file, and the session's cwd is the launch directory or
# below it. Says nothing about which session owns it (hook_owner). Sets the
# goal paths. Drains stdin first.
hook_in_scope() {
    if [ ! -f "$POINTER" ]; then cat >/dev/null; return 1; fi
    state_read_hook_input
    pointer_read || return 1
    local cwd
    cwd="$(cd "${HOOK_CWD:-$PWD}" 2>/dev/null && pwd -P)" || return 1
    case "$cwd/" in "$LAUNCH_DIR"/*) ;; *) return 1 ;; esac
    goal_paths "$ACTIVE_SLUG"
    [ -f "$STATE_FILE" ]
}

# This hook runs in the session that owns the goal.
hook_owner() { [ -n "${HOOK_SESSION_ID:-}" ] && [ "$OWNER" = "$HOOK_SESSION_ID" ]; }

# For a hook: the mode is on for this session - in scope and its owner.
hook_active() { hook_in_scope && hook_owner; }

# Value of key=... in a state file's marker line.
state_field() {
    head -n 3 "$1" 2>/dev/null | sed -n "s/.*<!-- *state:.*[[:space:]]$2=\([^[:space:]]*\).*/\1/p" | head -1
}

# Set key=value in a state file's marker line (adds the key if missing).
state_set_field() { # FILE KEY VALUE
    local f="$1" k="$2" v="$3"
    if head -n 1 "$f" | grep -q "[[:space:]]$k="; then
        sed -i "1s/\([[:space:]]$k=\)[^[:space:]]*/\1$v/" "$f"
    else
        sed -i "1s/ -->/ $k=$v -->/" "$f"
    fi
}

# The head of a state file: everything above the end-of-head line, capped.
# Enforced here so a head that grows degrades into a warning, not a tax.
state_head() {
    local f="$1" n
    n="$(grep -n -F "$HEAD_END" "$f" 2>/dev/null | head -1 | cut -d: -f1)"
    if [ -z "$n" ]; then
        head -n "$STATE_HEAD_CAP_LINES" "$f" | head -c "$STATE_HEAD_CAP_BYTES"
        printf '\n[no "%s" line - showing the first %s lines only]\n' "$HEAD_END" "$STATE_HEAD_CAP_LINES"
        return
    fi
    head -n "$((n - 1))" "$f" | head -n "$STATE_HEAD_CAP_LINES" | head -c "$STATE_HEAD_CAP_BYTES"
    if [ "$((n - 1))" -gt "$STATE_HEAD_CAP_LINES" ]; then
        printf '\n[head truncated at %s lines - move detail below the end-of-head line]\n' "$STATE_HEAD_CAP_LINES"
    fi
}

# ---- the context block ---------------------------------------------------
# What a session that takes the goal over is told: the goal's paths, a
# crash check against the previous transcript, Now entries with no report,
# the scratch report and (unless $3 is no) the state file's head. Printed
# by SessionStart in the owner, and by Stop on the turn that claims the
# goal. Needs the goal paths and the hook input.
goal_context() { # WHO MODE_LINE [yes|no: include the head]
    local scratch_mb total prev crash="" prev_m state_m lost="" line report rest brief label
    scratch_mb="$(state_scratch_report)"
    total="$(wc -l < "$STATE_FILE")"

    # Crash check. SessionEnd does not run on a crash or container restart,
    # so the previous transcript is found here rather than trusted to a
    # pointer written at exit.
    prev="$(state_prev_transcript)"
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

    # Lost-agent check. Now entries are
    # `- [<model>] <item> -> <label> -> <brief> -> <report>`; one whose
    # report file does not exist is still running or lost.
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

    printf '## Orchestrate: %s\n' "$SLUG"
    printf '(injected by %s)\n\n' "$1"
    printf 'Goal folder: %s\n' "$GOAL_DIR"
    if [ "${3:-yes}" = yes ]; then
        printf 'State file:  %s (%s lines; the head is below, the body is not loaded)\n' "$STATE_FILE" "$total"
    else
        printf 'State file:  %s (%s lines; not loaded)\n' "$STATE_FILE" "$total"
    fi
    printf 'Status file: %s\n' "$(status_path "$LAUNCH_DIR")"
    printf '%s\n' "$2"
    [ -n "$crash" ] && printf '\n%s\n' "$crash"
    [ -n "$lost" ] && printf '\n%s' "$lost"
    [ -n "$scratch_mb" ] && printf '\nScratch in %s/*/scratch totals %s MB (report threshold %s MB). Offer the user a cleanup: list each scratch dir with its size and whether its goal is stopped; delete only what they name.\n' "$ROOT" "$scratch_mb" "$SCRATCH_REPORT_MB"
    if [ "${3:-yes}" = yes ]; then
        printf '\n--- head of %s ---\n' "$STATE_FILE"
        state_head "$STATE_FILE"
        printf -- '--- end head ---\n'
    fi
    return 0
}

# ---- log -----------------------------------------------------------------
# Writer of log entries for the hooks and goal.sh (agents append to log.md
# directly, in the same format).
# Outliner-friendly: one entry is one top-level "- " bullet that starts with
# an ISO UTC timestamp; further lines of the text become child bullets.
log_append() {
    local log="$1" text="$2" first=1 line
    {
        while IFS= read -r line || [ -n "$line" ]; do
            [ -n "$line" ] || continue
            if [ "$first" = 1 ]; then
                printf -- '- %s %s\n' "$(now_iso)" "$line"
                first=0
            else
                printf -- '  - %s\n' "${line#- }"
            fi
        done <<<"$text"
    } >> "$log"
}

# ---- growth --------------------------------------------------------------
# Rotate a goal's log past LOG_ROTATE_BYTES: the full file moves to
# <goal>/archive/log.<UTC stamp>.md and a fresh log starts with a pointer to
# it, so a reader following the log finds every older entry.
state_rotate_log() {
    local log="$1" size stamp dest
    [ -f "$log" ] || return 0
    size="$(stat -c %s "$log" 2>/dev/null || echo 0)"
    [ "$size" -gt "$LOG_ROTATE_BYTES" ] || return 0
    stamp="$(date -u +%Y%m%dT%H%M%SZ)"
    mkdir -p "$(dirname "$log")/archive" || return 0
    dest="$(dirname "$log")/archive/log.$stamp.md"
    mv -n "$log" "$dest" || return 0
    log_append "$log" "rotated: earlier entries are in archive/$(basename "$dest")"
}

# Total size of every goal's scratch/ (stopped goals keep theirs), in MB,
# when past SCRATCH_REPORT_MB; empty otherwise.
state_scratch_report() {
    local mb
    compgen -G "$ROOT/*/scratch" >/dev/null || return 0
    mb="$(du -smc "$ROOT"/*/scratch 2>/dev/null | tail -1 | cut -f1)"
    [ "${mb:-0}" -gt "$SCRATCH_REPORT_MB" ] && printf '%s' "$mb"
    return 0
}

# Newest transcript of this project other than the current one.
state_prev_transcript() {
    [ -n "${HOOK_TRANSCRIPT:-}" ] || return 0
    ls -t "$(dirname "$HOOK_TRANSCRIPT")"/*.jsonl 2>/dev/null | grep -v -F "$HOOK_TRANSCRIPT" | head -1
}
