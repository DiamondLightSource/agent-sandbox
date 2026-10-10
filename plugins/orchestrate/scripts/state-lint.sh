#!/usr/bin/env bash
# Lint a goal's state file for the drift that edits made from memory leave
# behind. Run by the Stop and SessionStart hooks; also usable by hand.
#
#     state-lint.sh <state.md> [<log.md>] [<sidecar>]
#
# Prints one line per finding, "lint: <code>: <text>", and nothing when the
# file is clean. Always exits 0: a hook must never block. Reads only the
# files given. <log.md> lets done-order place Done entries that carry no
# YYYY-MM-DD HH:MMZ stamp. <sidecar> (the goal's .lint) keeps each pending section's
# hash and the Done count when it last changed, for section-stale; it is the
# only file written, so the orchestrator stays the state file's one writer.
#
# The format checked is the one SKILL.md describes and goal.sh now/land
# write:
#   ## Now    `- (nothing running)` or one entry per line, no blank lines
#   ## Done   the last section; one line per finished item, `- YYYY-MM-DD HH:MMZ <text>`,
#             appended in time order
#   ## Decided  rulings only, each ending `(<who>, YYYY-MM-DD)`
# Thresholds may come from the environment (the tests set them).

set -uo pipefail
# shellcheck source=lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

f="${1:?usage: state-lint.sh <state.md> [<log.md>] [<sidecar>]}"; log="${2:-}"; side="${3:-}"
[ -f "$f" ] || exit 0
goal_dir="$(cd "$(dirname "$f")" && pwd)"
MAX_BYTES="${STATE_LINT_MAX_BYTES:-10240}"      # the whole file, read on demand
MAX_DONE="${STATE_LINT_MAX_DONE:-20}"           # Done entries: one per item
STALE_DONE="${STATE_LINT_STALE_DONE:-5}"        # Done growth a pending section may ignore
MARKER_SLACK="${STATE_LINT_MARKER_SLACK:-900}"  # seconds updated= may trail the file

out() { printf 'lint: %s\n' "$*"; }

