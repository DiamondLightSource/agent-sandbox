#!/usr/bin/env bash
# claude-sandbox installer (bash-only). Idempotent: re-runs after a
# devcontainer rebuild re-establish container state without disturbing
# workspace edits.
#
# Test paths:
#   INSTALL_PREFIX    (default /)     — root of installed files
#   INSTALL_USER_HOME (default $HOME) — user settings and credential dirs
#   CLAUDE_SANDBOX_SMOKE=1            skip apt + the curl-install of every
#                                    agent binary.
#   WITH_CODEX=0                     skip fetching OpenAI's Codex CLI. The
#                                    codex SHADOW is
#                                    installed either way — the shadow
#                                    has to own the name on $PATH before the
#                                    vendor's installer can claim it
#                                    (Invariant 1), so opting out here only
#                                    skips the download.
#   WITH_PI=0                        skip the Pi standalone download;
#                                    its shadow and launch guard stay installed.
#   PI_VERSION=0.85.1                optional release pin (default: latest on a
#                                    fresh install; keep existing installations).
#   STATUS=1                         force-overwrite the user-scope
#                                    statusline script from the clone's
#                                    copy, instead of seed-only-if-absent.
#   CLAUDE_SANDBOX_IMPL=python       install the Python shadow (issue #72
#                                    phase 2, opt-in) instead of the bash
#                                    one; see provision_python_shadow.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# REPO_ROOT is the clone — two levels above .devcontainer/claude-sandbox.
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
PREFIX="${INSTALL_PREFIX:-/}"
USER_HOME="${INSTALL_USER_HOME:-$HOME}"
SMOKE="${CLAUDE_SANDBOX_SMOKE:-0}"
WITH_CODEX="${WITH_CODEX:-1}"
WITH_PI="${WITH_PI:-1}"
# Like Claude and Codex, fresh containers install the current release during
# postCreate. The standalone release includes its runtime and assets.
PI_VERSION="${PI_VERSION:-latest}"
FORCE_STATUSLINE="${STATUS:-0}"
IMPL="${CLAUDE_SANDBOX_IMPL:-bash}"

# Resolve a target under $PREFIX. Stripping the leading slash lets us
# compose relative-to-prefix paths cleanly without a `//` between root
# and the absolute path.
prefixed() {
    local abs="$1"
    if [ "$PREFIX" = "/" ]; then
        printf '%s\n' "$abs"
    else
        printf '%s\n' "${PREFIX%/}${abs}"
    fi
}

probe_or_refuse() {
    if [ "$SMOKE" = "1" ]; then
        return 0
    fi
    if ! command -v apt-get >/dev/null 2>&1; then
        echo "claude-sandbox: refusing — Debian/Ubuntu only (no apt-get on PATH)." >&2
        exit 1
    fi
}

apt_install() {
    if [ "$SMOKE" = "1" ]; then
        return 0
    fi
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -qq
    # passt provides `pasta`, the userspace network forwarder the egress
    # jail (ADR 0015) attaches to a per-Claude netns. The jail is ON by
    # default and fail-closed, so passt is installed unconditionally: a
    # host with the jail on (the default) must not fail to launch claude
    # for want of pasta. It's tiny. The matching host-side dep,
    # --device=/dev/net/tun, is a
    # devcontainer.json runArg this installer cannot add (see claude-shadow's
    # netns_launch error message and claude-sandbox.conf).
    apt-get install -y -qq --no-install-recommends \
        bubblewrap jq curl ca-certificates git nodejs gh passt socat iproute2 \
        ripgrep fd-find
    # Pi recognises Debian/Ubuntu's fdfind name directly. Installing its search
    # tools here avoids first-run downloads whose tar ownership cannot be
    # restored inside the sandbox's single-UID user namespace.
    # glab isn't in every Ubuntu repo; install-try.
    apt-get install -y -qq --no-install-recommends glab 2>/dev/null || true
}

probe_userns_or_refuse() {
    if [ "$SMOKE" = "1" ]; then
        return 0
    fi
    if ! bwrap --ro-bind / / --unshare-user-try --unshare-pid -- /bin/true \
            >/dev/null 2>&1; then
        cat >&2 <<'EOF'
claude-sandbox: refusing — kernel unprivileged user namespaces are
forbidden. The bwrap sandbox cannot start without them.

On Ubuntu 24.04:
    sudo sysctl -w kernel.apparmor_restrict_unprivileged_userns=0
On rootful Docker with default AppArmor: rebuild the devcontainer
under rootless podman, or relax AppArmor for bwrap.
EOF
        exit 1
    fi
}

# install_claude_binary: fetch the real Claude via the official
# installer, then relocate it to a path that is NOT on the user's
# PATH. The official installer drops the binary at ~/.local/bin/claude
# AND prepends ~/.local/bin to the user's shell rc — meaning plain
# `claude` would resolve past our shadow once a new shell starts. By
# moving the binary to /usr/libexec/claude-sandbox/, ~/.local/bin/
# stays empty and the rc-mutation becomes harmless.
install_claude_binary() {
    if [ "$SMOKE" = "1" ]; then
        return 0
    fi
    local real_dest
    real_dest="$(prefixed /usr/libexec/claude-sandbox/claude)"
    if [ -x "$real_dest" ]; then
        # Idempotent: purge any stale copy a prior curl-install may have
        # left at ~/.local/bin/claude so the shadow remains the only
        # `claude` on the user's PATH.
        rm -f "$HOME/.local/bin/claude"
        return 0
    fi
    curl -fsSL https://claude.ai/install.sh | bash
    if [ ! -x "$HOME/.local/bin/claude" ]; then
        echo "claude-sandbox: official installer did not produce \$HOME/.local/bin/claude" >&2
        exit 1
    fi
    mkdir -p "$(dirname "$real_dest")"
    mv "$HOME/.local/bin/claude" "$real_dest"
}

