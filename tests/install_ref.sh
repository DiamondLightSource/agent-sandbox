#!/usr/bin/env bash
# Bash unit test for the REVISION SELECTION in the `install` shim: which
# git ref a given invocation installs, and when it refuses to choose one.
#
# The shim defaults to the newest stable RELEASE TAG so that a first
# install (the documented clone one-liner, which lands on the default
# branch) and a later `claude-sandbox update` agree on what "current"
# means. Two properties here are load-bearing and must not regress:
#
#   1. It NEVER silently retargets a deliberate checkout. A team pins a
#      revision and bumps it as a reviewed act (ADR 0017); quietly
#      installing something newer would defeat the pin on every
#      teammate's next rebuild, with nothing in the diff to show for it.
#   2. Prereleases are excluded. "Stay current" must not hand someone a
#      beta — and a beta sorts ABOVE the release it precedes
#      (2.1.0-beta.1 > 2.0.0 by version sort), so this only holds if the
#      filter is explicit.
#
# Hermetic: builds a throwaway origin+clone with SYNTHETIC tags rather
# than testing against this repo's real history, so the expected answers
# are fixed and every tagged commit carries a STUB install.sh. Checking
# out a real tag would otherwise run that tag's REAL installer.
#
# Run via `bash tests/install_ref.sh`. Needs no capabilities, but DOES need
# root — not for anything it does (the stub installer touches nothing), only
# to clear the shim's own `id -u` gate, which is part of the contract under
# test. In a devcontainer you are already root; CI uses `sudo -E`.
# CLAUDE_SANDBOX_SMOKE=1 would clear that gate too, but it also forces
# --here, which is precisely what the interesting cases here are not.

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SHIM="$REPO_ROOT/install"

if [ ! -f "$SHIM" ]; then
    echo "FAIL: cannot find $SHIM" >&2
    exit 1
fi

# Shared assertions + PASS/FAIL counters + register_cleanup.
# shellcheck source=lib.sh
source "$REPO_ROOT/tests/lib.sh"

T="$(mktemp -d)"
register_cleanup "$T"

# The stub stands in for .devcontainer/claude-sandbox/install.sh. It records
# that it ran into $STUB_MARKER — a path OUTSIDE the clone, so recording a
# run never dirties the tree under test.
STUB=$(cat <<'STUBEOF'
#!/usr/bin/env bash
[ -n "${STUB_MARKER:-}" ] && printf 'ran%s\n' "${CLAUDE_SANDBOX_MINIMAL:+ minimal}" >> "$STUB_MARKER"
exit 0
STUBEOF
)

git_q() { git -C "$1" -c user.email=t@t -c user.name=t "${@:2}"; }

# Build the origin: four commits, tagged so that the newest tag overall is a
# PRERELEASE and the newest stable one is 2.0.0.
ORIGIN="$T/origin"
mkdir -p "$ORIGIN/.devcontainer/claude-sandbox"
git -C "$ORIGIN" init -q -b main
printf '%s\n' "$STUB" > "$ORIGIN/.devcontainer/claude-sandbox/install.sh"
for rev in 1.0.0 2.0.0 2.1.0-beta.1 unreleased; do
    # Every commit carries the shim UNDER TEST, so checking out any tag
    # exercises the real code path, against the stub installer.
    cp "$SHIM" "$ORIGIN/install"
    printf '%s\n' "$rev" > "$ORIGIN/marker"
    git_q "$ORIGIN" add -A
    git_q "$ORIGIN" commit -qm "$rev"
    [ "$rev" = unreleased ] || git_q "$ORIGIN" tag "$rev"
done

# fresh_clone DEST — a pristine clone: on the default branch, clean tree,
# origin/HEAD set. This is what the documented one-liner produces.
fresh_clone() {
    rm -rf "$1"
    git clone -q "$ORIGIN" "$1"
}

# try DEST [ARGS...] — run the shim; echo "<rc>|<head>|ran|NOTRUN".
# head is the tag name when the checkout sits on one, else a short SHA.
# The shim's own output goes to $T/last-out: `try` is called inside a
# command substitution, so a variable could not carry it back out.
try() {
    local dest="$1"; shift
    local marker="$T/last-marker" rc head ran
    rm -f "$marker"
    STUB_MARKER="$marker" bash "$dest/install" "$@" > "$T/last-out" 2>&1; rc=$?
    head="$(git -C "$dest" describe --tags --exact-match 2>/dev/null \
            || git -C "$dest" rev-parse --short HEAD)"
    ran="$( [ -f "$marker" ] && tr -d '\n' < "$marker" || echo NOTRUN )"
    printf '%s|%s|%s\n' "$rc" "$head" "$ran"
}

