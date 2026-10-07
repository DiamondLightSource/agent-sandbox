#!/usr/bin/env bash
# `claude-sandbox doctor` tests: the report, --fix with backups, idempotence,
# and the rc prompt blocks in real bash and zsh. Hermetic: every path doctor
# touches is redirected into a temp dir through the CLI's test seams.
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
source "$HERE/lib.sh"
REPO="$HERE/.."
# CLAUDE_SANDBOX_TEST_CLI is a bash file that runs the Python CLI in the
# container context; tests/python/test_bash_suites.py writes one and runs this.
CLI="${CLAUDE_SANDBOX_TEST_CLI:?run through tests/python/test_bash_suites.py}"

TMP="$(mktemp -d)"
register_cleanup "$TMP"
mkdir -p "$TMP/home/.claude" "$TMP/libexec" "$TMP/terminal-config"
cp "$REPO/.claude/statusline-command.sh" "$TMP/libexec/statusline-command.sh"
cp "$REPO/.devcontainer/claude-sandbox/pi-sandbox-tag.ts" "$TMP/libexec/pi-sandbox-tag.ts"
echo myproj-3f2a > "$TMP/tag"
printf '# user zshrc\n' > "$TMP/terminal-config/zshrc"
printf '# user bashrc\n' > "$TMP/terminal-config/bashrc"
printf '#!/bin/bash\necho mine\n' > "$TMP/home/.claude/statusline-command.sh"
printf '{"theme": "dark", "statusLine": {"type": "command", "command": "mine.sh"}}\n' \
    > "$TMP/home/.claude/settings.json"

# doctor [ENV=VAL ...] -- ARGS...: run the CLI against the temp paths.
doctor() {
    local -a envs=()
    while [ "$1" != "--" ]; do envs+=( "$1" ); shift; done; shift
    OUT="$(env -i PATH="$PATH" HOME="$TMP/home" CLAUDE_SANDBOX_LIBEXEC="$TMP/libexec" \
        CLAUDE_SANDBOX_TAG_FILE="$TMP/tag" USER_TERMINAL_CONFIG="$TMP/terminal-config" \
        CLAUDE_SANDBOX_EGRESS_JAIL=0 "${envs[@]}" bash "$CLI" doctor "$@" 2>&1)"; RC=$?
}
has() { printf '%s\n' "$OUT" | grep -qE -- "$2" && pass || fail "$1: no /$2/ in: $OUT"; }

# --- report only: lists what --fix would change, changes nothing ----------
doctor --
assert_eq "report exits 1 with work to do" 1 "$RC"
has "tag shown" '^  ok +container tag +myproj-3f2a$'
has "status line pending" '^  todo +claude status line '
has "settings pending" '^  todo +claude settings '
has "pi pending" '^  todo +pi footer '
has "zsh pending" '^  todo +zsh prompt '
has "bash pending" '^  todo +bash prompt '
# The host's own /dev/net/tun and conf stay out of it: the jail is off here.
has "tun not judged" '^  skip +tun device +the egress jail is off$'
assert_eq "report leaves the script alone" "echo mine" "$(tail -1 "$TMP/home/.claude/statusline-command.sh")"
ls "$TMP/home/.claude/"*.bak-* >/dev/null 2>&1 && fail "report made a backup" || pass

# --- --fix: installs, backs up the originals, keeps other settings --------
doctor -- --fix
assert_eq "fix exits 0" 0 "$RC"
has "backup reported" '^  backup +statusline-command.sh +saved the original as '
cmp -s "$TMP/libexec/statusline-command.sh" "$TMP/home/.claude/statusline-command.sh" \
    && pass || fail "status line script not installed"
bak=( "$TMP/home/.claude/statusline-command.sh.bak-"* )
assert_eq "original script backed up" "echo mine" "$(tail -1 "${bak[0]}")"
jq_check "statusLine points at the script" \
    '.statusLine.command == "bash $HOME/.claude/statusline-command.sh" and .theme == "dark"' \
    "$TMP/home/.claude/settings.json"
ls "$TMP/home/.claude/settings.json.bak-"* >/dev/null 2>&1 && pass || fail "settings not backed up"
cmp -s "$TMP/libexec/pi-sandbox-tag.ts" "$TMP/home/.pi/agent/extensions/claude-sandbox-tag.ts" \
    && pass || fail "pi extension not installed"
