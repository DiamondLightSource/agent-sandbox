#!/usr/bin/env bash
# Install smoke test. Installs files and user settings in temporary
# directories and asserts on the
# resulting file placement. Set CLAUDE_SANDBOX_SMOKE=1 to skip
# apt-install and the curl-install of the real Claude binary.
#
#   CLAUDE_SANDBOX_SMOKE=1 bash tests/smoke.sh

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

# Shared assertions + PASS/FAIL counters + jq_check + register_cleanup.
# shellcheck source=lib.sh
source "$REPO_ROOT/tests/lib.sh"

PREFIX="$(mktemp -d)"
# USER_HOME is the user-scope ~/.claude home the GLOBAL guard merges
# into. Pin it at a tmpdir so the suite NEVER touches the real
# ~/.claude/settings.json of whoever runs the test.
USER_HOME_DIR="$(mktemp -d)"
register_cleanup "$PREFIX" "$USER_HOME_DIR"

export CLAUDE_SANDBOX_SMOKE=1
# The suite runs against either installer: CLAUDE_SANDBOX_IMPL=python bash
# tests/smoke.sh runs it against the Python one (issue #72 phase 4), which a
# smoke run starts from the tree with this test-only interpreter.
export CLAUDE_SANDBOX_SMOKE_PYTHON="${CLAUDE_SANDBOX_SMOKE_PYTHON:-$(command -v python3)}"
case "${CLAUDE_SANDBOX_IMPL:-bash}" in
    python) SHADOW_SRC="$REPO_ROOT/.devcontainer/claude-sandbox/claude-shim" ;;
    *) SHADOW_SRC="$REPO_ROOT/.devcontainer/claude-sandbox/claude-shadow" ;;
esac
export INSTALL_PREFIX="$PREFIX"
export INSTALL_USER_HOME="$USER_HOME_DIR"

run_install() {
    bash "$REPO_ROOT/.devcontainer/claude-sandbox/install.sh" >/dev/null 2>&1
}

# First install.
if ! run_install; then
    fail "first install run exited non-zero"
fi

# Shadow placement.
if [ -x "$PREFIX/usr/libexec/claude-sandbox/codex-launch" ]; then
    pass
else
    fail "Codex launcher helper was not installed executable"
fi
SHADOW_DEST="$PREFIX/usr/local/bin/claude"
if [ -f "$SHADOW_DEST" ]; then
    pass
else
    fail "shadow not placed at $SHADOW_DEST"
fi

if [ -x "$SHADOW_DEST" ]; then
    pass
else
    fail "shadow not executable"
fi

# install(1) -m 0755 — check mode.
if [ "$(stat -c '%a' "$SHADOW_DEST" 2>/dev/null)" = "755" ]; then
    pass
else
    fail "shadow mode is $(stat -c '%a' "$SHADOW_DEST" 2>/dev/null), expected 755"
fi

# The shadow (or, with the Python opt-in, the shim) placed as is.
if cmp -s "$SHADOW_SRC" "$SHADOW_DEST"; then
    pass
else
    fail "shadow at $SHADOW_DEST is not $SHADOW_SRC"
fi

# Helper CLI placement: on PATH, executable, bash shebang — the shipped
# command surface (gh-auth, glab-auth, update, verify, version) that
# survives deletion of the install clone.
CLI_DEST="$PREFIX/usr/local/bin/claude-sandbox"
if [ -x "$CLI_DEST" ] && [ "$(stat -c '%a' "$CLI_DEST" 2>/dev/null)" = "755" ]; then
    pass
else
    fail "helper CLI missing or not 0755-executable at $CLI_DEST"
fi
if head -1 "$CLI_DEST" | grep -qxF '#!/usr/bin/env bash'; then
    pass
else
    fail "helper CLI does not start with #!/usr/bin/env bash"
fi

# CLI behaviour: help exits 0 and prints usage; unknown subcommand exits 2;
# version reports the stamped value via the test seam.
if bash "$CLI_DEST" help 2>/dev/null | grep -q '^Usage:'; then
    pass
else
    fail "claude-sandbox help did not print a Usage: line"
