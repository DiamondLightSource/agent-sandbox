#!/usr/bin/env bash
# The shadow's PATH watcher, end to end (ADR 27):
# quarantine executables a session adds ahead of system commands on PATH,
# and new git hooks, while the session runs; warn in outer shells.
#
# Run it INSIDE this repository's image, started as
# .github/workflows/container.yml starts it. The image's PATH starts with the project venv's bin, under the
# conf's `allow-write = /cache`. A probe stands in for the real claude
# (restored on exit): from inside the jail it leaves a `git` in the venv's
# bin and a pre-push hook in the workspace's repository, and waits while
# this script checks, from outside, that each was neutralised within about a
# second. Once with the egress jail on (the watcher is a thread of the
# launcher), once with it off (a forked watcher). Then git config changes,
# between sessions and in one: alerts only. Run in a throwaway
# container.
set -euo pipefail

real=/usr/libexec/claude-sandbox/claude
venv_bin="$(readlink -f "$(dirname "$(command -v python)")")"
alerts=/run/claude-sandbox/alerts
work="$(mktemp -d /work/watch.XXXXXX)"
saved="$(mktemp -d)"
cleanup() {
    [ -e "$saved/real" ] && mv -f "$saved/real" "$real"
    rm -rf "$saved" "$work" "$venv_bin/git"
}
trap cleanup EXIT

fail() { echo "FAIL: $*" >&2; exit 1; }
pass() { echo "PASS: $*"; }
# wait_for SECONDS CONDITION...: poll every 50ms; the seconds it took.
wait_for() {
    local limit="$1" start now
    shift
    start="$(date +%s%N)"
    until "$@"; do
        now="$(date +%s%N)"
        [ $(( (now - start) / 1000000 )) -lt $(( limit * 1000 )) ] || return 1
        sleep 0.05
    done
    now="$(date +%s%N)"
    printf '%d ms\n' $(( (now - start) / 1000000 ))
}
not_executable() { [ -e "$1" ] && [ ! -x "$1" ]; }
exists() { [ -e "$1" ]; }

grep -q " -I -m claude_sandbox _shadow " /usr/local/bin/claude \
    || fail "the Python shadow is not installed"
[ -x /usr/bin/git ] || fail "no /usr/bin/git to shadow"
[ "$(command -v git)" = /usr/bin/git ] || fail "git is $(command -v git) before the test"

cd "$work"
git init -q .
mv "$real" "$saved/real"
cat > "$real" <<EOF
#!/bin/bash
# Inside the jail.
here="\$PWD"
until_file() { for _ in \$(seq 400); do [ -e "\$1" ] && return 0; sleep 0.05; done; return 1; }
ls -A /run/claude-sandbox > "\$here/state-seen" 2>&1 || true
printf '#!/bin/sh\necho not git\n' > "$venv_bin/git"
chmod 755 "$venv_bin/git"
touch "\$here/wrote-git"
until_file "\$here/go-hook"
printf '#!/bin/sh\necho hook\n' > "\$here/.git/hooks/pre-push"
chmod 755 "\$here/.git/hooks/pre-push"
touch "\$here/wrote-hook"
until_file "\$here/go-exit"
EOF
chmod 755 "$real"