# install_codex_binary: same relocate-after-install dance as Claude, for
# OpenAI's Codex CLI (the client for GPT-6 Astra). Its installer also drops
# the binary into ~/.local/bin and prepends that dir to the user's shell rc,
# so leaving it there would let plain `codex` resolve past our shadow —
# Invariant 1, identically.
#
# BEST-EFFORT, unlike Claude's: a host that cannot reach chatgpt.com, or a
# vendor change to the install script, must not brick a claude-sandbox
# install. We warn and carry on; the codex shadow is still placed, and it
# loud-fails with a re-run-install message if someone types `codex`.
#
# What the vendor installer actually does (verified against
# chatgpt.com/codex/install.sh): it unpacks a versioned release under
# $CODEX_HOME/packages/standalone/releases/<version>/ and puts a SYMLINK to it
# at ~/.local/bin/codex. Two consequences we have to handle:
#
#   1. Relocating the symlink would relocate nothing. We resolve it first and
#      relocate the file it points at.
#   2. The real binary therefore lives INSIDE ~/.codex, which the shadow binds
#      READ-WRITE (it is CODEX_HOME — config and auth live there too). A copy
#      of the agent's own binary inside its writable set is a persistence
#      foothold: a compromised session could rewrite it and be re-executed by
#      the next launch. So we take a COPY to the root-owned, off-PATH,
#      ro-in-sandbox /usr/libexec, exec only that, and the shadow tmpfs-masks
#      $HOME/.codex/packages so the writable vendor tree is not visible in the
#      session at all — exactly what is already done for Claude's versioned
#      binary cache at ~/.local/share/claude.
#   3. ~/.codex is by this point usually a SYMLINK into the shared
#      cross-container store (link_terminal_config runs first), so letting the
#      vendor unpack there would push a versioned release tree — tens of MB,
#      one per release, pruned by nothing — onto the host share, where every
#      container that mounts it inherits the growth. We only ever want the
#      /usr/libexec copy, so the vendor unpacks into a TEMP CODEX_HOME that is
#      deleted once the copy is taken.

# codex_purge_vendor_tree [STAGE]: remove every writable copy of the codex
# package the vendor installer leaves behind, plus the unwrapped `codex` on the
# user's PATH (Invariant 1). Called on every exit path — including the
# idempotent one, so a tree left by an older claude-sandbox is cleaned up too.
codex_purge_vendor_tree() {
    local stage="${1:-}"
    # if, not `[ ... ] && rm`: this file runs under `set -e`, where a bare
    # AND-list that ends false takes the whole install down with it.
    if [ -n "$stage" ] && [ -d "$stage" ]; then
        rm -rf "$stage"
    fi
    # Belt and braces: if the vendor installer ignored CODEX_HOME and unpacked
    # into the real one anyway, that tree must not persist on the share either.
    # Safe to delete unconditionally — we never exec from it (the binary we run
    # is the /usr/libexec copy) and the shadow tmpfs-masks it in-session.
    rm -rf "$HOME/.codex/packages"
    rm -f "$HOME/.local/bin/codex" "$HOME/.local/bin/codex-code-mode-host"
    return 0
}

