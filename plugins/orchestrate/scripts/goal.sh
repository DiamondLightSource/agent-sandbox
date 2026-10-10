#!/usr/bin/env bash
# Goal lifecycle for orchestrate mode - the mechanics behind the skill's
# verbs, so the folder layout and the pointer are written one way only.
#
#     goal.sh start <slug> <goal sentence>   create the goal folder, write the pointer
#     goal.sh resume [<slug>]                 no slug: show the pointer, or list goals
#                                             (exit 3: ask the user which); with a
#                                             slug: point at that goal from here
#     goal.sh close                           mark the goal closed, archive it (not its
#                                             scratch), remove the pointer
#     goal.sh pause                           mode off, goal kept: status=paused,
#                                             pointer removed
#     goal.sh list [--all]                    every goal with its status (* = active);
#                                             --all lists stopped goals too
#     goal.sh touch                           set the state marker's updated= to now
#     goal.sh now '<entry>'                   a launch: add the entry to Now, in place
#                                             of "(nothing running)"
#     goal.sh land <label> ['<done line>']    a return: drop the Now entry "-> <label> ->",
#                                             "(nothing running)" when Now empties; with
#                                             a done line (the item is finished) append
#                                             "- YYYY-MM-DD HH:MMZ <done line>" to the end of Done;
#                                             label - when no agent ran
#
# touch, now and land act on the active goal's state file and set updated=.
# The launch directory is the current directory.
# start and resume pause the goal that is active, if it is another one.
# Exit 0 done, 1 refused (the message says why), 2 usage, 3 the user must
# choose.

set -uo pipefail
# shellcheck source=lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

usage() { sed -n '5,28p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//' >&2; exit 2; }
die() { printf 'goal.sh: %s\n' "$*" >&2; exit 1; }

