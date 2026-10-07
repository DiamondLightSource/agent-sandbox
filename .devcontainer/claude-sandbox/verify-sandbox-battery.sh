#!/usr/bin/env bash
# verify-sandbox phase-1 battery — the 22 deterministic PASS/FAIL checks
# behind the /verify-sandbox command (skills/verify-sandbox/SKILL.md).
#
# WHY THIS IS A COMMITTED SCRIPT, NOT INLINE IN THE COMMAND MARKDOWN:
#   - Slash-command loading substitutes $1..$9 as POSITIONAL ARGUMENTS.
#     The awk field references the checks depend on ($1, $5, $9 in
#     /proc/self/status and mountinfo parsing) were silently blanked to
#     empty when the snippets were injected from the .md, so checks
#     07/10/17/20 false-failed on awk syntax errors before any shell ran.
#     A file on disk is read straight by bash, so $1..$9 stay real.
#   - The interactive shell varies (zsh sets `nomatch`, which turns an
#     unmatched glob like /tmp/vscode-ipc-*.sock into a hard parse error
#     instead of bash's pass-through). The shebang pins bash, killing the
#     portability gap.
#   - Single source of truth: the command markdown documents the WHY of
#     each check; this script is the WHAT that actually runs.
#
# WHERE IT LIVES: installed to /usr/libexec/claude-sandbox (off the
# user's PATH, root-owned, ro-bound inside the sandbox via `--ro-bind /
# /`) — alongside the relocated real binary. A compromised in-session
# Claude (workspace is rw) therefore cannot rewrite the verifier to print
# PASS for a broken sandbox.
#
# CONTRACT: prints `/verify-sandbox: 22 checks`, one [PASS]/[FAIL] line
# per check, then `Summary: N PASS / M FAIL`, and exits with the FAIL
# count (0 == all green). The /verify-sandbox command runs this for
# phase 1; on a clean exit it proceeds to the open-ended phase-2
# adversarial probes, otherwise it reports the failure and stops.
#
# NOTE: deliberately NOT `set -e` — checks are expected to return
# non-zero and the runner must keep going to print the full table.
set -uo pipefail

PASS=0
FAIL=0

# result NUM NAME RC [DETAIL] — tally + print one table line. DETAIL is a
# failure reason on FAIL, or an informational note on PASS (used by the
# egress-jail checks to mark a legitimately-disabled jail).
result() {
    local num="$1" name="$2" rc="$3" detail="${4:-}"
    if [ "$rc" -eq 0 ]; then
        PASS=$((PASS + 1))
        if [ -n "$detail" ]; then
            printf '  [PASS] %s %s — %s\n' "$num" "$name" "$detail"
        else
            printf '  [PASS] %s %s\n' "$num" "$name"
        fi
    else
        FAIL=$((FAIL + 1))
        if [ -n "$detail" ]; then
            printf '  [FAIL] %s %s — %s\n' "$num" "$name" "$detail"
        else
            printf '  [FAIL] %s %s\n' "$num" "$name"
        fi
    fi
}

# mount_fstype MOUNTPOINT — print MOUNTPOINT's fs type from
# /proc/self/mountinfo. The fs type is the token AFTER the single "-"
# separator; the optional fields before it (field 7+) vary in count, so a
# fixed column ($9) is unstable across hosts. Parse by the separator.
mount_fstype() {
    awk -v m="$1" '$5 == m {
        for (i = 7; i <= NF; i++) if ($i == "-") { print $(i + 1); exit }
    }' /proc/self/mountinfo
}

echo "/verify-sandbox: 22 checks"

# 01 — IS_SANDBOX sentinel. Only `bwrap --setenv` sets it; unset means
# Claude ran against the real binary, bypassing the sandbox entirely.
if [ "${IS_SANDBOX:-}" = "1" ]; then
    result 01 "IS_SANDBOX sentinel set" 0
else
    result 01 "IS_SANDBOX sentinel set" 1 "IS_SANDBOX != 1 (running outside the bwrap shadow)"
fi

# 02 — NO_NEW_PRIVS: setuid binaries inside cannot gain privileges.
if grep -q '^NoNewPrivs:[[:space:]]*1$' /proc/self/status; then
    result 02 "NO_NEW_PRIVS: setuid escalation blocked" 0