assert_eq "one zsh block" 1 "$(grep -c '^# >>> claude-sandbox prompt tag >>>$' "$TMP/terminal-config/zshrc")"
assert_eq "user zshrc kept" "# user zshrc" "$(head -1 "$TMP/terminal-config/zshrc")"

# --- a second --fix changes nothing ---------------------------------------
doctor -- --fix
assert_eq "second fix exits 0" 0 "$RC"
printf '%s\n' "$OUT" | grep -qE '^  (fixed|backup|todo) ' && fail "second fix did work: $OUT" || pass
assert_eq "still one bash block" 1 "$(grep -c '^# >>> claude-sandbox prompt tag >>>$' "$TMP/terminal-config/bashrc")"
doctor --
assert_eq "report after fix exits 0" 0 "$RC"

# --- an older block is reported, then replaced in place --------------------
printf '# >>> claude-sandbox prompt tag >>>\nold block\n# <<< claude-sandbox prompt tag <<<\n# after\n' \
    > "$TMP/terminal-config/zshrc"
doctor --
has "old block pending" '^  todo +zsh prompt .*older tag block'
doctor -- --fix
has "old block updated" '^  fixed +zsh prompt +updated the tag block'
grep -qx 'old block' "$TMP/terminal-config/zshrc" && fail "old block kept" || pass
assert_eq "one block after update" 1 "$(grep -c '^# >>> claude-sandbox prompt tag >>>$' "$TMP/terminal-config/zshrc")"
assert_eq "text after the block kept" "# after" "$(tail -1 "$TMP/terminal-config/zshrc")"
doctor --
assert_eq "updated block reports ok" 0 "$RC"

# --- refusals ---------------------------------------------------------------
doctor IS_SANDBOX=1 -- --fix
assert_eq "fix refused in a sandbox" 1 "$RC"
doctor -- --bogus
assert_eq "unknown option" 2 "$RC"
rm "$TMP/tag"; doctor --
has "no tag is information, not a failure" '^  info +container tag '
assert_eq "no tag still exits 0" 0 "$RC"

# --- the prompt blocks, sourced twice, in real shells ----------------------
# The blocks read the fixed path /etc/claude-sandbox-tag. Substitute the temp
# tag so the test needs no root; everything else runs as appended.
echo myproj-3f2a > "$TMP/tag"
sed "s#/etc/claude-sandbox-tag#$TMP/tag#g" "$TMP/terminal-config/bashrc" > "$TMP/bashrc"
out="$(env -i PATH="$PATH" HOSTNAME=ws1.example bash --norc -c \
    "PS1='\$ '; source '$TMP/bashrc'; source '$TMP/bashrc'; printf '%s' \"\$PS1\"")"
assert_eq "bash prompt prefixed once" '\[\033[0;37m\]myproj-3f2a\[\033[0m\] $ ' "$out"
if command -v zsh >/dev/null; then
    sed "s#/etc/claude-sandbox-tag#$TMP/tag#g" "$TMP/terminal-config/zshrc" > "$TMP/zshrc"
    out="$(env -i PATH="$PATH" zsh -f -c \
        "HOST=ws1.example; PROMPT='%# '; source '$TMP/zshrc'; source '$TMP/zshrc'; printf '%s' \"\$PROMPT\"")"
    assert_eq "zsh prompt prefixed once" '%F{white}myproj-3f2a%f %# ' "$out"
    # dst layout: a leading blank line, user@host, then the input line. The
    # tag goes on the user@host line, so the blank line stays blank.
    out="$(env -i PATH="$PATH" zsh -f -c \
        "HOST=ws1; PROMPT=\$'\\nuser@host: %~\\n%# '; source '$TMP/zshrc'; source '$TMP/zshrc'; printf '%s' \"\$PROMPT\"")"
    assert_eq "zsh tag on the line above input" $'\n%F{white}myproj-3f2a%f user@host: %~\n%# ' "$out"
else
    echo "SKIP: zsh not installed; zsh prompt block not exercised" >&2
fi
rm "$TMP/tag"
out="$(env -i PATH="$PATH" bash --norc -c "PS1='\$ '; source '$TMP/bashrc'; printf '%s' \"\$PS1\"")"
assert_eq "bash prompt unchanged without a tag" '$ ' "$out"

finish doctor