install_codex_binary() {
    if [ "$SMOKE" = "1" ] || [ "$WITH_CODEX" != "1" ]; then
        return 0
    fi
    local dist_dest real_dest stage
    dist_dest="$(prefixed "$CODEX_DIST_DIR")"
    real_dest="$(prefixed "$CODEX_REAL_PATH")"
    if [ -x "$real_dest" ]; then
        codex_purge_vendor_tree
        return 0
    fi
    stage="$(mktemp -d)"
    # CODEX_NON_INTERACTIVE=1: the vendor installer ends with a "Start Codex
    # now? [y/N]" prompt that it reads from /dev/tty — NOT from stdin — so
    # piping the script to sh does not make it unattended. Without this, a
    # plain `./install` blocks on that prompt in any real terminal.
    #
    # TAR_OPTIONS=--no-same-owner: the release tarball carries uid/gid 1001,
    # and tar running as root tries to restore that ownership. In a ROOTLESS
    # container — the supported runtime — in-container root maps to the
    # unprivileged host user, uid 1001 is outside the mapped range, and every
    # chown fails: "Cannot change ownership to uid 1001 ... Operation not
    # permitted", then "Exiting with failure status". GNU tar reads this
    # variable and prepends the option, which is the only seam the vendor
    # script leaves us (it invokes tar itself).
    if ! curl -fsSL https://chatgpt.com/codex/install.sh \
            | CODEX_HOME="$stage" CODEX_NON_INTERACTIVE=1 TAR_OPTIONS=--no-same-owner sh; then
        echo "claude-sandbox: WARNING — the Codex CLI installer failed; skipping codex." >&2
        echo "  The codex shadow is still installed and will refuse to launch until" >&2
        echo "  a real binary lands at $real_dest. Re-run ./install to retry." >&2
        codex_purge_vendor_tree "$stage"
        return 0
    fi

    # Find the RELEASE DIRECTORY, not just the binary. Codex ships as a
    # package: the vendor's own validity check requires codex-package.json,
    # bin/codex, bin/codex-code-mode-host, codex-path/rg (ripgrep) and
    # codex-resources/bwrap to sit together. Relocating bin/codex alone would
    # strip codex of its search tool and its own sandbox helpers.
    #
    # ~/.local/bin/codex is a symlink into that directory, so resolve it and
    # walk up from bin/.
    local link resolved release_dir=""
    link="$HOME/.local/bin/codex"
    if [ -L "$link" ] || [ -x "$link" ]; then
        resolved="$(readlink -f "$link" 2>/dev/null || true)"
        if [ -n "$resolved" ] && [ -f "$resolved" ]; then
            # Never relocate our own shadow (it is on the search path by
            # construction, so a vendor download that leaves nothing behind
            # falls through to it). Compared by CONTENT, not by grepping for a
            # marker string: a marker is a promise about a file's wording that
            # nobody remembers to keep when the header is reworded, and
            # installing the shadow as its own target HANGS (the shadow execs
            # itself forever) rather than erroring.
            #
            # Compared against the RESOLVED binary, not "$release_dir/bin/codex":
            # release_dir is derived by two branches below, and on the plain
            # */codex one it is the binary's own parent — so "$release_dir/bin/
            # codex" named a different (usually absent) file and the check
            # silently no-opped on exactly the path that needed it.
            if cmp -s "$resolved" "$SCRIPT_DIR/claude-shadow" \
                    || cmp -s "$resolved" "$SCRIPT_DIR/claude-shim"; then
                echo "claude-sandbox: WARNING — $resolved is the claude-sandbox" >&2
                echo "  shadow itself, not a real codex binary; skipping codex relocation." >&2
                codex_purge_vendor_tree "$stage"
                return 0
            fi
            case "$resolved" in
                */bin/codex) release_dir="${resolved%/bin/codex}" ;;
                */codex)     release_dir="${resolved%/codex}" ;;
            esac
        fi
    fi
    # Fall back to the vendor's "current" symlink if the PATH entry is gone.
    # The staged CODEX_HOME first; the real one second, in case a future vendor
    # installer ignores CODEX_HOME and unpacks into ~/.codex regardless.
    local cur
    for cur in "$stage/packages/standalone/current" \
               "$HOME/.codex/packages/standalone/current"; do
        if [ -z "$release_dir" ] && [ -d "$cur" ]; then
            release_dir="$(readlink -f "$cur" 2>/dev/null || true)"
        fi
    done
    if [ -z "$release_dir" ] || [ ! -d "$release_dir" ]; then
        echo "claude-sandbox: WARNING — the Codex CLI installer ran but no release" >&2
        echo "  directory was found; skipping codex relocation." >&2
        codex_purge_vendor_tree "$stage"
        return 0
    fi
    # Same identity check again for the fallback branch, which reaches
    # release_dir without ever resolving the PATH symlink.
    if [ -f "$release_dir/bin/codex" ] \
            && { cmp -s "$release_dir/bin/codex" "$SCRIPT_DIR/claude-shadow" \
                || cmp -s "$release_dir/bin/codex" "$SCRIPT_DIR/claude-shim"; }; then
        echo "claude-sandbox: WARNING — $release_dir/bin/codex is the claude-sandbox" >&2
        echo "  shadow itself, not a real codex binary; skipping codex relocation." >&2
        codex_purge_vendor_tree "$stage"
        return 0
    fi

    # Copy the WHOLE package to a root-owned, off-PATH location. Read-only
    # inside the sandbox (--ro-bind / /), so — unlike Claude's, which is
    # bind-mounted rw at ~/.local/bin/claude — an in-session self-update
    # cannot rewrite the binary we exec.
    rm -rf "$dist_dest"
    mkdir -p "$(dirname "$dist_dest")"
    cp -a "$release_dir" "$dist_dest"
    chmod -R go-w "$dist_dest"
    if [ ! -x "$real_dest" ]; then
        echo "claude-sandbox: WARNING — copied $release_dir but $real_dest is not" >&2
        echo "  executable; codex will refuse to launch." >&2
    fi
    # Nothing unwrapped, and no writable copy of the package, may remain
    # (Invariant 1, plus the shared-store growth noted above).
    codex_purge_vendor_tree "$stage"
}

# Download Pi into a private staging directory and verify its checksum.
# Retry transient failures; preserve an existing installation on failure.
install_pi_binary() (
    [ "$SMOKE" != 1 ] && [ "$WITH_PI" = 1 ] || return 0
    local arch asset stage dest base checksum version release_url
    case "$(uname -m)" in
        x86_64) arch=x64 ;;
        aarch64|arm64) arch=arm64 ;;
        *) echo 'claude-sandbox: WARNING — Pi standalone supports Linux x64/arm64; skipping.' >&2; return 0 ;;
    esac
    dest="$(prefixed /usr/libexec/claude-sandbox/pi-dist)"
    if [ -x "$dest/pi" ] && { [ "$PI_VERSION" = latest ] \
        || [ "$(cat "$dest/.sandbox-version" 2>/dev/null)" = "$PI_VERSION" ]; }; then
        return 0
    fi
    version="$PI_VERSION"
    if [ "$version" = latest ]; then
        # Resolve ONCE, then fetch the archive and checksums from the same tag.
        # The release-page redirect avoids GitHub's anonymous API rate limit.
        if ! release_url="$(curl -fsSLI --retry 6 -o /dev/null -w '%{url_effective}' \
            https://github.com/earendil-works/pi/releases/latest)"; then
            echo 'claude-sandbox: WARNING — could not resolve the latest Pi release; existing installation preserved.' >&2
            return 0
        fi
        version="${release_url#https://github.com/earendil-works/pi/releases/tag/v}"
    fi
    if ! [[ "$version" =~ ^[0-9]+\.[0-9]+\.[0-9]+([.-][A-Za-z0-9.-]+)?$ ]]; then
        echo 'claude-sandbox: WARNING — invalid Pi release version; existing installation preserved.' >&2
        return 0
    fi
    stage="$(mktemp -d)"
    trap 'rm -rf "$stage"' EXIT
    asset="pi-linux-$arch.tar.gz"
    base="https://github.com/earendil-works/pi/releases/download/v$version"
    if ! curl -fLSs --retry 6 "$base/$asset" -o "$stage/$asset" \
        || ! curl -fLSs --retry 6 "$base/SHA256SUMS" -o "$stage/SHA256SUMS"; then
        echo 'claude-sandbox: WARNING — Pi download failed; its shadow remains installed.' >&2
        return 0
    fi
    checksum="$(awk -v asset="$asset" '$2 == asset || $2 == "*" asset {print $1}' "$stage/SHA256SUMS")"
    if ! [[ "$checksum" =~ ^[a-fA-F0-9]{64}$ ]] \
        || ! (cd "$stage" && printf '%s  %s\n' "$checksum" "$asset" | sha256sum -c - >/dev/null) \
        || ! tar -xzf "$stage/$asset" --no-same-owner -C "$stage" \
        || [ ! -x "$stage/pi/pi" ]; then
        echo 'claude-sandbox: WARNING — Pi release validation failed; existing installation preserved.' >&2
        return 0
    fi
    # Keep all assets together. The entire tree is read-only in the sandbox.
    mkdir -p "$(dirname "$dest")"
    rm -rf "$dest"
    mv "$stage/pi" "$dest"
    printf '%s\n' "$version" > "$dest/.sandbox-version"
)