fi
bash "$CLI_DEST" no-such-command >/dev/null 2>&1
[ "$?" -eq 2 ] && pass || fail "claude-sandbox unknown subcommand did not exit 2"

# Version stamp: recorded from the installing clone (tag when on a tag,
# hash otherwise — same `git describe` the installer runs), and reported
# by `claude-sandbox version`.
VERSION_DEST="$PREFIX/usr/libexec/claude-sandbox/version"
EXPECT_VER="$(git -C "$REPO_ROOT" describe --tags --always --dirty 2>/dev/null || echo unknown)"
if [ "$(cat "$VERSION_DEST" 2>/dev/null)" = "$EXPECT_VER" ]; then
    pass
else
    fail "version stamp at $VERSION_DEST is '$(cat "$VERSION_DEST" 2>/dev/null)', expected '$EXPECT_VER'"
fi
if [ "$(CLAUDE_SANDBOX_VERSION_FILE="$VERSION_DEST" bash "$CLI_DEST" version)" = "claude-sandbox $EXPECT_VER" ]; then
    pass
else
    fail "claude-sandbox version did not report the stamped value"
fi

# Installer stamp (ADR 23): absent for a clone install; `uvx` when the
# wheel drove it, which `claude-sandbox update` turns into a uvx hint.
INSTALLER_DEST="$PREFIX/usr/libexec/claude-sandbox/installer"
[ ! -e "$INSTALLER_DEST" ] && pass || fail "installer stamp present after a clone install"

# The explicit verification battery stays installed read-only in the jail.
BATTERY_DEST="$PREFIX/usr/libexec/claude-sandbox/verify-sandbox-battery.sh"
if [ -x "$BATTERY_DEST" ] && [ "$(stat -c '%a' "$BATTERY_DEST")" = 755 ]; then
    pass
else
    fail "battery missing or not executable"
fi

# `claude-sandbox doctor --fix` copies these, so they outlive the install clone.
cmp -s "$REPO_ROOT/.claude/statusline-command.sh" "$PREFIX/usr/libexec/claude-sandbox/statusline-command.sh" \
    && pass || fail "recommended status line not placed for doctor"
[ "$(stat -c '%a' "$PREFIX/usr/libexec/claude-sandbox/pi-sandbox-tag.ts" 2>/dev/null)" = 644 ] \
    && pass || fail "pi footer extension not placed for doctor"

# Shipped skills: the repo's top-level skills/ tree lands under /usr/libexec
# (root-owned, ro in-session — the shadow binds each skill into the agent's
# own skills dir). Placed by copy, so a byte-diff against the source proves
# the install ships exactly what the checkout carries; nothing is written
# under the user's ~/.claude/skills.
SKILLS_DEST="$PREFIX/usr/libexec/claude-sandbox/skills"
if [ -d "$REPO_ROOT/skills" ] && diff -r "$REPO_ROOT/skills" "$SKILLS_DEST" >/dev/null 2>&1; then
    pass
else
    fail "shipped skills at $SKILLS_DEST do not match $REPO_ROOT/skills"
fi
if [ -e "$USER_HOME_DIR/.claude/skills" ]; then
    fail "install wrote into the user's ~/.claude/skills ($USER_HOME_DIR/.claude/skills)"
else
    pass
fi
# Managed settings disable updates without adding prompt/session hooks.
MANAGED="$PREFIX/etc/claude-code/managed-settings.json"
jq_check "managed updater defaults" \
    '.env.DISABLE_AUTOUPDATER == "1" and .autoUpdates == false and (has("hooks") | not)' "$MANAGED"

# Seed a user statusline preference.
SETTINGS="$USER_HOME_DIR/.claude/settings.json"
SL_DEST="$USER_HOME_DIR/.claude/statusline-command.sh"
if [ -x "$SL_DEST" ]; then
    pass
else
    fail "user-scope statusline not placed/executable at $SL_DEST"
fi
jq_check "user settings.json missing or not valid JSON at $SETTINGS" \
    '.' "$SETTINGS"