else
    result 02 "NO_NEW_PRIVS: setuid escalation blocked" 1 "NoNewPrivs is not 1 in /proc/self/status"
fi

# 03 — strict-under-/root inversion: only the allow-listed binds/masks
# may appear under $HOME, and only gh/glab-cli under $HOME/.config.
check_03() {
    local extras config_extras agent own
    # The sandbox wraps more than one agent, and each session binds only its
    # OWN config dir. So the expected set depends on whose session this is —
    # and listing only that agent's dir makes the check STRONGER, not weaker:
    # a `.claude` appearing inside a codex session (or the reverse) is a
    # cross-agent credential leak, and now fails here.
    # IS_SANDBOX_AGENT is set by the shadow; default to claude so a session
    # launched by an older shadow still evaluates as it always did.
    agent="${IS_SANDBOX_AGENT:-claude}"
    case "$agent" in
        codex) own='\.codex' ;;
        pi)    own='\.pi' ;;
        *)     own='\.claude|\.claude\.json' ;;
    esac
    # .agents is allowed for every agent, but only as the parent of the
    # shared skills bind (ADR 25): anything else under it is a leak.
    extras="$(ls -A "$HOME" 2>/dev/null \
        | grep -vxE "$own"'|\.agents|\.cache|\.config|\.local|\.gitconfig|\.netrc|\.Xauthority|\.ICEauthority' \
        || true)"
    if [ -n "$extras" ]; then
        EXTRA_DETAIL="unexpected \$HOME entries: $(printf '%s' "$extras" | tr '\n' ' ')"
        return 1
    fi
    if [ -d "$HOME/.agents" ]; then
        extras="$(ls -A "$HOME/.agents" 2>/dev/null | grep -vx 'skills' || true)"
        if [ -n "$extras" ]; then
            EXTRA_DETAIL="unexpected \$HOME/.agents entries: $(printf '%s' "$extras" | tr '\n' ' ')"
            return 1
        fi
    fi
    if [ -d "$HOME/.config" ]; then
        config_extras="$(ls -A "$HOME/.config" 2>/dev/null | grep -vxE 'gh|glab-cli' || true)"
        if [ -n "$config_extras" ]; then
            EXTRA_DETAIL="unexpected \$HOME/.config entries: $(printf '%s' "$config_extras" | tr '\n' ' ')"
            return 1
        fi
    fi
    return 0
}
EXTRA_DETAIL=""
if check_03; then
    result 03 "strict-under-/root: only ${IS_SANDBOX_AGENT:-claude}'s own config (+.cache/.local) under \$HOME" 0
else
    result 03 "strict-under-/root: only ${IS_SANDBOX_AGENT:-claude}'s own config (+.cache/.local) under \$HOME" 1 "$EXTRA_DETAIL"
fi

# 04 — env scrub: host API/forge credentials must not survive --clearenv.
# OPENAI_API_KEY is asserted for BOTH agents, not just codex: an OpenAI key on
# the host is a credential the jail must not hand to any session, whichever
# agent is running.
scrub_leaks=""
[ -z "${GH_TOKEN:-}" ]        || scrub_leaks="$scrub_leaks GH_TOKEN"
[ -z "${OPENAI_API_KEY:-}" ]  || scrub_leaks="$scrub_leaks OPENAI_API_KEY"
if [ -z "$scrub_leaks" ]; then
    result 04 "env scrub: GH_TOKEN / OPENAI_API_KEY empty" 0
else
    result 04 "env scrub: GH_TOKEN / OPENAI_API_KEY empty" 1 "leaked into the sandbox:$scrub_leaks"
fi

# 05 — env scrub: DISPLAY (X11 reachability path) must be empty.
if [ -z "${DISPLAY:-}" ]; then
    result 05 "env scrub: DISPLAY empty" 0
else
    result 05 "env scrub: DISPLAY empty" 1 "DISPLAY leaked into the sandbox"
fi

# 06 — cap_drop ALL: effective capability set is all zeros.
if grep -q '^CapEff:[[:space:]]*0\{16\}$' /proc/self/status; then
    result 06 "cap_drop ALL: CapEff=0000000000000000" 0