# install_file: byte-stable copy of src → dst, mode 0755 unless a third
# argument names one (0644 for data such as the Pi system note). Refuses
# if src is missing (loud-fail beats a downstream errno). cmp -s
# short-circuits so a re-run is a true no-op when content matches.
install_file() {
    local src="$1" dst="$2" mode="${3:-0755}"
    if [ ! -f "$src" ]; then
        echo "claude-sandbox: cannot find $src" >&2
        exit 1
    fi
    mkdir -p "$(dirname "$dst")"
    if [ -f "$dst" ] && cmp -s "$src" "$dst"; then
        return 0
    fi
    install -m "$mode" "$src" "$dst"
}

# install_file_if_absent: place src at dst (mode 0755) only when dst is
# absent. Used for the user-scope statusline, which we seed on a fresh
# machine but never stomp if the owner already has one — the field is
# likewise set-only-if-absent in wire_user_statusline.
install_file_if_absent() {
    local src="$1" dst="$2"
    if [ ! -f "$src" ]; then
        echo "claude-sandbox: cannot find $src" >&2
        exit 1
    fi
    [ -f "$dst" ] && return 0
    mkdir -p "$(dirname "$dst")"
    install -m 0755 "$src" "$dst"
}

ensure_cred_dirs() {
    mkdir -p "$USER_HOME/.config/gh" "$USER_HOME/.config/glab-cli"
    touch "$USER_HOME/.claude.json"
    # ~/.codex is CODEX_HOME: config.toml, auth.json, sessions/. Pre-created
    # so the shadow's --bind of it succeeds on a first-ever codex launch.
    mkdir -p "$USER_HOME/.codex"
    mkdir -p "$USER_HOME/.pi/agent"
}

# install_conf: place the clone's claude-sandbox.conf at the host-global
# /etc/claude-sandbox.conf the shadow reads at launch. Re-run on every
# rebuild (via postCreate) so the /etc copy tracks the clone's conf.
# Unlike install_file this is skip-if-absent: a clone that carries no
# conf simply gets no global config
# (parse_config then no-ops). Mode 0644 — it's data, not an executable.
# cmp -s short-circuits so a re-run with unchanged content is a no-op.
install_conf() {
    local src dst
    src="$REPO_ROOT/.devcontainer/claude-sandbox.conf"
    dst="$(prefixed /etc/claude-sandbox.conf)"
    [ -f "$src" ] || return 0
    mkdir -p "$(dirname "$dst")"
    if [ -f "$dst" ] && cmp -s "$src" "$dst"; then
        return 0
    fi
    install -m 0644 "$src" "$dst"
}

# stamp_version: record what this clone is at install time, for
# `claude-sandbox version`. A tagged checkout stamps the tag (git
# describe on a tag == the tag, which is what `claude-sandbox update`
# installs); an unpinned main clone stamps a commit hash — honest
# "unreleased" reporting. CLAUDE_SANDBOX_VERSION overrides for builds
# with no .git (the container image). Deterministic per clone, so the
# byte-stable re-run property holds.
stamp_version() {
    local ver dst
    dst="$(prefixed "$VERSION_FILE_PATH")"
    ver="${CLAUDE_SANDBOX_VERSION:-$(git -C "$REPO_ROOT" describe --tags --always --dirty 2>/dev/null || echo unknown)}"
    mkdir -p "$(dirname "$dst")"
    if [ -f "$dst" ] && [ "$(cat "$dst")" = "$ver" ]; then
        return 0
    fi
    printf '%s\n' "$ver" > "$dst"
    chmod 0644 "$dst"
}

# stamp_installer: record the front door (see INSTALLER_FILE_PATH). Byte-
# stable on re-run like stamp_version; removed when a clone install
# replaces a wheel install so the record never lies.
stamp_installer() {
    local dst
    dst="$(prefixed "$INSTALLER_FILE_PATH")"
    if [ -z "${CLAUDE_SANDBOX_INSTALLER:-}" ]; then
        rm -f "$dst"
        return 0
    fi
    if [ -f "$dst" ] && [ "$(cat "$dst")" = "$CLAUDE_SANDBOX_INSTALLER" ]; then
        return 0
    fi
    printf '%s\n' "$CLAUDE_SANDBOX_INSTALLER" > "$dst"
    chmod 0644 "$dst"
}

# _is_mount PATH — true if PATH is itself a mount target. Compares the
# st_dev of PATH against its parent (the heuristic mountpoint(1) uses),
# NOT /proc/mounts: inside the sandbox /proc is the host's procfs
# (--ro-bind /proc /proc), so /proc/mounts shows the host namespace and
# would miss a path the sandbox itself bind-mounted. stat(2) queries the
# live kernel, and unlike mountpoint(1) the st_dev check works for a
# bind-mounted ~/.claude.json (a regular file) too.
_is_mount() {
    local path="$1" pdev ddev
    [ -e "$path" ] || return 1
    pdev="$(stat -c '%d' "$path" 2>/dev/null)" || return 1
    ddev="$(stat -c '%d' "$(dirname "$path")" 2>/dev/null)" || return 1
    [ "$pdev" != "$ddev" ]
}

