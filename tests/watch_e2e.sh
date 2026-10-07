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
# launcher), once with it off (a forked watcher). Run in a throwaway
# container.
set -euo pipefail

real=/usr/libexec/claude-sandbox/claude
venv_bin="$(readlink -f "$(dirname "$(command -v python)")")"
alerts=/run/claude-sandbox/alerts
work="$(mktemp -d /work/watch.XXXXXX)"
saved="$(mktemp -d)"
py3_was="$(readlink "$venv_bin/python3" || true)"
uv_made=()  # what the uv store check creates, removed on exit
cleanup() {
    [ -e "$saved/real" ] && mv -f "$saved/real" "$real"
    rm -rf "$saved" "$work" "$venv_bin/git" "${uv_made[@]}"
    if [ -n "$py3_was" ]; then ln -sfn "$py3_was" "$venv_bin/python3"; fi
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
        *"quarantined what a sandboxed session left:"*"$venv_bin/git"*) ;;
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
        grep -q "quarantined during this session" session.out \
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

# uv's Python store (ADR 27), as a guest devcontainer has it: under the
# read-write ~/.local/share. bwrap.py binds it read-only and pins the
# directory between (~/.local/share/uv) as a mount point, so a session can
# neither write the store nor move its parent aside and put its own store at
# the path the watcher trusts. A venv link into the store stays; a link to
# an interpreter the session wrote is quarantined. A system python3 later on
# PATH makes the venv's python3 a shadow. The image points
# UV_PYTHON_INSTALL_DIR at its own root-owned store; these launches unset
# it, as a guest devcontainer has it.
share=/root/.local/share
store="$share/uv/python"
[ ! -e "$share/uv" ] || fail "$share/uv exists before the test"
[ ! -e /usr/local/bin/python3 ] || fail "/usr/local/bin/python3 exists before the test"
uv_made+=("$share/uv" /usr/local/bin/python3)
mkdir -p "$store/cpython-3.13-test/bin"
printf '#!/bin/sh\necho store python\n' > "$store/cpython-3.13-test/bin/python3.13"
chmod 755 "$store/cpython-3.13-test/bin/python3.13"
ln -s "$(readlink -f /usr/libexec/claude-sandbox/venv/bin/python)" /usr/local/bin/python3
cat > "$real" <<EOF
#!/bin/bash
# Inside the jail. Each attempt must fail; one that works says ESCAPED.
here="\$PWD"
until_file() { for _ in \$(seq 400); do [ -e "\$1" ] && return 0; sleep 0.05; done; return 1; }
{
    mv $share/uv $share/uv.moved && echo "ESCAPED: moved the parent"
    mv $store $share/python.moved && echo "ESCAPED: moved the store"
    rm -rf $share/uv
    [ -x $store/cpython-3.13-test/bin/python3.13 ] || echo "ESCAPED: removed the store"
    touch $store/x && echo "ESCAPED: wrote the store"
} > "\$here/uv-escapes" 2>&1
ln -sfn $store/cpython-3.13-test/bin/python3.13 "$venv_bin/python3"
touch "\$here/linked-store"
until_file "\$here/go-evil"
mkdir -p $share/evil/bin
printf '#!/bin/sh\necho evil\n' > $share/evil/bin/python3.13
chmod 755 $share/evil/bin/python3.13
ln -sfn $share/evil/bin/python3.13 "$venv_bin/python3"
touch "\$here/linked-evil"
until_file "\$here/go-exit"
rm -rf $share/evil
EOF
rm -f go-exit
{ : > "$alerts"; } 2>/dev/null || true
env -u UV_PYTHON_INSTALL_DIR claude < /dev/null > session.out 2>&1 &
pid=$!
wait_for 30 exists linked-store > /dev/null || fail "uv store: the probe never linked: $(cat session.out)"
! grep -q ESCAPED uv-escapes || fail "uv store: $(cat uv-escapes)"
grep -q "cannot move '$share/uv'.*Device or resource busy" uv-escapes \
    || fail "uv store: moving the parent did not fail with EBUSY: $(cat uv-escapes)"
grep -q "cannot touch '$store/x': Read-only file system" uv-escapes \
    || fail "uv store: writing it did not fail with EROFS: $(cat uv-escapes)"
pass "uv store: inside the jail its parent cannot be moved (EBUSY), nor it written (EROFS), moved or removed"
sleep 1.5  # more than a watcher tick
[ "$(readlink "$venv_bin/python3")" = "$store/cpython-3.13-test/bin/python3.13" ] \
    || fail "uv store: the venv's link into the store was removed: $(cat "$alerts")"
pass "uv store: a venv python3 linked into the read-only store stays"
touch go-evil
wait_for 30 exists linked-evil > /dev/null || fail "uv store: the probe never linked the other python"
wait_for 2 sh -c "[ ! -L '$venv_bin/python3' ]" > /dev/null \
    || fail "uv store: a link to a python the session wrote stays"
grep -qF "removed the link $venv_bin/python3 -> $share/evil/bin/python3.13" "$alerts" \
    || fail "uv store: no alert: $(cat "$alerts")"
pass "uv store: a venv python3 linked to a python the session wrote is quarantined"
touch go-exit
wait "$pid" || fail "uv store: the session failed: $(cat session.out)"
[ -x "$store/cpython-3.13-test/bin/python3.13" ] && [ ! -e "$share/uv.moved" ] \
    || fail "uv store: the store changed outside"

# `uv-python-store = writable` (here through its variable): no bind.
cat > "$real" <<EOF
#!/bin/bash
touch $store/x
EOF
env -u UV_PYTHON_INSTALL_DIR CLAUDE_SANDBOX_UV_PYTHON_STORE=writable claude < /dev/null > session.out 2>&1 \
    || fail "uv store: the writable session failed: $(cat session.out)"
[ -e "$store/x" ] || fail "uv store: uv-python-store = writable left it read-only"
pass "uv store: uv-python-store = writable leaves it writable"