else
    result 06 "cap_drop ALL: CapEff=0000000000000000" 1 "CapEff is non-zero in /proc/self/status"
fi

# 07 — --unshare-pid: NSpid lists our PID per pidns level; nested == >= 2.
nspid_count="$(awk '$1=="NSpid:"{print NF-1; exit}' /proc/self/status)"
if [ "${nspid_count:-1}" -ge 2 ]; then
    result 07 "--unshare-pid: NSpid has >= 2 entries (kernel pidns isolated)" 0
else
    result 07 "--unshare-pid: NSpid has >= 2 entries (kernel pidns isolated)" 1 "NSpid has ${nspid_count:-1} entry (not in a nested pidns)"
fi

# 08 — --unshare-ipc: ipcns symlink present and well-formed.
ipc_link="$(readlink /proc/self/ns/ipc 2>/dev/null || true)"
case "$ipc_link" in
    ipc:\[*\]) result 08 "--unshare-ipc: ipcns symlink present" 0 ;;
    *)         result 08 "--unshare-ipc: ipcns symlink present" 1 "/proc/self/ns/ipc is '$ipc_link'" ;;
esac

# 09 — --unshare-uts: utsns symlink present and well-formed.
uts_link="$(readlink /proc/self/ns/uts 2>/dev/null || true)"
case "$uts_link" in
    uts:\[*\]) result 09 "--unshare-uts: utsns symlink present" 0 ;;
    *)         result 09 "--unshare-uts: utsns symlink present" 1 "/proc/self/ns/uts is '$uts_link'" ;;
esac

# 10 — private /dev: a fresh tmpfs/devtmpfs (not a bind of the host /dev).
dev_fstype="$(mount_fstype /dev)"
if printf '%s\n' "$dev_fstype" | grep -qE '^(tmpfs|devtmpfs)$'; then
    result 10 "private /dev: fresh tmpfs (TIOCSTI blocked)" 0
else
    result 10 "private /dev: fresh tmpfs (TIOCSTI blocked)" 1 "/dev fstype is '${dev_fstype:-none}' (expected tmpfs/devtmpfs)"
fi

# 11 — /tmp tmpfs (always --tmpfs): VS Code IPC/git sockets masked. In bash
# an unmatched glob passes through literally and `ls` fails, so `!` PASSes.
tmp_fstype="$(mount_fstype /tmp)"
if [ "$tmp_fstype" = "tmpfs" ] \
        && ! ls /tmp/vscode-ipc-*.sock /tmp/vscode-git-*.sock >/dev/null 2>&1; then
    result 11 "/tmp tmpfs: no vscode-ipc-*.sock visible" 0
else
    result 11 "/tmp tmpfs: no vscode-ipc-*.sock visible" 1 "/tmp fstype='${tmp_fstype:-none}' or a vscode-ipc/git socket is visible"
fi

# 12 — /run/user masked: no host DBus/IPC bridges. The launcher only
# --tmpfs-masks this when the host has /run/user, so absent ⇒ nothing to
# mask (OK); a non-tmpfs *mount* present-but-empty must still FAIL.
run_user_fstype="$(mount_fstype /run/user)"
if [ "${run_user_fstype:-tmpfs}" = "tmpfs" ] && [ -z "$(ls -A /run/user 2>/dev/null)" ]; then
    result 12 "/run/user empty" 0
else
    result 12 "/run/user empty" 1 "/run/user fstype='${run_user_fstype:-none}' or non-empty"
fi

# 13 — /run/secrets masked (Docker/Compose secrets). Same tmpfs-or-absent
# rule as /run/user (launcher --tmpfs-masks only when the host has it).
run_secrets_fstype="$(mount_fstype /run/secrets)"
if [ "${run_secrets_fstype:-tmpfs}" = "tmpfs" ] && [ -z "$(ls -A /run/secrets 2>/dev/null)" ]; then
    result 13 "/run/secrets empty (Docker/Compose secrets masked)" 0
else
    result 13 "/run/secrets empty (Docker/Compose secrets masked)" 1 "/run/secrets fstype='${run_secrets_fstype:-none}' or non-empty"