# _is_empty PATH KIND — true when an existing dir has no entries / a file
# is zero-length. Caller guarantees PATH exists.
_is_empty() {
    local path="$1" kind="$2"
    if [ "$kind" = dir ]; then
        [ -z "$(ls -A "$path" 2>/dev/null)" ]
    else
        [ ! -s "$path" ]
    fi
}

# _ensure_shared SHARED KIND — create an empty shared dir/file when
# absent, so a symlink into it never dangles.
_ensure_shared() {
    local shared="$1" kind="$2"
    if [ -e "$shared" ]; then return 0; fi
    if [ "$kind" = dir ]; then
        mkdir -p "$shared"
    else
        mkdir -p "$(dirname "$shared")"
        : > "$shared"
    fi
}

# _share_path TARGET SHARED KIND — make TARGET ($HOME/.claude{,.json}) a
# symlink into the shared config store, picking the data-preserving
# action for whatever TARGET currently is. The old create-if-absent
# guard silently lost the share whenever TARGET already existed — and in
# a devcontainer that doesn't run install from postCreate an
# unsandboxed `claude` or the VS Code extension routinely writes a local
# ~/.claude before install ever runs, permanently shadowing the share.
# Cases:
#   symlink -> SHARED         : nothing to do (idempotent re-run).
#   symlink elsewhere         : repoint to SHARED.
#   active mountpoint         : leave alone — a devcontainer that binds
#                               ~/.claude in directly is already shared,
#                               and a busy mount can't be moved (EBUSY).
#   real, SHARED empty/absent : SEED — move TARGET into SHARED, then
#                               symlink. Preserves a first-run OAuth
#                               token / local history as the new baseline.
#   real, SHARED populated    : shared wins — back TARGET up timestamped,
#                               then symlink (the "adopt" case).
#   absent                    : ensure SHARED exists, then symlink.
_share_path() {
    local target="$1" shared="$2" kind="$3"

    if [ -L "$target" ]; then
        if [ "$(readlink "$target")" = "$shared" ]; then
            return 0
        fi
        _ensure_shared "$shared" "$kind"
        rm -f "$target"
        ln -s "$shared" "$target"
        return 0
    fi

    if _is_mount "$target"; then
        echo "claude-sandbox: $target is an active mountpoint; leaving as-is (assumed already shared)." >&2
        return 0
    fi

    if [ -e "$target" ]; then
        if [ ! -e "$shared" ] || _is_empty "$shared" "$kind"; then
            if [ -e "$shared" ]; then rm -rf "$shared"; fi
            mkdir -p "$(dirname "$shared")"
            mv "$target" "$shared"
            echo "claude-sandbox: seeded shared config $shared from $target." >&2
        else
            local backup="$target.pre-sandbox.$(date +%Y%m%d-%H%M%S)"
            mv "$target" "$backup"
            echo "claude-sandbox: $shared already populated; backed up $target -> $backup." >&2
        fi
        ln -s "$shared" "$target"
        return 0
    fi

    _ensure_shared "$shared" "$kind"
    ln -s "$shared" "$target"
}

# link_terminal_config: when /user-terminal-config is mounted (the
# convention used by terminal-config-style devcontainers), share
# ~/.claude and ~/.claude.json into it so Claude's settings and OAuth
# state follow the user across every devcontainer on the host. Runs
# before install_claude_binary. Delegates per-path policy to
# _share_path, which adopts (or seeds from) a pre-existing local config
# instead of silently leaving it un-shared.
link_terminal_config() {
    local shared="${CLAUDE_SHARED_CONFIG:-/user-terminal-config}"
    [ -d "$shared" ] || return 0
    _share_path "$HOME/.claude"      "$shared/.claude"      dir
    _share_path "$HOME/.claude.json" "$shared/.claude.json" file
    # ~/.codex carries ONE OpenAI login (auth.json), the same class of
    # credential as ~/.claude — not a repo-scoped forge PAT, which stays
    # container-scoped by design (Invariant 2). Sharing it means signing in to
    # Codex once per host rather than once per devcontainer rebuild.
    #
    # Guarded on writability, unlike the two above: those predate this and
    # their behaviour is deliberately unchanged, but a shared store that is
    # mounted read-only (or owned by another uid) must not abort the whole
    # install under `set -e` just because codex could not be shared. Losing
    # the share costs a re-login after a rebuild; losing the install costs
    # the sandbox.
    if [ -w "$shared" ]; then
        _share_path "$HOME/.codex" "$shared/.codex" dir
        _share_path "$HOME/.pi" "$shared/.pi" dir
        # Shared user skills, bound rw into every agent's session (ADR 25).
        # Only the skills dir joins the share, not the rest of ~/.agents.
        mkdir -p "$HOME/.agents"
        _share_path "$HOME/.agents/skills" "$shared/.agents/skills" dir
    else
        echo "claude-sandbox: $shared is not writable; ~/.codex stays container-scoped (expect to sign in to codex again after a rebuild)." >&2
        echo "claude-sandbox: ~/.pi and ~/.agents/skills also stay container-scoped." >&2
    fi
}

# Runtime helpers and managed updater settings.
LIBEXEC="/usr/libexec/claude-sandbox"
BATTERY_PATH="$LIBEXEC/verify-sandbox-battery.sh"
VERSION_FILE_PATH="$LIBEXEC/version"
INSTALLER_FILE_PATH="$LIBEXEC/installer"
MANAGED_SETTINGS="/etc/claude-code/managed-settings.json"
CODEX_DIST_DIR="$LIBEXEC/codex-dist"
CODEX_REAL_PATH="$CODEX_DIST_DIR/bin/codex"
CODEX_MANAGED_CONFIG="/etc/codex/managed_config.toml"
CODEX_MARKER="# Managed by claude-sandbox — do not edit by hand."
USER_SL_CMD='bash $HOME/.claude/statusline-command.sh'

