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
# names it and the directory the session was launched in:
#
#     slug=<slug>
#     dir=<absolute launch directory>
#
# The hooks act only while the pointer exists and the session's cwd is that
# directory (or below it: Claude Code's cwd follows a `cd` inside the
# project). Without the pointer every hook exits before reading anything,
# so installing the plugin costs a session that never enters the mode one
# cheap process per hook event and no tokens.

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

# Read the pointer: set ACTIVE_SLUG and LAUNCH_DIR. Fails when there is none.
pointer_read() {
    ACTIVE_SLUG=""
    LAUNCH_DIR=""
    [ -f "$POINTER" ] || return 1
    ACTIVE_SLUG="$(sed -n 's/^slug=//p' "$POINTER" | head -1)"
    LAUNCH_DIR="$(sed -n 's/^dir=//p' "$POINTER" | head -1)"
    [ -n "$ACTIVE_SLUG" ] && [ -n "$LAUNCH_DIR" ]
}

pointer_write() { # SLUG DIR
    mkdir -p "$ROOT" || return 1
    printf 'slug=%s\ndir=%s\n' "$1" "$2" > "$POINTER.tmp" && mv -f "$POINTER.tmp" "$POINTER"
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

# For a hook: succeed only when the mode is on for this session - the
# pointer exists, its goal has a state file, and the session's cwd is the
# launch directory or below it. Sets the goal paths. Drains stdin first.
hook_active() {
    if [ ! -f "$POINTER" ]; then cat >/dev/null; return 1; fi
    state_read_hook_input
    pointer_read || return 1
    local cwd
    cwd="$(cd "${HOOK_CWD:-$PWD}" 2>/dev/null && pwd -P)" || return 1
    case "$cwd/" in "$LAUNCH_DIR"/*) ;; *) return 1 ;; esac
    goal_paths "$ACTIVE_SLUG"
    [ -f "$STATE_FILE" ]
}

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