fi

# 14 — file mask: $HOME/.netrc bound to /dev/null (size zero).
if [ ! -s "$HOME/.netrc" ]; then
    result 14 "file mask: \$HOME/.netrc is empty" 0
else
    result 14 "file mask: \$HOME/.netrc is empty" 1 "\$HOME/.netrc is non-empty (host credentials reachable)"
fi

# 15 — file mask: $HOME/.Xauthority bound to /dev/null (size zero).
if [ ! -s "$HOME/.Xauthority" ]; then
    result 15 "file mask: \$HOME/.Xauthority is empty" 0
else
    result 15 "file mask: \$HOME/.Xauthority is empty" 1 "\$HOME/.Xauthority is non-empty (X11 cookie reachable)"
fi

# 16 — curated gitconfig in effect: GIT_CONFIG_GLOBAL pinned + email set.
if [ "${GIT_CONFIG_GLOBAL:-}" = "/etc/claude-gitconfig" ] \
        && [ -n "$(git config --get user.email 2>/dev/null)" ]; then
    result 16 "curated gitconfig: GIT_CONFIG_GLOBAL set, user.email present" 0
else
    result 16 "curated gitconfig: GIT_CONFIG_GLOBAL set, user.email present" 1 "GIT_CONFIG_GLOBAL='${GIT_CONFIG_GLOBAL:-}' or user.email unset"
fi

# 17 — workspace scoped to $PWD, not the broad /workspaces bind. Only
# meaningful when /workspaces exists and $PWD is a proper subdir. Parse
# mountinfo field 6 (per-mount opts) of the LAST matching /workspaces
# line and require an EXACT "rw" token — never the superblock opts after
# the "-" separator, which routinely end in "rw" even for a ro bind.
check_17() {
    [ -d /workspaces ] || return 0
    [ "$PWD" != /workspaces ] || return 0
    if awk '$5=="/workspaces"{opts=$6}
            END{if(opts==""){exit 1}
                n=split(opts,o,","); for(i=1;i<=n;i++) if(o[i]=="rw") exit 0
                exit 1}' /proc/self/mountinfo; then
        # /workspaces is rw-bound — only allowed with the explicit opt-in.
        [ "${CLAUDE_SANDBOX_WORKSPACE_ROOT:-}" = "/workspaces" ] || return 1
    fi
    return 0
}
if check_17; then
    result 17 "workspace scoped to \$PWD (not broad /workspaces)" 0
else
    result 17 "workspace scoped to \$PWD (not broad /workspaces)" 1 "/workspaces is rw-bound without CLAUDE_SANDBOX_WORKSPACE_ROOT opt-in"
fi

# 18 — config read from /etc, not the rw workspace. Inspect the installed
# shadow (visible ro via --ro-bind / /): a shim that must exec the
# root-owned interpreter with -I. The package in that interpreter's venv
# must pin CONFIG_PATH to /etc and feed it to parse_config, with no
# parse_config reading .devcontainer.
check_18() {
    local shadow pkg
    shadow="$(command -v claude || true)"
    [ -n "$shadow" ] || { EXTRA_DETAIL="no claude shadow found on PATH"; return 1; }
    if ! grep -qxF 'exec /usr/libexec/claude-sandbox/venv/bin/python -I -m claude_sandbox _shadow "${0##*/}" -- "$@"' "$shadow"; then
        EXTRA_DETAIL="$shadow is not the shim that runs the root-owned interpreter with -I"
        return 1
    fi
    for pkg in /usr/libexec/claude-sandbox/venv/lib/python3*/site-packages/claude_sandbox; do
        [ -f "$pkg/shadow.py" ] || continue
        grep -qxF 'CONFIG_PATH = "/etc/claude-sandbox.conf"' "$pkg/config.py" \
            && grep -qF 'config_path: str = CONFIG_PATH' "$pkg/shadow.py" \
            && grep -qF 'parse_config(host.config_path' "$pkg/shadow.py" \
            && ! grep -q 'parse_config(.*\.devcontainer' "$pkg/shadow.py" "$pkg/config.py"
        return
    done
    EXTRA_DETAIL="the Python shadow's package is missing from its venv"
    return 1
}
EXTRA_DETAIL=""
if check_18; then
    result 18 "config read from /etc/claude-sandbox.conf (no \$PWD/.devcontainer read)" 0