# --- The Python shadow: OPT-IN, PHASE 2 SCAFFOLDING (issue #72, ADR 26) ---
# CLAUDE_SANDBOX_IMPL=python installs the three-line shim (claude-shim) as the
# shadow at /usr/local/bin/{claude,codex,pi}, and a minimal root-owned
# interpreter and venv for it under /usr/libexec/claude-sandbox. Unset (or
# `bash`), the installer installs exactly what it always has, and removes any
# venv an earlier opt-in left. The choice is made here, by whoever runs the
# installer outside the jail: nothing reads it at launch, and /usr is
# read-only inside the jail, so a session can never flip it.
#
# Kept minimal on purpose; phase 4 replaces it with the Python installer
# (pinned patch release, pruning). What the phase 0 spike found is kept:
# uv's cache is never used (~/.cache is writable from the jail), the venv
# points at the resolved patch directory (readlink -f), uv's _virtualenv.pth
# goes, and everything is root-owned and byte-compiled. The package is copied
# from the clone's src/ (it is standard library only, so nothing is built).
PY_DIR="$LIBEXEC/python"
PY_VENV="$LIBEXEC/venv"
PY_VERSION="3.13"

shadow_source() {
    case "$IMPL" in
        bash) printf '%s\n' "$SCRIPT_DIR/claude-shadow" ;;
        python) printf '%s\n' "$SCRIPT_DIR/claude-shim" ;;
        *)
            echo "claude-sandbox: CLAUDE_SANDBOX_IMPL must be bash or python, got '$IMPL'." >&2
            exit 2
            ;;
    esac
}

# The prompt hook (ADR 27): every outer bash and zsh warns at the prompt when
# the Python shadow's PATH watcher has quarantined something. The file goes
# in /etc/profile.d (login shells) and is sourced from the system rc files
# (every interactive shell), between markers so a bash install removes it.
ALERTS_HOOK="/etc/profile.d/claude-sandbox-alerts.sh"
ALERTS_BEGIN="# >>> claude-sandbox alerts >>>"
ALERTS_END="# <<< claude-sandbox alerts <<<"

alerts_hook() {
    local rc
    for rc in /etc/bash.bashrc /etc/zsh/zshrc; do
        rc="$(prefixed "$rc")"
        [ -f "$rc" ] || continue
        if grep -qxF "$ALERTS_BEGIN" "$rc"; then
            sed -i "/^$ALERTS_BEGIN\$/,/^$ALERTS_END\$/d" "$rc"
        fi
        if [ "$IMPL" = python ]; then
            printf '%s\n%s\n%s\n' "$ALERTS_BEGIN" \
                "[ -r $ALERTS_HOOK ] && . $ALERTS_HOOK" "$ALERTS_END" >> "$rc"
        fi
    done
    if [ "$IMPL" = python ]; then
        install_file "$SCRIPT_DIR/alerts-prompt.sh" "$(prefixed "$ALERTS_HOOK")" 0644
    else
        rm -f "$(prefixed "$ALERTS_HOOK")"
    fi
}

provision_python_shadow() {
    local py_dir venv interp site
    py_dir="$(prefixed "$PY_DIR")"
    venv="$(prefixed "$PY_VENV")"
    alerts_hook
    if [ "$IMPL" != python ]; then
        rm -rf "$py_dir" "$venv"
        return 0
    fi
    # A download, like the agent binaries.
    if [ "$SMOKE" = "1" ]; then
        return 0
    fi
    if [ ! -f "$REPO_ROOT/src/claude_sandbox/shadow.py" ]; then
        echo "claude-sandbox: CLAUDE_SANDBOX_IMPL=python installs from a clone (./install --here); $REPO_ROOT has no src/claude_sandbox." >&2
        exit 1
    fi
    # ADR 26: no executable found through PATH. uv comes only from a fixed
    # system location, and only a root-owned copy nobody else can write.
    local uv="" candidate
    for candidate in /usr/bin/uv /usr/local/bin/uv; do
        if [ -f "$candidate" ] && [ -x "$candidate" ] \
                && [ "$(stat -c %u "$candidate")" = 0 ] \
                && [ -z "$(find "$candidate" -perm /022)" ]; then
            uv="$candidate"
            break
        fi
    done
    if [ -z "$uv" ]; then
        echo "claude-sandbox: CLAUDE_SANDBOX_IMPL=python needs a root-owned uv at /usr/bin/uv or /usr/local/bin/uv." >&2
        exit 1
    fi
    # An active venv or a UV_PYTHON request must not choose the interpreter.
    # --no-bin: write nothing outside $py_dir (uv would otherwise add a
    # python3.x launcher to the installing user's ~/.local/bin).
    env -u VIRTUAL_ENV -u UV_PYTHON UV_PYTHON_INSTALL_DIR="$py_dir" UV_NO_CACHE=1 \
        "$uv" python install --no-config --no-bin --quiet "$PY_VERSION"
    interp="$(env -u VIRTUAL_ENV -u UV_PYTHON UV_PYTHON_INSTALL_DIR="$py_dir" \
        "$uv" python find --no-config --no-project --managed-python "$PY_VERSION")"
    interp="$(readlink -f "$interp")"
    case "$interp" in
        "$py_dir"/*) ;;
        *)
            echo "claude-sandbox: uv found $interp, not an interpreter under $py_dir; refusing." >&2
            exit 1
            ;;
    esac
    if [ "$(readlink -f "$venv/bin/python" 2>/dev/null)" != "$interp" ]; then
        rm -rf "$venv"
        env -u VIRTUAL_ENV UV_NO_CACHE=1 \
            "$uv" venv --no-config --no-project --quiet --python "$interp" "$venv"
    fi
    site="$("$venv/bin/python" -I -c 'import sysconfig; print(sysconfig.get_path("purelib"))')"
    rm -f "$site/_virtualenv.pth" "$site/_virtualenv.py"
    rm -rf "$site/claude_sandbox"
    cp -R "$REPO_ROOT/src/claude_sandbox" "$site/claude_sandbox"
    find "$site/claude_sandbox" -name __pycache__ -prune -exec rm -rf {} +
    "$venv/bin/python" -I -m compileall -q --invalidation-mode checked-hash "$site/claude_sandbox"
    if [ "$(id -u)" = 0 ]; then
        chown -R 0:0 "$py_dir" "$venv"
    fi
    chmod -R u+rwX,go+rX,go-w "$py_dir" "$venv"
}

install_runtime_scripts() {
    install_file "$SCRIPT_DIR/codex-launch" "$(prefixed "$LIBEXEC/codex-launch")"
    install_file "$SCRIPT_DIR/verify-sandbox-battery.sh" "$(prefixed "$BATTERY_PATH")"
    # The recommended status line, kept where `claude-sandbox doctor --fix`
    # finds it after the install clone is deleted.
    install_file "$REPO_ROOT/.claude/statusline-command.sh" "$(prefixed "$LIBEXEC/statusline-command.sh")"
    install_file "$SCRIPT_DIR/pi-sandbox-tag.ts" "$(prefixed "$LIBEXEC/pi-sandbox-tag.ts")" 0644
}

# install_shipped_skills: place the repo's top-level skills/ tree (the skills
# every sandboxed agent gets, as opposed to .claude/skills/, which is for
# developing this repo) under /usr/libexec next to the runtime helpers. The
# shadow ro-binds each one into the agent's skills dir INSIDE the jail, so
# the user's shared ~/.claude is never written to and a compromised session
# cannot rewrite a skill's scripts. The tree is REPLACED, not merged: a skill
# removed from the repo must disappear on re-install, or a stale copy keeps
# shipping with no source to audit. Root-owned, world-readable, scripts
# executable (mode as checked in; the wheel preserves it).
SKILLS_LIBEXEC="$LIBEXEC/skills"
install_shipped_skills() {
    local src="$REPO_ROOT/skills" dst; dst="$(prefixed "$SKILLS_LIBEXEC")"
    rm -rf "$dst"
    [ -d "$src" ] || return 0
    mkdir -p "$dst"
    local skill
    for skill in "$src"/*/; do
        [ -f "$skill/SKILL.md" ] || continue
        cp -R "$skill" "$dst/"
    done
    chmod -R u=rwX,go=rX "$dst"
}

