#!/usr/bin/env bash
# shellcheck shell=bash
# Shared test harness for the claude-sandbox bash test suite
# (smoke.sh, launcher.sh, doctor.sh, ...). Sourced — defines the PASS/FAIL
# counters, the assertion helpers, a jq predicate wrapper, and a
# register-once EXIT cleanup. Source-safe: defining functions and zeroing
# the counters is all that runs at source time. Owns no `set` options —
# the caller keeps its own `set -uo pipefail`.

PASSED=0
FAILED=0

# pass / fail "<msg>": tally a result. fail echoes a FAIL: line to stderr.
pass() { PASSED=$((PASSED + 1)); }
fail() {
    FAILED=$((FAILED + 1))
    echo "FAIL: $1" >&2
}

# assert_contains NAME ARGV TOKEN — ARGV (newline-joined) has a line == TOKEN.
assert_contains() {
    local name="$1" argv="$2" token="$3"
    if printf '%s\n' "$argv" | grep -qxF -- "$token"; then
        pass
    else
        fail "$name — missing token: $token"
        { echo "----- argv -----"; printf '%s\n' "$argv"; echo "----------------"; } >&2
    fi
}

# assert_not_contains NAME ARGV TOKEN — ARGV has NO line == TOKEN.
assert_not_contains() {
    local name="$1" argv="$2" token="$3"
    if printf '%s\n' "$argv" | grep -qxF -- "$token"; then
        fail "$name — unexpected token: $token"
    else
        pass
    fi
}

# assert_eq NAME EXPECTED ACTUAL — string equality.
assert_eq() {
    local name="$1" expected="$2" actual="$3"
    if [ "$expected" = "$actual" ]; then
        pass
    else
        fail "$name — expected '$expected', got '$actual'"
    fi
}

# assert_parse NAME CMD... — run CMD; pass iff it exits 0.
assert_parse() {
    local name="$1"; shift
    if "$@"; then pass; else fail "$name"; fi
}

# expect_file PATH [MSG] — pass iff PATH is a regular file.
expect_file() {
    local path="$1" msg="${2:-missing file $1}"
    if [ -f "$path" ]; then pass; else fail "$msg"; fi
}

# jq_check LABEL FILTER FILE — pass iff `jq -e FILTER FILE` succeeds. Collapses
# the repeated `if jq -e '…'; then pass; else fail '…'; fi` blocks.
jq_check() {
    local label="$1" filter="$2" file="$3"
    if jq -e "$filter" "$file" >/dev/null 2>&1; then pass; else fail "$label"; fi
}

# register_cleanup PATH... — accumulate paths to rm -rf on EXIT. Sets the EXIT
# trap ONCE, so callers register tmpdirs as they create them instead of
# rewriting an ever-growing `trap 'rm -rf …' EXIT` line (the old footgun).
_CLEANUP_PATHS=()
_cleanup_trap_set=0
_run_cleanup() {
    [ "${#_CLEANUP_PATHS[@]}" -gt 0 ] && rm -rf "${_CLEANUP_PATHS[@]}"
}
register_cleanup() {
    _CLEANUP_PATHS+=( "$@" )
    if [ "$_cleanup_trap_set" -eq 0 ]; then
        trap _run_cleanup EXIT
        _cleanup_trap_set=1
    fi
}

# finish NAME — print the summary line and yield a 0/1 exit status. Call as the
# script's last command so its status becomes the script's exit status.
finish() {
    echo "$1: $PASSED passed / $FAILED failed"
    [ "$FAILED" -eq 0 ]
}