else
    result 18 "config read from /etc/claude-sandbox.conf (no \$PWD/.devcontainer read)" 1 "${EXTRA_DETAIL:-the shadow does not pin CONFIG_PATH to /etc}"
fi

# 19 — egress jail active (or deliberately disabled). Blackhole routes
# appear only when netns_holder programmed them, so their presence is the
# active marker; absence == jail disabled (opt-out, not a failure).
routes="$(ip route show 2>/dev/null || true)"
if grep -qE '^blackhole (10\.0\.0\.0/8|172\.16\.0\.0/12|192\.168\.0\.0/16)' <<<"$routes"; then
    if grep -qE '^blackhole 10\.0\.0\.0/8'    <<<"$routes" \
            && grep -qE '^blackhole 172\.16\.0\.0/12' <<<"$routes" \
            && grep -qE '^blackhole 192\.168\.0\.0/16' <<<"$routes" \
            && grep -qE '^blackhole 100\.64\.0\.0/10'  <<<"$routes" \
            && grep -qE '^default '                    <<<"$routes"; then
        result 19 "egress jail active: RFC1918 blackholed in netns (or disabled)" 0
    else
        result 19 "egress jail active: RFC1918 blackholed in netns (or disabled)" 1 "jail active but RFC1918/CGNAT/default set incomplete (partial programming)"
    fi
else
    result 19 "egress jail active: RFC1918 blackholed in netns (or disabled)" 0 "jail not active (disabled)"
fi

# 20 — behavioural counterpart: representative non-allow-listed RFC1918
# (+ CGNAT + a connected-subnet base) must be unreachable while the
# gateway stays routable. Disabled jail == nothing to assert (PASS).
check_20() {
    local blocked=1 ip subnet gw
    local probes=(10.255.255.254 172.31.255.254 192.168.255.254 100.127.255.254)
    # A probe counts as "reachable" only when `ip route get` yields a
    # FORWARDING route (plain unicast toward a peer/gateway). Non-forwarding
    # results — blackhole / unreachable / prohibit / broadcast / local /
    # multicast / throw — are NOT lateral egress and must not count. In
    # particular, a connected subnet's network base address carries a kernel
    # `broadcast` route in local table 255, so `ip route get <base>` exits 0
    # even while every real host in the subnet is blackholed; testing exit
    # status alone false-"resolved" the base and masked a working blackhole.
    route_forwardable() {
        local out
        out="$(ip route get "$1" 2>/dev/null)" || return 1
        case "$out" in
            blackhole*|unreachable*|prohibit*|broadcast*|local*|multicast*|throw*) return 1 ;;
            *) return 0 ;;
        esac
    }
    # Connected subnet (host LAN): a blackhole line that is NOT one of the
    # generic ranges the jail always programs. Probe its network base.
    subnet="$(awk '/^blackhole /{print $2}' <<<"$routes" \
        | grep -vxE '10\.0\.0\.0/8|172\.16\.0\.0/12|192\.168\.0\.0/16|100\.64\.0\.0/10|169\.254\.0\.0/16' \
        | head -n1)"
    [ -n "$subnet" ] && probes+=("${subnet%/*}")
    for ip in "${probes[@]}"; do
        route_forwardable "$ip" && blocked=0
    done
    gw="$(ip route show default 2>/dev/null | awk 'NR==1{for(i=1;i<=NF;i++) if($i=="via"){print $(i+1); exit}}')"
    [ -n "$gw" ] && route_forwardable "$gw" && [ "$blocked" = 1 ]
}
if grep -qE '^blackhole (10\.0\.0\.0/8|172\.16\.0\.0/12|192\.168\.0\.0/16)' <<<"$routes"; then
    if check_20; then
        result 20 "RFC1918 lateral egress unreachable, gateway still routable (or disabled)" 0
    else
        result 20 "RFC1918 lateral egress unreachable, gateway still routable (or disabled)" 1 "a non-allow-listed RFC1918 dest resolved, or the gateway is unreachable"
    fi