dir="$(pwd -P)"
cmd="${1:-}"; [ $# -gt 0 ] && shift

goal_line() { grep -m1 '^# Goal:' "$1" 2>/dev/null | sed 's/^# Goal: *//' | cut -c1-80; }

# Keep the status file out of commits: add it to .git/info/exclude when the
# launch directory is in a git work tree and nothing ignores it yet.
status_exclude() {
    local d="$1" top rel ex
    top="$(git -C "$d" rev-parse --show-toplevel 2>/dev/null)" || return 0
    git -C "$d" check-ignore -q --no-index .claude/status.md 2>/dev/null && return 0
    rel="${d#"$(cd "$top" && pwd -P)"}"
    ex="$(git -C "$d" rev-parse --path-format=absolute --git-path info/exclude 2>/dev/null)" || return 0
    mkdir -p "$(dirname "$ex")" && printf '%s/.claude/status.md\n' "$rel" >> "$ex"
}

print_paths() {
    printf 'goal:     %s\n' "$SLUG"
    printf 'folder:   %s\n' "$GOAL_DIR"
    printf 'state:    %s\n' "$STATE_FILE"
    printf 'status:   %s\n' "$(status_path "$1")"
    printf 'launched: %s\n' "$1"
}

list_goals() { # [--all]
    local f n=0 slug mark
    pointer_read
    for f in "$ROOT"/*/state.md; do
        [ -f "$f" ] || continue
        n=$((n + 1))
        slug="$(basename "$(dirname "$f")")"
        mark=' '; [ "$slug" = "$ACTIVE_SLUG" ] && mark='*'
        printf '%s %-28s %-7s updated %s  %s\n' "$mark" "$slug" \
            "$(state_field "$f" status)" "$(state_field "$f" updated)" "$(goal_line "$f")"
    done
    if [ "${1:-}" = "--all" ]; then
        for f in "$ROOT"/archive/*/state.md; do
            [ -f "$f" ] || continue
            printf '  %-28s %-7s updated %s  %s\n' "archive/$(basename "$(dirname "$f")")" \
                "$(state_field "$f" status)" "$(state_field "$f" updated)" "$(goal_line "$f")"
        done
    elif [ -d "$ROOT/archive" ]; then
        printf '  (%s stopped goals in %s/archive/; goal.sh list --all shows them)\n' \
            "$(find "$ROOT/archive" -mindepth 1 -maxdepth 1 -type d | wc -l)" "$ROOT"
    fi
    [ "$n" -gt 0 ]
}

# Pause the active goal when it is not $1: mark it paused, log it, drop the
# pointer (the caller writes the new one). Run only after every check that
# can refuse, so a refused start or resume pauses nothing.
pause_other_active() {
    pointer_read || return 0
    [ "$ACTIVE_SLUG" != "${1:-}" ] || return 0
    pause_goal
}

# Pause the goal the pointer names.
pause_goal() {
    local f="$ROOT/$ACTIVE_SLUG/state.md"
    if [ -f "$f" ] && [ "$(state_field "$f" status)" != closed ]; then
        state_set_field "$f" status paused
        state_set_field "$f" updated "$(now_iso)"
        log_append "$ROOT/$ACTIVE_SLUG/log.md" "goal:$ACTIVE_SLUG paused"
    fi
    rm -f "$POINTER"
    printf 'paused:   %s\n' "$ACTIVE_SLUG"
}

# Rewrite the active goal's Now section, and append to Done, in one pass.
# From the environment: NOW_ADD an entry to add, NOW_DROP a label whose
# entries go, DONE_ADD a line for the end of Done. Now keeps its entries in
# order with no blank line among them, holds "- (nothing running)" when
# empty, and ends with one blank line; Done gains its line after its last
# entry. Then updated= is set and the new Now printed.
state_edit() {
    pointer_read || die "no active goal"
    goal_paths "$ACTIVE_SLUG"
    [ -f "$STATE_FILE" ] || die "goal '$SLUG' has no state file"
    grep -qx '## Now' "$STATE_FILE" || die "$STATE_FILE has no '## Now' line"
    # The copy keeps the file's mode; awk then replaces its content.
    cp -p "$STATE_FILE" "$STATE_FILE.tmp" || die "cannot write $STATE_FILE.tmp"
    awk -v end="$HEAD_END" '
        function heading(l) { return l ~ /^## / || l == end }
        # An entry names a label as "-> <label> ->" or by starting with it.
        function named(l, lab,   p, c) {
            if (index(l, " -> " lab " -> ")) return 1
            p = "- " lab; c = substr(l, length(p) + 1, 1)
            return substr(l, 1, length(p)) == p && (c == "" || c == " " || c == ":")
        }
        function close_now(   i) {
            if (ENVIRON["NOW_ADD"] != "") e[++n] = ENVIRON["NOW_ADD"]
            if (n == 0) e[++n] = "- (nothing running)"
            for (i = 1; i <= n; i++) print e[i]
            print ""; innow = 0
        }
        function close_done() {
            if (ENVIRON["DONE_ADD"] != "") print ENVIRON["DONE_ADD"]
            printf "%s", blanks; blanks = ""; indone = 0; done = 1
        }
        innow && heading($0) { close_now() }
        indone && heading($0) { close_done() }
        innow {
            if ($0 ~ /^[[:space:]]*$/ || $0 == "- (nothing running)") next
            if (ENVIRON["NOW_DROP"] != "" && named($0, ENVIRON["NOW_DROP"])) next
            e[++n] = $0; next
        }
        indone && /^[[:space:]]*$/ { blanks = blanks $0 "\n"; next }
        indone { printf "%s", blanks; blanks = ""; print; next }
        { print }
        $0 == "## Now" { innow = 1 }
        $0 == "## Done" || index($0, "## Done ") == 1 { indone = 1 }
        END {
            if (innow) close_now()
            if (indone) close_done()
            else if (!done && ENVIRON["DONE_ADD"] != "") { print "## Done"; print ENVIRON["DONE_ADD"] }
        }' "$STATE_FILE" > "$STATE_FILE.tmp" && mv -f "$STATE_FILE.tmp" "$STATE_FILE" \
        || { rm -f "$STATE_FILE.tmp"; die "cannot rewrite $STATE_FILE"; }
    state_set_field "$STATE_FILE" updated "$(now_iso)"
    sed -n '/^## Now$/,/^$/p' "$STATE_FILE"
}

# Entries in the active goal's Now, not counting the placeholder.
now_count() {
    pointer_read && goal_paths "$ACTIVE_SLUG" || return 0
    sed -n '/^## Now$/,/^## /p' "$STATE_FILE" 2>/dev/null | grep '^- ' | grep -vcx -- '- (nothing running)'
}

case "$cmd" in
touch)
    pointer_read || die "no active goal"
    goal_paths "$ACTIVE_SLUG"
    [ -f "$STATE_FILE" ] || die "goal '$SLUG' has no state file"
    state_set_field "$STATE_FILE" updated "$(now_iso)"
    head -n 1 "$STATE_FILE"
    ;;
now)
    entry="$*"; entry="${entry//$'\n'/ }"; entry="${entry#- }"
    [ -n "$entry" ] || usage
    NOW_ADD="- $entry" state_edit
    ;;
land)
    label="${1:-}"; shift || true
    [ -n "$label" ] || usage
    done_line="$*"; done_line="${done_line//$'\n'/ }"
    [ -n "$done_line" ] && done_line="- $(date -u "+%Y-%m-%d %H:%MZ") ${done_line#- }"
    n="$(now_count)"
    NOW_DROP="$label" DONE_ADD="$done_line" state_edit
    [ "$label" = - ] || [ "$(now_count)" != "$n" ] \
        || printf 'note:     no Now entry names %s; Now is unchanged\n' "$label"
    ;;