jq_check "user settings.json missing/!command .statusLine" \
    '(.statusLine.type == "command") and (.statusLine.command | endswith("statusline-command.sh"))' "$SETTINGS"
jq_check "user hooks were installed" 'has("hooks") | not' "$SETTINGS"

# Config placement: install copies the clone's conf to the host-global
# /etc/claude-sandbox.conf the shadow reads at launch (prefixed for the
# tmpdir). Skip-if-absent in install_conf means this only asserts when
# the clone actually carries a conf — which it does in-tree.
CONF_DEST="$PREFIX/etc/claude-sandbox.conf"
CONF_SRC="$REPO_ROOT/.devcontainer/claude-sandbox.conf"
if [ -f "$CONF_DEST" ]; then
    pass
else
    fail "config not placed at $CONF_DEST"
fi
if cmp -s "$CONF_SRC" "$CONF_DEST"; then
    pass
else
    fail "installed config differs from source conf"
fi

# Reinstalling unchanged inputs is byte-stable.
declare -A SUM_A
for f in "$SHADOW_DEST" "$CLI_DEST" "$VERSION_DEST" "$BATTERY_DEST" "$MANAGED" "$SETTINGS" "$CONF_DEST"; do
    SUM_A["$f"]="$(sha256sum "$f" | awk '{print $1}')"
done

if ! run_install; then
    fail "second install run exited non-zero"
fi

for f in "$SHADOW_DEST" "$CLI_DEST" "$VERSION_DEST" "$BATTERY_DEST" "$MANAGED" "$SETTINGS" "$CONF_DEST"; do
    if [ "${SUM_A[$f]}" = "$(sha256sum "$f" | awk '{print $1}')" ]; then
        pass
    else
        fail "$f drifted across install re-run"
    fi
done

# Preserve existing managed policy and administrator hooks.
MGD_PREFIX="$(mktemp -d)"
register_cleanup "$MGD_PREFIX"
mkdir -p "$MGD_PREFIX/etc/claude-code"
cat > "$MGD_PREFIX/etc/claude-code/managed-settings.json" <<'JSON'
{
  "permissions": {"defaultMode": "plan"},
  "hooks": {
    "SessionStart": [{"hooks": [{"type": "command", "command": "org-audit.sh"}]}]
  }
}
JSON
MGD="$MGD_PREFIX/etc/claude-code/managed-settings.json"
INSTALL_PREFIX="$MGD_PREFIX" INSTALL_USER_HOME="$(mktemp -d)" \
    bash "$REPO_ROOT/.devcontainer/claude-sandbox/install.sh" >/dev/null 2>&1

jq_check "managed merge dropped pre-existing admin key" \
    '.permissions.defaultMode == "plan"' "$MGD"
jq_check "managed merge dropped pre-existing admin hook" \
    'any(.hooks.SessionStart[].hooks[]?; .command == "org-audit.sh")' "$MGD"
jq_check "managed merge changed administrator hooks" \
    '.hooks == {SessionStart: [{hooks: [{type: "command", command: "org-audit.sh"}]}]}' "$MGD"

# Installation preserves user hooks and an existing statusline preference.
SETTINGS_HOME="$(mktemp -d)"
register_cleanup "$SETTINGS_HOME"
mkdir -p "$SETTINGS_HOME/.claude"
cat > "$SETTINGS_HOME/.claude/settings.json" <<'JSON'
{
  "model": "opus",
  "statusLine": {"type": "command", "command": "their-statusline.sh"},
  "hooks": {
    "UserPromptSubmit": [
      {"hooks": [
        {"type": "command", "command": "bash /custom/check-project.sh"},
        {"type": "command", "command": "their-ups.sh"}
      ]}
    ]
  }
}
JSON
cp "$SETTINGS_HOME/.claude/settings.json" "$SETTINGS_HOME/before.json"
printf '#!/usr/bin/env bash\necho custom\n' > "$SETTINGS_HOME/.claude/statusline-command.sh"
chmod 0755 "$SETTINGS_HOME/.claude/statusline-command.sh"
INSTALL_USER_HOME="$SETTINGS_HOME" \
    bash "$REPO_ROOT/.devcontainer/claude-sandbox/install.sh" >/dev/null 2>&1