session() {  # MODE: one launch, checked from outside while it runs
    local mode="$1" took pid
    rm -f wrote-git wrote-hook go-hook go-exit state-seen "$venv_bin/git" .git/hooks/pre-push
    { : > "$alerts"; } 2>/dev/null || true
    if [ "$mode" = jail-off ]; then
        CLAUDE_SANDBOX_EGRESS_JAIL=0 claude < /dev/null > session.out 2>&1 &
    else
        claude < /dev/null > session.out 2>&1 &
    fi
    pid=$!

    wait_for 30 exists wrote-git > /dev/null || fail "$mode: the probe never wrote git: $(cat session.out)"
    took="$(wait_for 2 not_executable "$venv_bin/git")" \
        || fail "$mode: $venv_bin/git is still executable"
    [ "$(bash -c 'command -v git')" = /usr/bin/git ] || fail "$mode: git is not /usr/bin/git outside"
    grep -qF "cleared the execute bits of $venv_bin/git (it shadowed /usr/bin/git)" "$alerts" \
        || fail "$mode: no alert: $(cat "$alerts")"
    pass "$mode: $venv_bin/git quarantined in $took; git is /usr/bin/git outside; alerted"

    # Without a controlling terminal, so it cannot take this one's foreground.
    prompt="$(setsid -w bash -i <<< 'exit' 2>&1 >/dev/null)"
    case "$prompt" in
        *"alerts about what a sandboxed session left:"*"$venv_bin/git"*) ;;
        *) fail "$mode: no warning at an interactive bash prompt: $prompt" ;;
    esac
    pass "$mode: an interactive bash warns at its prompt"

    touch go-hook
    wait_for 30 exists wrote-hook > /dev/null || fail "$mode: the probe never wrote the hook"
    took="$(wait_for 2 not_executable .git/hooks/pre-push)" \
        || fail "$mode: the pre-push hook is still executable"
    grep -qF "$work/.git/hooks/pre-push (a git hook)" "$alerts" \
        || fail "$mode: no hook alert: $(cat "$alerts")"
    pass "$mode: the new pre-push hook was quarantined in $took and alerted"

    touch go-exit
    wait "$pid" || fail "$mode: the session failed: $(cat session.out)"
    [ ! -s state-seen ] || fail "$mode: the session could see the watcher's state: $(cat state-seen)"
    if [ "$mode" = jail-on ]; then
        grep -q "alerts from this session" session.out \
            || fail "$mode: no summary at the session's end: $(cat session.out)"
        pass "$mode: the session could not see the alerts; its end printed a summary"
    else
        wait_for 3 sh -c '! pgrep -f "[_]shadow claude" > /dev/null' > /dev/null \
            || fail "$mode: the forked watcher outlived the session"
        pass "$mode: the session could not see the alerts; the watcher left with it"
    fi
}

session jail-on
session jail-off

# The Python CLI, until the installer puts it on PATH (issue #72 phase 4).
cli=(/usr/libexec/claude-sandbox/venv/bin/python -I -m claude_sandbox)
"${cli[@]}" alerts | grep -qF "$venv_bin/git" || fail "claude-sandbox alerts lists nothing"
report="$("${cli[@]}" doctor || true)"  # non-zero: it has things to report
grep -qE "^  warn +quarantined +[0-9]+ alert" <<< "$report" \
    || fail "doctor does not report the alerts: $report"
grep -qE "^  ok +entry points " <<< "$report" || fail "doctor: $report"
"${cli[@]}" alerts --clear
[ -z "$("${cli[@]}" alerts)" ] || fail "claude-sandbox alerts --clear left alerts"
pass "claude-sandbox alerts lists them and --clear empties the list; doctor reports them"

# Git config: core.hooksPath changed between sessions (as husky sets it), and
# core.fsmonitor added in one, alert and quarantine nothing; a remote, an
# upstream and user.name raise no alert.
mkdir .husky
printf '#!/bin/sh\nexit 0\n' > .husky/pre-commit
chmod 755 .husky/pre-commit
git config core.hooksPath .husky
cat > "$real" <<'EOF'
#!/bin/bash
# Inside the jail.
set -e
git init -q --bare bare.git
git remote add origin "$PWD/bare.git"
git config user.name "Someone else"
git -c user.email=probe@example.invalid commit -q --allow-empty -m probe
git push -q -u origin HEAD
git config core.fsmonitor /nonexistent/fsmonitor-hook
EOF
claude < /dev/null > session.out 2>&1 || fail "git config: the session failed: $(cat session.out)"
grep -qF "found at launch: between sessions, core.hooksPath of $work changed from (unset) to .husky" session.out \
    || fail "git config: no launch warning: $(cat session.out)"
expected="between sessions, core.hooksPath of $work changed from (unset) to .husky
core.fsmonitor in the git config of $work changed from (unset) to \"/nonexistent/fsmonitor-hook\""
[ "$(cut -d' ' -f3- "$alerts")" = "$expected" ] || fail "git config: the alerts are: $(cat "$alerts")"
[ -x .husky/pre-commit ] || fail "git config: .husky/pre-commit was quarantined"
[ "$(git -c core.fsmonitor=false config branch."$(git -c core.fsmonitor=false branch --show-current)".remote)" = origin ] \
    || fail "git config: the probe did not set an upstream"
pass "git config: a hooksPath change between sessions and core.fsmonitor alerted, nothing quarantined; routine keys did not"
"${cli[@]}" alerts --clear