# said PATTERN — did the last `try` print it?
said() { grep -q -- "$1" "$T/last-out"; }
# diag NAME — the last shim output, for a failure message.
diag() { printf '%s — got: %s' "$1" "$(tr '\n' ' ' < "$T/last-out")"; }

W="$T/work"

# --- 1. Pristine clone, no flag: newest STABLE tag, prerelease skipped ----
fresh_clone "$W"
assert_eq "pristine clone installs newest stable tag (not the prerelease)" \
    "0|2.0.0|ran" "$(try "$W")"
if said 'installing release 2.0.0'; then pass
else fail "$(diag 'pristine clone — expected an "installing release 2.0.0" notice')"; fi

# --- 2. A TEAM PIN must never be silently replaced -------------------------
fresh_clone "$W"; git -C "$W" checkout -q 1.0.0
assert_eq "pinned checkout refuses, stays put, runs nothing" \
    "1|1.0.0|NOTRUN" "$(try "$W")"
if said -- '--here'; then pass
else fail "$(diag 'pinned checkout — the refusal must name --here')"; fi

# --- 3. --here honours the pin --------------------------------------------
assert_eq "--here installs the pinned checkout as-is" \
    "0|1.0.0|ran" "$(try "$W" --here)"

# --- 4. Explicit retargets ------------------------------------------------
assert_eq "--release REF installs that ref from a pinned clone" \
    "0|2.0.0|ran" "$(try "$W" --release 2.0.0)"
fresh_clone "$W"; git -C "$W" checkout -q 1.0.0
assert_eq "--release with no REF retargets a pinned clone to newest stable" \
    "0|2.0.0|ran" "$(try "$W" --release)"

# --- 5. Non-default branch and dirty tree also refuse ----------------------
fresh_clone "$W"; git -C "$W" checkout -q -b feature/x
assert_eq "feature branch refuses" "1|$(git -C "$W" rev-parse --short HEAD)|NOTRUN" "$(try "$W")"
assert_eq "feature branch + --here installs it" \
    "0|$(git -C "$W" rev-parse --short HEAD)|ran" "$(try "$W" --here)"

fresh_clone "$W"; echo scratch > "$W/untracked-note"
assert_eq "dirty tree refuses" "1|$(git -C "$W" rev-parse --short HEAD)|NOTRUN" "$(try "$W")"

# --- 6. A smoke run must never retarget the tree it is testing -------------
fresh_clone "$W"
CLAUDE_SANDBOX_SMOKE=1 STUB_MARKER="$T/smoke-marker" bash "$W/install" >/dev/null 2>&1
assert_eq "CLAUDE_SANDBOX_SMOKE=1 forces --here" \
    "$(git -C "$ORIGIN" rev-parse --short HEAD)" "$(git -C "$W" rev-parse --short HEAD)"

# --- 7. Not a git clone (tarball / vendored copy) --------------------------
# There are no revisions to choose between, so the default installs what is
# there; --release has nothing to resolve and says so rather than guessing.
TAR="$T/tarball"
mkdir -p "$TAR/.devcontainer/claude-sandbox"
cp "$SHIM" "$TAR/install"
printf '%s\n' "$STUB" > "$TAR/.devcontainer/claude-sandbox/install.sh"
rm -f "$T/last-marker"
STUB_MARKER="$T/last-marker" bash "$TAR/install" > "$T/last-out" 2>&1
assert_eq "no .git — installs the files as they are" "ran" \
    "$( [ -f "$T/last-marker" ] && tr -d '\n' < "$T/last-marker" || echo NOTRUN )"
if said 'not a git clone'; then pass
else fail "$(diag 'no .git — expected a notice')"; fi
bash "$TAR/install" --release > "$T/last-out" 2>&1
assert_eq "no .git — --release refuses" "1" "$?"

# --- 8. Argument handling -------------------------------------------------
fresh_clone "$W"
bash "$W/install" --latest > "$T/last-out" 2>&1
assert_eq "unknown argument exits 2" "2" "$?"
if said 'Usage: install'; then pass
else fail "$(diag 'unknown argument — expected usage text')"; fi
assert_eq "--help exits 0" "0" "$(bash "$W/install" --help >/dev/null 2>&1; echo $?)"

# --- 9. --minimal (#101) reaches the installer, across a retarget too ------
fresh_clone "$W"
assert_eq "--minimal with the default ref" "0|2.0.0|ran minimal" "$(try "$W" --minimal)"
fresh_clone "$W"; git -C "$W" checkout -q 1.0.0
assert_eq "--minimal after --here" "0|1.0.0|ran minimal" "$(try "$W" --here --minimal)"
assert_eq "--minimal before --release REF" "0|2.0.0|ran minimal" \
    "$(try "$W" --minimal --release 2.0.0)"

finish install_ref
