#!/usr/bin/env bash
# The entry-point guard of the opt-in Python shadow, end to end: protect the
# sandbox's entry-point names (Invariant 1). A session cannot create a command
# named claude, codex, pi or claude-sandbox in a writable directory that
# precedes the shadow on PATH, and the shadow refuses to launch when one is
# there. Run it INSIDE this repository's image with the Python shadow
# installed (CLAUDE_SANDBOX_IMPL=python) and the egress jail on, started as
# .github/workflows/container.yml starts it. The image's PATH starts with the
# project venv's bin, and its conf allows writes under it.
#
# It swaps the real claude binary for a probe for the duration (restored on
# exit), so run it in a throwaway container.
set -euo pipefail

real=/usr/libexec/claude-sandbox/claude
venv_bin="$(readlink -f "$(dirname "$(command -v python)")")"
work="$(mktemp -d)"
cleanup() {
    [ -e "$work/real" ] && mv -f "$work/real" "$real"
    rm -rf "$work"
}
trap cleanup EXIT

fail() { echo "FAIL: $*" >&2; exit 1; }
pass() { echo "PASS: $*"; }

grep -q " -I -m claude_sandbox _shadow " /usr/local/bin/claude \
    || fail "the Python shadow is not installed"
case ":$PATH:" in
    *":$(dirname "$(command -v python)"):"*":/usr/local/bin:"*) ;;
    *) fail "the venv's bin does not precede /usr/local/bin on PATH ($PATH)" ;;
esac
[ "$(command -v claude)" = /usr/local/bin/claude ] || fail "claude is $(command -v claude)"

# The probe, launched by a plain `claude` in place of the real binary: from
# inside the jail, try to make each name in the venv's bin, any way it can.
mv "$real" "$work/real"
cat > "$real" <<EOF
#!/bin/bash
out="\$PWD/probe.out"
: > "\$out"
for name in claude codex pi claude-sandbox; do
    p="$venv_bin/\$name"
    { printf '#!/bin/sh\n' > "\$p"; } 2>/dev/null && echo "wrote \$p" >> "\$out"
    rm -f "\$p" 2>/dev/null && echo "removed \$p" >> "\$out"
    mv -f "\$p" "\$p.moved" 2>/dev/null && echo "renamed \$p" >> "\$out"
    ln -sf /bin/sh "\$p" 2>/dev/null && echo "linked \$p" >> "\$out"
    chmod 755 "\$p" 2>/dev/null && echo "chmodded \$p" >> "\$out"
    [ -x "\$p" ] && echo "executable \$p" >> "\$out"
done
[ -w "$venv_bin" ] && echo "venv-bin-writable" >> "\$out"
printf '%s\n' "\$CLAUDE_SANDBOX_ENTRY_GUARD" >> "\$out"
echo done >> "\$out"
EOF
chmod 755 "$real"

cd "$work"
claude < /dev/null
[ "$(tail -n1 probe.out)" = done ] || fail "the probe did not finish: $(cat probe.out)"
[ "$(head -n1 probe.out)" = "venv-bin-writable" ] \
    || fail "the jail changed something it should not have: $(cat probe.out)"
[ "$(sed -n 2p probe.out)" = "$venv_bin" ] \
    || fail "CLAUDE_SANDBOX_ENTRY_GUARD is '$(sed -n 2p probe.out)'"
pass "inside the jail, no entry-point name in $venv_bin could be made"

for name in claude codex pi claude-sandbox; do
    [ -f "$venv_bin/$name" ] && [ ! -x "$venv_bin/$name" ] && [ ! -s "$venv_bin/$name" ] \
        || fail "$venv_bin/$name is not the empty, non-executable mount point"
done
for shell in bash sh; do
    got="$("$shell" -c 'command -v claude')"
    [ "$got" = /usr/local/bin/claude ] || fail "$shell: command -v claude is $got"
done
pass "outside the jail, the empty mount points leave claude resolving to the shadow"

# Through script(1)'s terminal, so lines end in CR LF.
claude-sandbox verify < /dev/null | tr -d '\r' > verify.out \
    || fail "the battery failed: $(cat verify.out)"
grep -qxF "  [PASS] 22 entry-point names guarded in writable PATH dirs ahead of the shadow" verify.out \
    || fail "check 22: $(grep ' 22 ' verify.out)"
pass "battery check 22 passes, with directories to check"

# Made outside the jail, an executable entry point refuses the launch.
rm -f "$venv_bin/codex"
printf '#!/bin/sh\necho not the shadow\n' > "$venv_bin/codex"
chmod 755 "$venv_bin/codex"
if claude < /dev/null > launch.out 2>&1; then
    fail "the launch went ahead: $(cat launch.out)"
fi
grep -qF "$venv_bin/codex is ahead of /usr/local/bin/codex on PATH" launch.out \
    || fail "unexpected refusal: $(cat launch.out)"
[ -x "$venv_bin/codex" ] || fail "the shadow removed or changed the file"
rm -f "$venv_bin/codex"
pass "the shadow refuses to launch past an executable entry point, and names it"