if jq -e --slurp ' .[0] == .[1] ' "$SETTINGS_HOME/before.json" "$SETTINGS_HOME/.claude/settings.json" >/dev/null; then
    pass
else
    fail "installation changed user settings"
fi
if grep -qx 'echo custom' "$SETTINGS_HOME/.claude/statusline-command.sh"; then
    pass
else
    fail "install_file_if_absent overwrote a pre-existing statusline script"
fi

# link_terminal_config: with CLAUDE_SHARED_CONFIG pointing at a fake
# shared dir and HOME pointing at a fresh tmpdir, install must create
# symlinks at $HOME/.claude{,.json} into the shared dir. INSTALL_USER_HOME
# stays pinned at the global tmpdir so the guard merge never touches HOME.
LINK_HOME="$(mktemp -d)"
LINK_SHARED="$(mktemp -d)"
register_cleanup "$LINK_HOME" "$LINK_SHARED"
HOME="$LINK_HOME" CLAUDE_SHARED_CONFIG="$LINK_SHARED" \
    bash "$REPO_ROOT/.devcontainer/claude-sandbox/install.sh" >/dev/null 2>&1
if [ "$(readlink "$LINK_HOME/.claude" 2>/dev/null)" = "$LINK_SHARED/.claude" ] \
        && [ "$(readlink "$LINK_HOME/.claude.json" 2>/dev/null)" = "$LINK_SHARED/.claude.json" ]; then
    pass
else
    fail "link_terminal_config did not symlink ~/.claude{,.json} into $LINK_SHARED"
fi
# Shared user skills join the share (ADR 25) — the skills dir only, with
# ~/.agents itself left a real, container-local directory.
if [ "$(readlink "$LINK_HOME/.agents/skills" 2>/dev/null)" = "$LINK_SHARED/.agents/skills" ] \
        && [ -d "$LINK_SHARED/.agents/skills" ] \
        && [ ! -L "$LINK_HOME/.agents" ]; then
    pass
else
    fail "link_terminal_config did not symlink ~/.agents/skills into $LINK_SHARED"
fi

# /verify-sandbox phase-1 battery: drive the INSTALLED script outside any
# sandbox. It must RUN TO COMPLETION (header + a well-formed Summary line)
# and exit NON-ZERO — IS_SANDBOX is unset here so at least check 01 fails.
# This is the deterministic, no-live-claude guard for the script's
# plumbing: it catches exactly the regression class that motivated
# extracting the battery from the command markdown — broken awk field
# refs ($1..$9 eaten by slash-command arg substitution) or a glob that
# aborts under a non-bash shell would break the run or the format here.
BATTERY_OUT="$(env -u IS_SANDBOX bash "$BATTERY_DEST" 2>/dev/null)"
BATTERY_RC=$?
if [ "$BATTERY_RC" -ne 0 ] \
        && printf '%s\n' "$BATTERY_OUT" | grep -qx '/verify-sandbox: 22 checks' \
        && printf '%s\n' "$BATTERY_OUT" | grep -qE '^  Summary: [0-9]+ PASS / [0-9]+ FAIL$'; then
    pass
else
    fail "battery did not run-to-format-and-exit-nonzero outside the sandbox (rc=$BATTERY_RC)"
fi

# link_terminal_config ADOPT: a pre-existing *local* ~/.claude{,.json}
# (e.g. written by an unsandboxed claude / VS Code extension before
# install ran) must not shadow an already-populated shared store. The
# shared copy wins; the local one is backed up, not destroyed.
ADOPT_HOME="$(mktemp -d)"
ADOPT_SHARED="$(mktemp -d)"
SEED_HOME="$(mktemp -d)"
SEED_SHARED="$(mktemp -d)"
register_cleanup "$ADOPT_HOME" "$ADOPT_SHARED" "$SEED_HOME" "$SEED_SHARED"
mkdir -p "$ADOPT_HOME/.claude" "$ADOPT_SHARED/.claude"
echo local  > "$ADOPT_HOME/.claude/marker";   printf 'local'  > "$ADOPT_HOME/.claude.json"
echo shared > "$ADOPT_SHARED/.claude/marker"; printf 'shared' > "$ADOPT_SHARED/.claude.json"
HOME="$ADOPT_HOME" CLAUDE_SHARED_CONFIG="$ADOPT_SHARED" \
    bash "$REPO_ROOT/.devcontainer/claude-sandbox/install.sh" >/dev/null 2>&1