start)
    slug="${1:-}"; shift || true
    goal="$*"
    [ -n "$slug" ] && [ -n "$goal" ] || usage
    valid_slug "$slug" || die "bad slug '$slug' (lower-case letters, digits, '-')"
    goal_paths "$slug"
    [ -e "$STATE_FILE" ] && die "goal '$slug' exists; use: goal.sh resume $slug"
    [ -e "$ROOT/archive/$slug" ] && die "a stopped goal is archived as '$slug'; pick another slug"
    pause_other_active "$slug"
    mkdir -p "$GOAL_DIR"/{briefs,reports,maps,scratch} || die "cannot create $GOAL_DIR"
    awk '/^```markdown$/ {on=1; next} on && /^```$/ {exit} on' "$TEMPLATE" \
        | sed -e "1s|.*|<!-- state: updated=$(now_iso) status=active -->|" \
              -e "s|^# Goal:.*|# Goal: ${goal//|/\\|}|" > "$STATE_FILE"
    log_append "$GOAL_LOG" "goal:$slug started in $dir: $goal"
    pointer_write "$slug" "$dir" || die "cannot write $POINTER"
    status_exclude "$dir"
    print_paths "$dir"
    ;;
resume)
    slug="${1:-}"
    if [ -z "$slug" ]; then
        if pointer_read; then
            goal_paths "$ACTIVE_SLUG"
            print_paths "$LAUNCH_DIR"
            [ "$LAUNCH_DIR" = "$dir" ] || printf 'note:     this session is in %s, not the launch directory; the hooks are off here. To move the goal here: goal.sh resume %s\n' "$dir" "$ACTIVE_SLUG"
            exit 0
        fi
        printf 'No active goal. Goals in %s:\n' "$ROOT"
        list_goals || printf '  (none)\n'
        exit 3
    fi
    goal_paths "$slug"
    [ -f "$STATE_FILE" ] || die "no goal '$slug' (goal.sh list)"
    [ "$(state_field "$STATE_FILE" status)" = "closed" ] && die "goal '$slug' is closed"
    # Resuming the goal that is already active leaves state.md alone, so a
    # new session's crash check still compares against the last real write.
    pointer_read && [ "$ACTIVE_SLUG" = "$slug" ] && was_active=1 || was_active=0
    pause_other_active "$slug"
    if [ "$was_active" = 0 ]; then
        state_set_field "$STATE_FILE" status active
        state_set_field "$STATE_FILE" updated "$(now_iso)"
    fi
    pointer_write "$slug" "$dir" || die "cannot write $POINTER"
    status_exclude "$dir"
    log_append "$GOAL_LOG" "goal:$slug resumed in $dir"
    print_paths "$dir"
    ;;
close)
    pointer_read || die "no active goal"
    goal_paths "$ACTIVE_SLUG"
    [ -f "$STATE_FILE" ] || die "goal '$SLUG' has no state file"
    state_set_field "$STATE_FILE" status closed
    state_set_field "$STATE_FILE" updated "$(now_iso)"
    log_append "$GOAL_LOG" "goal:$SLUG stopped"
    dest="$ROOT/archive/$SLUG"
    [ -e "$dest" ] && dest="$dest.$(date -u +%Y%m%dT%H%M%SZ)"
    mkdir -p "$dest" || die "cannot create $dest"
    # Scratch stays: it can hold git worktrees, whose registration in their
    # parent repo names the absolute path, so a move would break them.
    for x in "$GOAL_DIR"/* "$GOAL_DIR"/.[!.]*; do
        [ -e "$x" ] || continue
        [ "$x" = "$GOAL_DIR/scratch" ] && continue
        mv -n "$x" "$dest/"
    done
    rm -f "$POINTER"
    rmdir "$GOAL_DIR/scratch" 2>/dev/null
    rmdir "$GOAL_DIR" 2>/dev/null
    printf 'closed:   %s\narchived: %s\n' "$SLUG" "$dest"
    [ -d "$GOAL_DIR/scratch" ] && printf 'kept:     %s (%s)\n' "$GOAL_DIR/scratch" "$(du -sh "$GOAL_DIR/scratch" | cut -f1)"
    printf 'pointer removed: the hooks are off.\n'
    ;;
list)
    list_goals "${1:-}" || printf '  (no goals in %s)\n' "$ROOT"
    pointer_read && printf 'active: %s (launched in %s)\n' "$ACTIVE_SLUG" "$LAUNCH_DIR"
    exit 0
    ;;
pause)
    pointer_read || die "no active goal"
    pause_goal
    printf 'the hooks are off; goal.sh resume %s brings it back.\n' "$ACTIVE_SLUG"
    ;;
*) usage ;;
esac