# Disable Claude updates while preserving any existing managed policy.
wire_managed_settings() {
    local settings; settings="$(prefixed "$MANAGED_SETTINGS")"
    mkdir -p "$(dirname "$settings")"

    if [ -f "$settings" ] && ! jq -e . "$settings" >/dev/null 2>&1; then
        cat >&2 <<EOF
claude-sandbox: WARNING — $settings is not valid JSON.
Skipping the updater settings. Set "env":{"DISABLE_AUTOUPDATER":"1"}
and "autoUpdates": false in the managed policy.
EOF
        return 0
    fi

    local input merged tmp
    if [ -f "$settings" ]; then input="$(cat "$settings")"; else input='{}'; fi

    merged="$(printf '%s' "$input" | jq '.env.DISABLE_AUTOUPDATER = "1" | .autoUpdates = false')"

    tmp="$(mktemp "$settings.XXXXXX")"
    printf '%s\n' "$merged" > "$tmp"
    chmod 0644 "$tmp"
    mv "$tmp" "$settings"
}

codex_managed_config_body() {
    cat <<TOML
$CODEX_MARKER
#
# Root-cause removal of the update-driven sandbox bypass: a Codex self-update
# re-creates ~/.local/bin/codex, which would then resolve ahead of the shadow.
# Upgrade the image or rebuild the devcontainer to update the agent.
check_for_update_on_startup = false
TOML
}

# install_owned_toml BODY_FN DEST LABEL: write a file this installer owns,
# byte-stably (so a re-run is a true no-op), refusing to clobber one it did
# not write.
install_owned_toml() {
    local body_fn="$1" dest="$2" label="$3" tmp
    mkdir -p "$(dirname "$dest")"
    if [ -f "$dest" ] && [ "$(head -n 1 "$dest")" != "$CODEX_MARKER" ]; then
        {
            echo "claude-sandbox: WARNING — $dest exists and was not written by us."
            echo "Leaving it untouched, so the $label is NOT active on this host."
            echo "To apply it, merge this in by hand:"
            echo
            "$body_fn" | tail -n +2
        } >&2
        return 0
    fi
    tmp="$(mktemp "$dest.XXXXXX")"
    "$body_fn" > "$tmp"
    chmod 0644 "$tmp"
    if [ -f "$dest" ] && cmp -s "$tmp" "$dest"; then
        rm -f "$tmp"
        return 0
    fi
    mv "$tmp" "$dest"
}

wire_codex_managed() {
    install_owned_toml codex_managed_config_body \
        "$(prefixed "$CODEX_MANAGED_CONFIG")" "codex updater settings"
}