if [ "$(readlink "$ADOPT_HOME/.claude" 2>/dev/null)" = "$ADOPT_SHARED/.claude" ] \
        && [ "$(readlink "$ADOPT_HOME/.claude.json" 2>/dev/null)" = "$ADOPT_SHARED/.claude.json" ] \
        && [ "$(cat "$ADOPT_HOME/.claude/marker" 2>/dev/null)" = shared ] \
        && cat "$ADOPT_HOME"/.claude.pre-sandbox.*/marker 2>/dev/null | grep -qx local \
        && cat "$ADOPT_HOME"/.claude.json.pre-sandbox.* 2>/dev/null | grep -qx local; then
    pass
else
    fail "adopt: populated shared must win and local ~/.claude{,.json} be backed up"
fi

# link_terminal_config SEED: when the shared store is empty/absent, a
# real local ~/.claude{,.json} (carrying the first-run OAuth token) must
# be MOVED into the shared store as the baseline — never discarded.
mkdir -p "$SEED_HOME/.claude"
echo seedme > "$SEED_HOME/.claude/marker"
printf 'token-abc' > "$SEED_HOME/.claude.json"
HOME="$SEED_HOME" CLAUDE_SHARED_CONFIG="$SEED_SHARED" \
    bash "$REPO_ROOT/.devcontainer/claude-sandbox/install.sh" >/dev/null 2>&1
if [ "$(readlink "$SEED_HOME/.claude" 2>/dev/null)" = "$SEED_SHARED/.claude" ] \
        && [ "$(readlink "$SEED_HOME/.claude.json" 2>/dev/null)" = "$SEED_SHARED/.claude.json" ] \
        && [ "$(cat "$SEED_SHARED/.claude/marker" 2>/dev/null)" = seedme ] \
        && [ "$(cat "$SEED_SHARED/.claude.json" 2>/dev/null)" = token-abc ]; then
    pass
else
    fail "seed: empty shared must be seeded from local config without data loss"
fi

# Bwrap sanity check (when bwrap is available AND we are not already
# inside a sandbox — nested userns is forbidden). CI installs bwrap as
# a pre-step; locally we tolerate absence or nesting as a skip.
if command -v bwrap >/dev/null 2>&1 && [ "${IS_SANDBOX:-}" != "1" ]; then
    if bwrap --ro-bind / / -- /bin/true >/dev/null 2>&1; then
        pass
    else
        fail "bwrap --ro-bind / / -- /bin/true failed (runner cannot enter a sandbox)"
    fi
fi

# --- Codex CLI: same shadow, same guard, delivered through /etc/codex ---
# The sandbox wraps more than one agent, and the whole design rests on the
# codex path being the SAME machinery rather than a parallel copy. These
# assertions lock that.

# The codex shadow is the SAME FILE as the claude shadow (it dispatches on
# argv[0]). Byte-equality is the assertion: a divergent copy is the failure
# mode this design exists to prevent.
CODEX_SHADOW="$PREFIX/usr/local/bin/codex"
CLAUDE_SHADOW_DEST="$PREFIX/usr/local/bin/claude"
if [ -x "$CODEX_SHADOW" ] && cmp -s "$CODEX_SHADOW" "$CLAUDE_SHADOW_DEST"; then
    pass
else
    fail "codex shadow missing at $CODEX_SHADOW, or has diverged from the claude shadow"
fi

# Codex gets updater defaults, without a managed hook requirements file.
CODEX_MGD="$PREFIX/etc/codex/managed_config.toml"
expect_file "$CODEX_MGD"
[ ! -e "$PREFIX/etc/codex/requirements.toml" ] && pass || fail 'installed Codex hook requirements'