# The lines of "## NAME" (a heading may carry a suffix) up to the next
# heading or the end-of-head line.
section() {
    awk -v s="## $1" -v end="$HEAD_END" '
        $0 == s || index($0, s " ") == 1 { on = 1; next }
        on && (/^## / || $0 == end) { exit }
        on' "$f"
}

# ---- Now ---------------------------------------------------------------------
now="$(section Now)"
# One blank line before the next heading is the separator; any other is drift.
blank="$(section Now | awk '/^[[:space:]]*$/ { n++ } END { print (n > 1 ? n - 1 : 0) }')"
[ "$blank" -gt 0 ] && out "now-blank: $blank blank line(s) inside ## Now; one entry per line, none blank"
grep -q '^- ' <<<"$now" || out "now-empty: ## Now has no entry; when nothing runs it holds '- (nothing running)'"
while IFS= read -r line; do
    case "$line" in "- ["*" -> "*" -> "*" -> "*) ;; *) continue ;; esac
    report="${line##* -> }"; report="${report%% (*}"; report="${report/#\~/$HOME}"
    case "$report" in /*) ;; *) report="$goal_dir/$report" ;; esac
    rest="${line% -> *}"; rest="${rest% -> *}"; label="${rest##* -> }"
    [ -e "$report" ] && out "now-finished: $label has its report; it has returned: goal.sh land $label"
done <<<"$now"

# ---- finished numbers still pending ------------------------------------------
# A #N is finished when a Done or Decided line says it merged or closed. Next,
# Awaiting user or Queue still naming one is worth a look (a follow-up may
# legitimately name it; the Stop hook reports a finding once).
finished="$( { section Done; section Decided; } \
    | grep -oiE '#[0-9]+[^#]{0,25}(merged|closed)|(merged|closed)[^#]{0,12}#[0-9]+' \
    | grep -oE '#[0-9]+' | sort -u)"
for s in Next "Awaiting user" Queue; do
    for n in $(section "$s" | grep -oE '#[0-9]+' | sort -u); do
        grep -qxF -- "$n" <<<"$finished" || continue
        out "pending-but-done: ## $s names $n, which Done or Decided records as merged or closed"
    done
done

# ---- Decided: rulings only ---------------------------------------------------
dec="$(section Decided | grep '^- ')"
ev="$(grep -cE 'MERGED|CLOSED|APPROVE|CHANGES|^- Opened |- done [0-9]|\((opus|sonnet|haiku|fable)\)[: ]' <<<"$dec")"
[ "$ev" -gt 0 ] && out "decided-events: $ev ## Decided line(s) read as events (merges, reviews, agent returns); they belong in Done or the log"
nowho="$(grep -cvE '\([^()]*, *(20[0-9][0-9]|<YYYY-MM-DD>)[^()]*\)' <<<"$dec")"
[ -n "$dec" ] && [ "$nowho" -gt 0 ] && out "decided-unattributed: $nowho ## Decided line(s) lack '(<who>, YYYY-MM-DD)'; a ruling names who made it, anything else is not a ruling"

# ---- Done: appended in time order --------------------------------------------
# A stamped entry (- YYYY-MM-DD HH:MMZ) earlier than the one above it was
# inserted, not appended. An unstamped entry is placed by its first word,
# when the log timestamps that label.
inv="$(section Done | awk -v lf="$log" '
    BEGIN { if (lf != "") while ((getline l < lf) > 0)
                if (l ~ /^- [0-9]{4}-[0-9][0-9]-[0-9][0-9]T[0-9:]+Z? [a-z0-9-]+:/) {
                    split(l, a, " "); lab = a[3]; sub(/:$/, "", lab)
                    if (!(lab in ts)) ts[lab] = a[2] } }
    /^- [0-9]{4}-[0-9][0-9]-[0-9][0-9] [0-2][0-9]:[0-5][0-9]Z / {
        m = $2 " " $3
        if (pm != "" && m < pm) n++
        pm = m; next }
    /^- / { if ($2 in ts) { if (pl != "" && ts[$2] < pl) n++; pl = ts[$2] } }
    END { print n + 0 }')"
[ "$inv" -gt 0 ] && out "done-order: $inv ## Done entr(y/ies) sit above an older one; append each at the end of Done"

# ---- size ----------------------------------------------------------------------
bytes="$(wc -c < "$f")"
[ "$bytes" -gt "$MAX_BYTES" ] && out "size: $bytes bytes (> $MAX_BYTES); collapse each finished item to one Done line and move the detail to the log"
done_n="$(section Done | grep -c '^- ')"
[ "$done_n" -gt "$MAX_DONE" ] && out "done-long: $done_n ## Done entries (> $MAX_DONE); one line per finished item, not per agent return"
hn="$(grep -n -F -x "$HEAD_END" "$f" | head -1 | cut -d: -f1)"
if [ -n "$hn" ]; then
    hb="$(head -n "$((hn - 1))" "$f" | wc -c)"
    { [ "$((hn - 1))" -gt "$STATE_HEAD_CAP_LINES" ] || [ "$hb" -gt "$STATE_HEAD_CAP_BYTES" ]; } \
        && out "head-long: the head is $((hn - 1)) lines, $hb bytes (cap $STATE_HEAD_CAP_LINES lines, $STATE_HEAD_CAP_BYTES bytes); the injected copy is cut short"
fi

# ---- marker ------------------------------------------------------------------
upd="$(state_field "$f" updated)"
u="$(date -u -d "$upd" +%s 2>/dev/null || echo 0)"
if [ -n "$upd" ] && [ "$u" -gt 0 ]; then
    m="$(stat -c %Y "$f")"; t="$(date -u +%s)"
    if [ "$((m - u))" -gt "$MARKER_SLACK" ] || [ "$((u - t))" -gt 300 ]; then
        out "marker-stale: updated=$upd does not match the file's last change; run goal.sh touch, never type the time"
    fi
fi

# ---- pending sections left alone while work moved ------------------------------
if [ -n "$side" ]; then
    touch "$side" 2>/dev/null || exit 0
    for s in Next "Awaiting user" Queue Map; do
        # An empty section, or one holding only a placeholder such as
        # "- (none)", has nothing to go stale.
        section "$s" | grep '^- ' | grep -qv '^- ([^()]*)$' || continue
        h="$(section "$s" | md5sum | cut -c1-12)"; key="${s// /_}"
        prev="$(grep "^$key " "$side" | tail -1)"
        # A new hash, or a Done that shrank (items collapsed), resets the count.
        if [ -z "$prev" ] || [ "$(cut -d' ' -f2 <<<"$prev")" != "$h" ] \
            || [ "$(cut -d' ' -f3 <<<"$prev")" -gt "$done_n" ]; then
            { grep -v "^$key " "$side"; printf '%s %s %s\n' "$key" "$h" "$done_n"; } > "$side.tmp" \
                && mv -f "$side.tmp" "$side"
            continue
        fi
        since="$(cut -d' ' -f3 <<<"$prev")"
        [ "$((done_n - since))" -ge "$STALE_DONE" ] || continue
        out "section-stale: ## $s unchanged while Done grew by $((done_n - since)); re-read it and fix each line it falsifies"
        # Reported: count again from here, so a section confirmed as it is
        # is named again only after another $STALE_DONE items.
        { grep -v "^$key " "$side"; printf '%s %s %s\n' "$key" "$h" "$done_n"; } > "$side.tmp" \
            && mv -f "$side.tmp" "$side"
    done
fi
exit 0