else
    result 20 "RFC1918 lateral egress unreachable, gateway still routable (or disabled)" 0 "jail not active (disabled)"
fi

# 21 — agent binary mask: the vendor unpacks Codex's own binary under
# $CODEX_HOME/packages, i.e. INSIDE the ~/.codex we bind read-write. A writable
# copy of the agent's binary in its own session is a persistence foothold — a
# compromised codex rewrites it and is re-executed next launch, entirely
# bypassing the read-only /usr/libexec copy we actually exec. The shadow
# tmpfs-masks it; this asserts the mask is really there, because check 03 only
# inspects $HOME's TOP level and .codex is legitimately allow-listed.
#
# Claude sessions have nothing to assert here (its equivalent cache,
# ~/.local/share/claude, is masked by an unconditional --tmpfs the argv tests
# already cover), so they PASS with a note rather than skewing the count.
check_21() {
    local pkgs="$HOME/.codex/packages"
    # A mask is a tmpfs mounted ON that path. Absent directory is equally fine:
    # nothing writable is exposed either way.
    [ -e "$pkgs" ] || return 0
    mountpoint -q "$pkgs" 2>/dev/null || return 1
    # Masked AND empty — a non-empty tmpfs would mean something in-session
    # already wrote a binary tree there.
    [ -z "$(ls -A "$pkgs" 2>/dev/null)" ]
}
if [ "${IS_SANDBOX_AGENT:-claude}" = "codex" ]; then
    if check_21; then
        result 21 "agent binary mask: ~/.codex/packages is an empty tmpfs" 0
    else
        result 21 "agent binary mask: ~/.codex/packages is an empty tmpfs" 1 "the vendor's writable package tree is visible in-session (persistence foothold)"
    fi
else
    result 21 "agent binary mask: ~/.codex/packages is an empty tmpfs" 0 "not a codex session"
fi

# 22 — entry-point guard. Protect the sandbox's entry-point names (Invariant
# 1): a session cannot create a command named claude, codex, pi or
# claude-sandbox in a writable directory that precedes the shadow on the
# launching PATH. The shadow lists the
# directories it guarded in CLAUDE_SANDBOX_ENTRY_GUARD (the jail's own PATH
# is not the launching one); each name in each must be a read-only mount
# point (so it cannot be replaced, renamed or removed) that is not
# executable. Unset, there is nothing to check: no writable directory
# precedes the shadow.
check_22() {
    local dir name path dirs=()
    IFS=: read -r -a dirs <<<"$CLAUDE_SANDBOX_ENTRY_GUARD"
    [ "${#dirs[@]}" -gt 0 ] || { EXTRA_DETAIL="CLAUDE_SANDBOX_ENTRY_GUARD lists no directory"; return 1; }
    for dir in "${dirs[@]}"; do
        for name in claude codex pi claude-sandbox; do
            path="$dir/$name"
            if [ -L "$path" ] || [ -d "$path" ] || [ -x "$path" ]; then
                EXTRA_DETAIL="$path could run in place of the shadow"
                return 1
            fi
            # Read-only by the mount's own options: `test -w` cannot tell,
            # since a device node stays writable on a read-only mount.
            case ",$(awk -v m="$path" '$5 == m { print $6; exit }' /proc/self/mountinfo)," in
                *,ro,*) ;;
                *)
                    EXTRA_DETAIL="$path is not a read-only mount point (a session could create it)"
                    return 1
                    ;;
            esac
        done
    done
    return 0
}
EXTRA_DETAIL=""
if [ -z "${CLAUDE_SANDBOX_ENTRY_GUARD:-}" ]; then
    result 22 "entry-point names guarded in writable PATH dirs ahead of the shadow" 0 "no guarded directories"
elif check_22; then
    result 22 "entry-point names guarded in writable PATH dirs ahead of the shadow" 0
else
    result 22 "entry-point names guarded in writable PATH dirs ahead of the shadow" 1 "$EXTRA_DETAIL"
fi

echo "  Summary: $PASS PASS / $FAIL FAIL"
exit "$FAIL"