# Updater disabled: a self-update re-creates ~/.local/bin/codex and re-arms
# the bypass.
if grep -q 'check_for_update_on_startup = false' "$CODEX_MGD" 2>/dev/null; then
    pass
else
    fail "codex managed_config.toml does not disable the startup update check"
fi

# install_codex_binary must NEVER relocate the claude-sandbox shadow as the
# "real" codex binary. The shadow is on the search path by construction
# (main() installs it at /usr/local/bin/codex first), so if the vendor
# download leaves nothing behind, the candidate search falls through to it.
# Relocating it makes the shadow exec itself forever — a HANG, with no error
# to go on. Regression test for a marker-grep self-check that stopped
# matching when the shadow's header was reworded.
SELF_PREFIX="$(mktemp -d)"
SELF_HOME="$(mktemp -d)"
register_cleanup "$SELF_PREFIX" "$SELF_HOME"
mkdir -p "$SELF_HOME/.local/bin"
# The one candidate on the search path is a copy of our own shadow.
cp "$REPO_ROOT/.devcontainer/claude-sandbox/claude-shadow" "$SELF_HOME/.local/bin/codex"
chmod 0755 "$SELF_HOME/.local/bin/codex"
SELF_OUT="$( (
    # shellcheck source=../.devcontainer/claude-sandbox/install.sh
    source "$REPO_ROOT/.devcontainer/claude-sandbox/install.sh"
    SMOKE=0; WITH_CODEX=1; PREFIX="$SELF_PREFIX"; HOME="$SELF_HOME"
    # Vendor installer "succeeds" but produces no binary of its own.
    curl() { return 0; }
    install_codex_binary
) 2>&1 || true )"
# Assert the GUARD FIRED, not merely that some path is absent. An earlier
# version of this test looked for a copy of the shadow at
# /usr/libexec/claude-sandbox/codex — a path install_codex_binary never
# writes (codex ships as a package, so the binary lands under
# codex-dist/bin/) — so the assertion passed vacuously and would have kept
# passing if the shadow HAD been relocated.
case "$SELF_OUT" in
    *"is the claude-sandbox"*"shadow itself"*) pass ;;
    *) fail "install_codex_binary did not refuse to relocate the shadow as codex: $SELF_OUT" ;;
esac
# ...and that nothing shadow-shaped landed anywhere under the install prefix,
# whatever the layout: the real destination, the historical one, or any other.
SELF_LIBEXEC="$SELF_PREFIX/usr/libexec/claude-sandbox"
SELF_PLANTED=0
if [ -d "$SELF_LIBEXEC" ]; then
    while IFS= read -r cand; do
        if cmp -s "$cand" "$REPO_ROOT/.devcontainer/claude-sandbox/claude-shadow"; then
            SELF_PLANTED=1
        fi
    done < <(find "$SELF_LIBEXEC" -type f 2>/dev/null)
fi
if [ "$SELF_PLANTED" = 0 ]; then
    pass
else
    fail "install_codex_binary relocated the shadow under $SELF_LIBEXEC (infinite exec loop)"
fi

# Second line of defence: even if something did point the shadow at a copy of
# itself, launching must ERROR rather than spin. A hang is the worst possible
# failure here because it tells the user nothing.
LOOP_DIR="$(mktemp -d)"
register_cleanup "$LOOP_DIR"
sed "s|AGENT_REAL=\"/usr/libexec/claude-sandbox/codex-dist/bin/codex\"|AGENT_REAL=\"$LOOP_DIR/real\"|" \
    "$REPO_ROOT/.devcontainer/claude-sandbox/claude-shadow" > "$LOOP_DIR/codex"
chmod 0755 "$LOOP_DIR/codex"
cp "$LOOP_DIR/codex" "$LOOP_DIR/real"
LOOP_OUT="$(env -u IS_SANDBOX CLAUDE_SANDBOX_AGENT=codex timeout 10 "$LOOP_DIR/codex" 2>&1 || true)"
case "$LOOP_OUT" in
    *"is a copy of this shadow"*) pass ;;
    *) fail "shadow did not refuse to exec a copy of itself (hang risk): $LOOP_OUT" ;;
esac