# Seed the user's statusline when absent. Preserve existing preferences
# and hooks.
wire_user_statusline() {
    local settings="$USER_HOME/.claude/settings.json"
    mkdir -p "$(dirname "$settings")"

    local sl_src="$REPO_ROOT/.claude/statusline-command.sh"
    if [ -f "$sl_src" ]; then
        # Seed-only-if-absent by default (never stomp an owner's own);
        # STATUS=1 forces the clone's copy to win (content-compared).
        if [ "$FORCE_STATUSLINE" = "1" ]; then
            install_file "$sl_src" "$USER_HOME/.claude/statusline-command.sh"
        else
            install_file_if_absent "$sl_src" "$USER_HOME/.claude/statusline-command.sh"
        fi
    fi

    if [ -f "$settings" ] && ! jq -e . "$settings" >/dev/null 2>&1; then
        echo "claude-sandbox: WARNING — $settings is not valid JSON; skipping statusline wiring." >&2
        return 0
    fi

    local had_file=false input merged tmp sl_present=false
    [ -f "$settings" ] && had_file=true
    if [ "$had_file" = true ]; then input="$(cat "$settings")"; else input='{}'; fi
    if [ -f "$USER_HOME/.claude/statusline-command.sh" ]; then sl_present=true; fi

    merged="$(printf '%s' "$input" | jq --arg sl "$USER_SL_CMD" --argjson slp "$sl_present" '
        if ($slp and .statusLine == null) then .statusLine = {type:"command",command:$sl} else . end
    ')"

    # Don't create an empty {} settings on a fresh home that has no
    # statusline source to wire.
    if [ "$had_file" = false ] && printf '%s' "$merged" | jq -e '. == {}' >/dev/null 2>&1; then
        return 0
    fi

    tmp="$(mktemp "$settings.XXXXXX")"
    printf '%s\n' "$merged" > "$tmp"
    chmod 0644 "$tmp"
    mv "$tmp" "$settings"
}

main() {
    local image_build=0
    case "$*" in
        '') ;;
        --image-build) image_build=1 ;;
        *) echo 'Usage: install.sh [--image-build]' >&2; return 2 ;;
    esac
    probe_or_refuse
    local shadow
    shadow="$(shadow_source)"
    # The Python shadow's interpreter lands before its shim, so a failed
    # download leaves the previous shadow in place.
    provision_python_shadow
    # Shadow first: with /usr/local/bin/claude in place before the
    # official installer runs, any `claude` lookup during the rest of
    # install resolves (and bash-hashes) to the shadow path, even if
    # the shadow itself transiently fails because bwrap or the real
    # binary haven't landed yet.
    install_file "$shadow" "$(prefixed /usr/local/bin/claude)"
    # The SAME shadow under the other agent's name — it dispatches on argv[0].
    # Placed unconditionally, even when WITH_CODEX=0 or the download failed:
    # the shadow must own `codex` on $PATH before the vendor's installer can
    # claim it (Invariant 1). An unbacked shadow loud-fails with instructions;
    # an unshadowed vendor binary would silently run outside the jail.
    install_file "$shadow" "$(prefixed /usr/local/bin/codex)"
    install_file "$shadow" "$(prefixed /usr/local/bin/pi)"
    install_file "$SCRIPT_DIR/pi-run" "$(prefixed /usr/libexec/claude-sandbox/pi-run)"
    install_file "$SCRIPT_DIR/pi-system.md" "$(prefixed /usr/libexec/claude-sandbox/pi-system.md)" 0644
    # The helper CLI (gh-auth, glab-auth, update, verify, version) —
    # on PATH so it works after the install clone is deleted.
    install_file "$SCRIPT_DIR/claude-sandbox" "$(prefixed /usr/local/bin/claude-sandbox)"
    apt_install
    # Image builds have no runtime mounts and cannot validate the host kernel.
    # The container entrypoint performs these two steps at startup.
    if [ "$image_build" = 0 ]; then
        probe_userns_or_refuse
        link_terminal_config
    fi
    install_claude_binary
    install_codex_binary
    install_pi_binary
    ensure_cred_dirs
    install_conf
    stamp_version
    stamp_installer
    install_runtime_scripts
    install_shipped_skills
    wire_managed_settings
    wire_codex_managed
    wire_user_statusline

    echo "claude-sandbox: install complete."
    echo "  shadow:      $(prefixed /usr/local/bin/claude), $(prefixed /usr/local/bin/codex), $(prefixed /usr/local/bin/pi)"
    if [ "$IMPL" = python ]; then
        echo "  python:      the Python shadow (opt-in), interpreter in $(prefixed "$PY_VENV")"
    fi
    echo "  real pi:     $(prefixed /usr/libexec/claude-sandbox/pi-dist/pi) $([ -x "$(prefixed /usr/libexec/claude-sandbox/pi-dist/pi)" ] && echo 'installed (standalone, ro in sandbox)' || echo 'NOT installed — pi will refuse to launch')"
    echo "  cli:         $(prefixed /usr/local/bin/claude-sandbox) ($(cat "$(prefixed "$VERSION_FILE_PATH")"))"
    echo "  real claude: $(prefixed /usr/libexec/claude-sandbox/claude)"
    echo "  real codex:  $(prefixed "$CODEX_REAL_PATH") $([ -x "$(prefixed "$CODEX_REAL_PATH")" ] && echo 'installed (whole package, ro in sandbox)' || echo 'NOT installed — `codex` will refuse to launch')"
    echo "  config:      $(prefixed /etc/claude-sandbox.conf)"
    echo "  battery:     $(prefixed "$BATTERY_PATH") (off-PATH, ro in sandbox; /verify-sandbox phase 1)"
    echo "  skills:      $(prefixed "$SKILLS_LIBEXEC") ($(ls "$(prefixed "$SKILLS_LIBEXEC")" 2>/dev/null | wc -l) shipped; ro-bound into each agent's skills dir in-session)"
    echo "  managed:     $(prefixed "$MANAGED_SETTINGS") (updater disabled)"
    echo "  codex conf:  $(prefixed "$CODEX_MANAGED_CONFIG") (updater settings)"
    echo "  statusline:  $USER_HOME/.claude/settings.json (preference only)"
    echo "  run \`claude-sandbox verify\` for the live battery, or use the shipped verify-sandbox skill for the full audit."


}

# The container entrypoint and tests source these functions without installing.
if [ "${BASH_SOURCE[0]}" = "$0" ]; then
    main "$@"
fi