# A requirements.toml we did NOT write (a site's real Codex policy) must be
# left untouched, with a warning — never bricked. Same call the non-JSON
# managed-settings path makes.
FOREIGN_PREFIX="$(mktemp -d)"
register_cleanup "$FOREIGN_PREFIX"
mkdir -p "$FOREIGN_PREFIX/etc/codex"
FOREIGN_REQ="$FOREIGN_PREFIX/etc/codex/requirements.toml"
printf '# ACME Corp Codex policy\nsandbox_mode = "read-only"\n' > "$FOREIGN_REQ"
FOREIGN_BEFORE="$(cksum < "$FOREIGN_REQ")"
CLAUDE_SANDBOX_SMOKE=1 INSTALL_PREFIX="$FOREIGN_PREFIX" INSTALL_USER_HOME="$(mktemp -d)" \
    bash "$REPO_ROOT/.devcontainer/claude-sandbox/install.sh" >/dev/null 2>&1
if [ "$(cksum < "$FOREIGN_REQ")" = "$FOREIGN_BEFORE" ]; then
    pass
else
    fail "install clobbered a foreign /etc/codex/requirements.toml"
fi

# The Python shadow is opt-in at install time (issue #72 phases 2 and 4):
# CLAUDE_SANDBOX_IMPL=python places the shim under all three names, a default
# re-install puts the bash shadow back and removes the interpreter an opt-in
# left, and any other value refuses before anything is written. (The smoke
# flag skips fetching the interpreter, as it skips the agent binaries.)
IMPL_PREFIX="$(mktemp -d)"
register_cleanup "$IMPL_PREFIX"
impl_install() {
    CLAUDE_SANDBOX_SMOKE=1 INSTALL_PREFIX="$IMPL_PREFIX" INSTALL_USER_HOME="$IMPL_PREFIX/home" \
        bash "$REPO_ROOT/.devcontainer/claude-sandbox/install.sh" >/dev/null 2>&1
}
CLAUDE_SANDBOX_IMPL=python impl_install || fail "CLAUDE_SANDBOX_IMPL=python install exited non-zero"
for agent in claude codex pi; do
    cmp -s "$REPO_ROOT/.devcontainer/claude-sandbox/claude-shim" "$IMPL_PREFIX/usr/local/bin/$agent" \
        && pass || fail "CLAUDE_SANDBOX_IMPL=python did not place the shim as $agent"
done
mkdir -p "$IMPL_PREFIX/usr/libexec/claude-sandbox/venv" "$IMPL_PREFIX/usr/libexec/claude-sandbox/python" \
    "$IMPL_PREFIX/usr/libexec/claude-sandbox/uv"
CLAUDE_SANDBOX_IMPL=bash impl_install || fail "default re-install after CLAUDE_SANDBOX_IMPL=python exited non-zero"
cmp -s "$REPO_ROOT/.devcontainer/claude-sandbox/claude-shadow" "$IMPL_PREFIX/usr/local/bin/claude" \
    && pass || fail "default re-install did not restore the bash shadow"
[ ! -e "$IMPL_PREFIX/usr/libexec/claude-sandbox/venv" ] && [ ! -e "$IMPL_PREFIX/usr/libexec/claude-sandbox/python" ] \
    && [ ! -e "$IMPL_PREFIX/usr/libexec/claude-sandbox/uv" ] \
    && pass || fail "default re-install left the Python installer's interpreter or uv behind"
BAD_PREFIX="$(mktemp -d)"
register_cleanup "$BAD_PREFIX"
CLAUDE_SANDBOX_IMPL=perl CLAUDE_SANDBOX_SMOKE=1 INSTALL_PREFIX="$BAD_PREFIX" INSTALL_USER_HOME="$BAD_PREFIX/home" \
    bash "$REPO_ROOT/.devcontainer/claude-sandbox/install.sh" >/dev/null 2>&1
[ "$?" -eq 2 ] && [ -z "$(ls -A "$BAD_PREFIX")" ] && pass \
    || fail "an unknown CLAUDE_SANDBOX_IMPL did not refuse before writing"

finish smoke.sh
