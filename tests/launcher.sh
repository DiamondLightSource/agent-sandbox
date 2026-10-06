#!/usr/bin/env bash
# Launcher argv tests for container/claude-container — the verbs, the
# host-network default, the in-container refusal
# and the uvx-aware update hint. Drives the REAL launcher against a fake
# container engine on PATH that logs every call and answers the inspect
# queries the launcher makes; no image, no container, no network.
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
source "$HERE/lib.sh"
# CLAUDE_SANDBOX_TEST_LAUNCHER runs the suite against another launcher, a
# bash file (tests/python/test_bash_suites.py points it at the Python CLI).
LAUNCHER="${CLAUDE_SANDBOX_TEST_LAUNCHER:-$HERE/../container/claude-container}"

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
mkdir -p "$TMP/bin" "$TMP/project"
LOG="$TMP/engine.log"

# The fake engine. Existence of the container is a marker file created by
# `create`; the image label is FAKE_IMG_VER (empty = unlabelled).
cat > "$TMP/bin/podman" <<'FAKE'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "$LOG"
case "$*" in
    "container inspect -f {{.State.Running}} "*) case "$*" in *running*) echo true ;; *) [ -e "$MARK" ] && echo true ;; esac ;;
    "container inspect -f {{join .Config.Cmd \" \"}} "*) echo 'bash -c trap "exit 0" TERM INT; while :; do sleep 60 & wait $!; done' ;;
    "container inspect -f {{.Created}} "*) echo 2026-09-13T00:00:00 ;;
    "container inspect -f {{.Image}} "*) echo img1 ;;
    "container inspect -f {{range .Mounts}}{{println .Destination}}{{end}} "*) printf '%s' "${FAKE_MOUNTS:-}" ;;
    "container inspect -f {{len .ExecIDs}} "*) echo 0 ;;
    "container inspect claude-sandbox-"*) case "$*" in *"$(cat "$PS" 2>/dev/null | head -1)"*) [ -s "$PS" ] ;; *) [ -e "$MARK" ] ;; esac ;;
    "container inspect "*) [ -e "$MARK" ] ;;
    "image inspect -f {{index .Config.Labels \"io.diamondlightsource.claude-sandbox.launcher-version\"}} "*)
        [ -n "${FAKE_IMG_VER:-}" ] && echo "$FAKE_IMG_VER" ;;
    "image inspect -f {{index .Config.Labels \"org.opencontainers.image.revision\"}} "*) echo abc123 ;;
    "image inspect -f {{.Id}} "*) echo img1 ;;
    "start "*) touch "$MARK" ;;
    "create "*)
        touch "$MARK"
        for arg in "$@"; do
            case "$arg" in
                CLAUDE_SANDBOX_ALLOW_WRITE=*) printf '%s' "${arg#*=}" > "$LOG.mount-env" ;;
                CLAUDE_SANDBOX_ALLOW_DEVICES=*) printf '%s' "${arg#*=}" > "$LOG.device-env" ;;
            esac
        done
        ;;
    "run --rm --entrypoint find -v "*" /cache/venv-for "*) cat "$VENVS" 2>/dev/null ;;
    "ps -a --filter name=^claude-sandbox- --format {{.Names}}") cat "$PS" 2>/dev/null ;;
    "images --filter reference=*/diamondlightsource/claude-sandbox --format {{.Repository}}:{{.Tag}}") cat "$IMAGES" 2>/dev/null ;;
    "rmi "*) [ "${2:-}" != "in-use:1" ] ;;
    *) : ;;
esac
FAKE
chmod +x "$TMP/bin/podman"
ln -s podman "$TMP/bin/docker"

# run [ENV=VAL ...] -- ARGS...: fresh engine state, from the project dir.
run() {
    local -a envs=()
    while [ "$1" != "--" ]; do envs+=( "$1" ); shift; done; shift
    : > "$LOG"; rm -f "$TMP/mark" "$LOG.mount-env" "$LOG.device-env"
    ( cd "${PROJECT:-$TMP/project}" && env -i PATH="$TMP/bin:/usr/bin:/bin" HOME="$TMP" \
        LOG="$LOG" MARK="$TMP/mark" PS="$TMP/ps" IMAGES="$TMP/images" VENVS="$TMP/venvs" CLAUDE_SANDBOX_NESTED=1 "${envs[@]}" \
        bash "$LAUNCHER" "$@" 2>"$TMP/err" ); RC=$?
    ERR="$(cat "$TMP/err")"
}
exec_line() { grep '^exec -it ' "$LOG" | head -1; }
create_line() { grep '^create ' "$LOG" | head -1; }

# --- version prints the project version -----------------------------------
out="$(bash "$LAUNCHER" --version)"
ver="$(sed -n 's/^VERSION="\(.*\)"$/\1/p' "$LAUNCHER")"
[ "$out" = "claude-sandbox $ver" ] && pass || fail "--version printed '$out'"

# --- verbs -----------------------------------------------------------------
run --; assert_contains "default verb is claude" "$(exec_line)" "exec -it claude-sandbox-project-$(printf '%s' "$TMP/project" | cksum | awk '{print $1}') claude"
run -- pi -p hi;   case "$(exec_line)" in *" pi -p hi") pass ;; *) fail "pi verb: $(exec_line)" ;; esac
run -- codex;      case "$(exec_line)" in *" codex") pass ;; *) fail "codex verb: $(exec_line)" ;; esac
run -- shell;      case "$(exec_line)" in *"exec bash \"\$@\" _ bash") pass ;; *) fail "shell verb default without host SHELL: $(exec_line)" ;; esac
# The ancestor walk finds this harness (bash) where /proc is readable; inside the
# sandbox host /proc is bound with foreign PIDs, so it falls back to SHELL (zsh).
run SHELL=/usr/bin/zsh -- shell;            case "$(exec_line)" in *" _ bash"|*" _ zsh") pass ;; *) fail "shell verb default: $(exec_line)" ;; esac
run SHELL=/usr/bin/zsh CLAUDE_SANDBOX_SHELL=fish -- shell -c ls; case "$(exec_line)" in *" _ fish -c ls") pass ;; *) fail "CLAUDE_SANDBOX_SHELL override: $(exec_line)" ;; esac
run CLAUDE_SANDBOX_SHELL=fish --; assert_not_contains "shell choice is not baked at create" "$(create_line)" "-e CLAUDE_SANDBOX_SHELL=fish"
run -- --resume;   case "$(exec_line)" in *" claude --resume") pass ;; *) fail "agent args without verb: $(exec_line)" ;; esac
run -- version;    case "$(exec_line)" in *" claude-sandbox version") pass ;; *) fail "version verb not forwarded: $(exec_line)" ;; esac
run -- gh-auth;    case "$(exec_line)" in *" claude-sandbox gh-auth") pass ;; *) fail "gh-auth verb not forwarded: $(exec_line)" ;; esac
run -- verify --agent pi; case "$(exec_line)" in *" claude-sandbox verify --agent pi") pass ;; *) fail "verify args not forwarded: $(exec_line)" ;; esac
run -- doctor --fix; case "$(exec_line)" in *" claude-sandbox doctor --fix") pass ;; *) fail "doctor verb not forwarded: $(exec_line)" ;; esac

# --- the container tag: short slug plus 4 hex digits of the path hash ------
hash="$(printf '%s' "$TMP/project" | cksum | awk '{print $1}')"
run --; assert_contains "tag passed at create" "$(create_line | tr ' ' '\n')" \
    "CLAUDE_SANDBOX_TAG=project-$(printf '%04x' $((hash % 65536)))"
run CLAUDE_SANDBOX_TAG=forged --; assert_not_contains "host tag variable not passed through" \
    "$(create_line | tr ' ' '\n')" "CLAUDE_SANDBOX_TAG=forged"
mkdir -p "$TMP/a-very-long-project-directory-name"
PROJECT="$TMP/a-very-long-project-directory-name" run --
case "$(create_line)" in *"CLAUDE_SANDBOX_TAG=a-very-long-project--"[0-9a-f][0-9a-f][0-9a-f][0-9a-f]" "*) pass ;; *) fail "long slug not cut to 20: $(create_line)" ;; esac

# --- host networking is the default; --bridge opts out (create-time) -------
touch "$TMP/.gitconfig"
run --
case "$(create_line)" in
    *"$TMP/.gitconfig:/root/.gitconfig-host:ro"*) pass ;;
    *) fail "host git config is not mounted separately read-only" ;;
esac
assert_not_contains "container git config remains writable" "$(create_line)" ":/root/.gitconfig:ro"

run --; case "$(create_line)" in *"--network=host"*) pass ;; *) fail "host net not default: $(create_line)" ;; esac
run -- --bridge; case "$(create_line)" in *"--network=host"*) fail "--bridge still host net" ;; *) pass ;; esac

# --- filesystem view: parent rw, project rw, --mount ro, --mount-rw ---------
run --
assert_parse 'no GPU by default' grep -Fvq 'nvidia.com/gpu' <<< "$(create_line)"
assert_parse 'no device opt-in by default' grep -Fvq 'CLAUDE_SANDBOX_ALLOW_DEVICES=' <<< "$(create_line)"
run -- --gpu
assert_parse 'Podman GPU uses CDI' grep -Fq -- '--device nvidia.com/gpu=all' <<< "$(create_line)"
assert_parse 'GPU reaches shadow' grep -Fq -- '-e CLAUDE_SANDBOX_GPU=1' <<< "$(create_line)"
assert_parse 'GPU flag consumed' grep -Fvq -- '--gpu' <<< "$(exec_line)"
run CLAUDE_SANDBOX_ENGINE=docker -- --gpu
assert_parse 'Docker GPU request' grep -Fq -- '--gpus all' <<< "$(create_line)"
assert_parse 'Docker does not use Podman CDI flag' grep -Fvq 'nvidia.com/gpu' <<< "$(create_line)"
run CLAUDE_SANDBOX_ALLOW_DEVICES=/dev/full -- --gpu --device /dev/null --device /dev/zero
assert_parse 'first device reaches engine' grep -Fq -- '--device /dev/null' <<< "$(create_line)"
assert_parse 'second device reaches engine' grep -Fq -- '--device /dev/zero' <<< "$(create_line)"
assert_eq 'device list merges environment' $'/dev/full\n/dev/null\n/dev/zero' "$(cat "$LOG.device-env")"
assert_parse 'Podman device keeps host groups' grep -Fq -- '--group-add keep-groups' <<< "$(create_line)"
run -- --gpu
assert_parse 'no host groups without --device' grep -Fvq -- 'keep-groups' <<< "$(create_line)"
run CLAUDE_SANDBOX_ENGINE=docker -- --device /dev/null
assert_parse 'Docker has no keep-groups' grep -Fvq -- 'keep-groups' <<< "$(create_line)"
run CLAUDE_SANDBOX_ALLOW_DEVICES=/dev/full -- --gpu --device /dev/null --device /dev/zero
binds="$(
    export CLAUDE_SHADOW_SOURCE_ONLY=1
    source "$HERE/../.devcontainer/claude-sandbox/claude-shadow"
    CLAUDE_SANDBOX_ALLOW_DEVICES="$(cat "$LOG.device-env")"
    bwrap_argv_build built "$TMP/project" /fake/claude
    printf '%s\n' "${built[@]}"
)"
for device in /dev/full /dev/null /dev/zero; do
    assert_pair 'host device reaches sandbox' "$binds" --dev-bind "$device"
done
for invalid in /dev /dev/pts /dev/no-such-claude-device /etc/passwd /dev/../etc/passwd --gpu; do
    run -- --device "$invalid"
    assert_eq "reject invalid device $invalid" 1 "$RC"
    assert_eq 'invalid device never creates container' '' "$(create_line)"
done
run -- --device
assert_eq 'device requires argument' 1 "$RC"
printf '%s\n' "claude-sandbox-project-$(printf '%s' "$TMP/project" | cksum | awk '{print $1}')" > "$TMP/ps"
run -- --gpu --device /dev/null
assert_parse 'device reuse warns' grep -Fq -- 'ignored on an existing container: --gpu --device /dev/null' <<< "$ERR"
assert_eq 'device reuse does not recreate' '' "$(create_line)"
rm -f "$TMP/ps"

mkdir -p "$TMP/ws/project" "$TMP/ro" "$TMP/rw"
PROJECT="$TMP/ws/project" run --
assert_parse 'no parent mount by default' grep -Fvq -- "src=$TMP/ws,dst=$TMP/ws," <<< "$(create_line)"
assert_parse 'default retains project mount' grep -Fq -- "-v $TMP/ws/project:$TMP/ws/project -w $TMP/ws/project" <<< "$(create_line)"
PROJECT="$TMP/ws/project" run -- --peers
case "$(create_line)" in *"--mount type=bind,src=$TMP/ws,dst=$TMP/ws,bind-propagation=slave -v $TMP/ws/project:$TMP/ws/project -w"*) pass ;; *) fail "peers parent not rw before project: $(create_line)" ;; esac
assert_parse 'peers is not an agent argument' grep -Fvq -- '--peers' <<< "$(exec_line)"
assert_not_contains "parent not in allow-write" "$(create_line)" "ALLOW_WRITE=$TMP/ws "
PROJECT="$TMP/ws/project" run -- --no-peers
assert_parse 'no-peers skips parent mount' grep -Fvq -- "src=$TMP/ws,dst=$TMP/ws," <<< "$(create_line)"
assert_parse 'no-peers retains project mount' grep -Fq -- "-v $TMP/ws/project:$TMP/ws/project -w $TMP/ws/project" <<< "$(create_line)"
assert_parse 'no-peers is not an agent argument' grep -Fvq -- '--no-peers' <<< "$(exec_line)"
PROJECT="$TMP/ws/project" run -- --no-peers --mount "$TMP/ro" --mount-rw "$TMP/rw"
assert_parse 'no-peers retains explicit read-only mount' grep -Fq -- "src=$TMP/ro,dst=$TMP/ro,ro,bind-propagation=slave" <<< "$(create_line)"
assert_parse 'no-peers retains explicit writable mount' grep -Fq -- "src=$TMP/rw,dst=$TMP/rw,bind-propagation=slave" <<< "$(create_line)"
assert_eq 'no-peers retains sandbox write permission' "$TMP/rw" "$(cat "$LOG.mount-env")"
# Reuse an existing container: create-time options must not silently imply
# that an existing parent mount has been removed.
printf '%s\n' "claude-sandbox-project-$(printf '%s' "$TMP/ws/project" | cksum | awk '{print $1}')" > "$TMP/ps"
PROJECT="$TMP/ws/project" run -- --peers
assert_eq 'peers reuse does not recreate' '' "$(create_line)"
assert_eq 'peers reuse completes normally' 0 "$RC"
assert_parse 'peers reuse warns option was ignored' grep -Fq -- '  warning     ignored on an existing container: --peers' <<< "$ERR"
assert_parse 'peers reuse explains recreation' grep -Fq -- '  rebuild     claude-container --recreate' <<< "$ERR"
# A container created with the old default keeps its parent mount: say so.
PROJECT="$TMP/ws/project" run FAKE_MOUNTS="$TMP/ws"$'\n'"$TMP/ws/project"$'\n' --
assert_parse 'old parent mount warns' grep -Fq -- '  warning     mounts the parent directory; peers are now off by default' <<< "$ERR"
assert_parse 'old parent mount explains recreation' grep -Fq -- '  rebuild     claude-container --recreate' <<< "$ERR"
PROJECT="$TMP/ws/project" run FAKE_MOUNTS="$TMP/ws"$'\n' -- --peers
case "$ERR" in *"mounts the parent directory"*) fail "peers container warned about its own parent mount" ;; *) pass ;; esac
# A plain reuse prints the headline alone.
PROJECT="$TMP/ws/project" run --
assert_eq 'plain reuse prints one line' "claude-sandbox: reusing claude-sandbox-project-$(printf '%s' "$TMP/ws/project" | cksum | awk '{print $1}')" "$ERR"
rm -f "$TMP/ps"
run -- --peers   # project directly under $HOME: parent holds ~, must not be mounted
assert_not_contains "parent containing HOME is not mounted" "$(create_line)" "src=$TMP,dst=$TMP,"
case "$ERR" in *"contains your home directory"*) pass ;; *) fail "HOME guard silent: $ERR" ;; esac
run -- --mount "$TMP/ro" --mount-rw "$TMP/rw"
case "$(create_line)" in *"src=$TMP/ro,dst=$TMP/ro,ro,bind-propagation=slave"*) pass ;; *) fail "--mount not ro: $(create_line)" ;; esac
case "$(create_line)" in *"src=$TMP/rw,dst=$TMP/rw,bind-propagation=slave"*) pass ;; *) fail "--mount-rw not rw: $(create_line)" ;; esac
case "$(create_line)" in *"-e CLAUDE_SANDBOX_ALLOW_WRITE=$TMP/rw "*) pass ;; *) fail "allow-write missing rw mount: $(create_line)" ;; esac
assert_not_contains "ro mount not in allow-write" "$(create_line)" "ALLOW_WRITE=$TMP/rw:$TMP/ro"
run -- --mount; [ "$RC" = 1 ] && pass || fail "--mount without PATH accepted (rc=$RC)"

# Exercise the format handed from the host launcher to the actual shadow.
mkdir -p "$TMP/rw second" "$TMP/from-env"
run CLAUDE_SANDBOX_ALLOW_WRITE="$TMP/from-env" -- --mount-rw "$TMP/rw" --mount-rw "$TMP/rw second"
assert_eq 'merge writable mount paths as lines' \
    "$TMP/from-env"$'\n'"$TMP/rw"$'\n'"$TMP/rw second" "$(cat "$LOG.mount-env")"
binds="$(
    export CLAUDE_SHADOW_SOURCE_ONLY=1
    source "$HERE/../.devcontainer/claude-sandbox/claude-shadow"
    CLAUDE_SANDBOX_ALLOW_WRITE="$(cat "$LOG.mount-env")"
    bwrap_argv_build built "$TMP/project" /fake/claude
    printf '%s\n' "${built[@]}"
)"
for path in "$TMP/from-env" "$TMP/rw" "$TMP/rw second"; do
    assert_pair 'host writable path reaches sandbox' "$binds" --bind "$path"
done
run CLAUDE_SANDBOX_ALLOW_WRITE="$TMP/from-env" --
assert_eq 'forward writable environment without mount flags' "$TMP/from-env" "$(cat "$LOG.mount-env")"

# --- env: locale always, X11 only when the host has a DISPLAY --------------
run --; case "$(create_line)" in *"-e LANG=en_US.UTF-8"*) pass ;; *) fail "LANG default: $(create_line)" ;; esac
assert_not_contains "no DISPLAY without one on the host" "$(create_line)" "DISPLAY"
touch "$TMP/.Xauthority"
run DISPLAY=:1 --
case "$(create_line)" in *"-e DISPLAY=:1"*) pass ;; *) fail "DISPLAY not passed: $(create_line)" ;; esac
case "$(create_line)" in *"-v $TMP/.Xauthority:/root/.Xauthority:ro"*) pass ;; *) fail "Xauthority not mounted ro: $(create_line)" ;; esac
rm -f "$TMP/.Xauthority"

# --- /cache on a named volume; per-project venv on it, as the devcontainer --
run --
case "$(create_line)" in *"-v claude-sandbox-cache:/cache "*) pass ;; *) fail "cache volume: $(create_line)" ;; esac
case "$(create_line)" in *"-e UV_PROJECT_ENVIRONMENT=/cache/venv-for$TMP/project -e VIRTUAL_ENV=/cache/venv-for$TMP/project"*) pass ;; *) fail "per-project venv env: $(create_line)" ;; esac
case "$(create_line)" in *"-e PRE_COMMIT_HOME=/cache/pre-commit"*) pass ;; *) fail "pre-commit home: $(create_line)" ;; esac
assert_not_contains "no link-mode override on one filesystem" "$(create_line)" "UV_LINK_MODE=copy"
run CLAUDE_SANDBOX_CACHE=mine --
case "$(create_line)" in *"-v mine:/cache "*) pass ;; *) fail "cache volume name override: $(create_line)" ;; esac
assert_not_contains "volume name not baked as env" "$(create_line)" "-e CLAUDE_SANDBOX_CACHE=mine"
run CLAUDE_SANDBOX_CACHE= --
case "$(create_line)" in *":/cache "*) fail "empty name should disable the volume: $(create_line)" ;; *) pass ;; esac

run -- install; [ "$RC" = 2 ] && pass || fail "install verb accepted by the script (rc=$RC)"

# --- in-container refusal (seam off) ---------------------------------------
if [ -e /run/.containerenv ] || [ -e /.dockerenv ]; then
    : > "$LOG"
    ( cd "${PROJECT:-$TMP/project}" && env -i PATH="$TMP/bin:/usr/bin:/bin" HOME="$TMP" LOG="$LOG" MARK="$TMP/mark" \
        bash "$LAUNCHER" 2>"$TMP/err" ); rc=$?
    [ "$rc" = 1 ] && grep -q 'inside' "$TMP/err" && [ -z "$(exec_line)" ] && pass \
        || fail "in-container launch not refused (rc=$rc): $(cat "$TMP/err")"
else
    echo "note: not in a container — in-container refusal not exercised" >&2
fi

# --- update hint names the front door that ran us ---------------------------
run FAKE_IMG_VER=99.0.0 --; case "$ERR" in *"curl -fsSLO"*) pass ;; *) fail "copied-script hint: $ERR" ;; esac
run FAKE_IMG_VER=99.0.0 CLAUDE_SANDBOX_LAUNCHER=uvx --
case "$ERR" in *"uvx claude-sandbox@latest"*) pass ;; *) fail "uvx hint: $ERR" ;; esac
case "$ERR" in *"curl"*) fail "uvx hint still mentions curl" ;; *) pass ;; esac
run FAKE_IMG_VER=0.1.0 CLAUDE_SANDBOX_LAUNCHER=uvx --
case "$ERR" in *"uvx claude-sandbox --recreate"*) pass ;; *) fail "newer-launcher hint under uvx: $ERR" ;; esac


# --- messages: headline plus aligned details, no wrapped lines --------------
run CLAUDE_SANDBOX_LAUNCHER=uvx --
assert_parse 'create headline' grep -Eq -- '^claude-sandbox: created claude-sandbox-project-[0-9]+$' <<< "$ERR"
assert_parse 'create names the image' grep -Fxq -- '  image       ghcr.io/diamondlightsource/claude-sandbox:latest' <<< "$ERR"
assert_parse 'create names forge auth' grep -Fxq -- '  forge auth  uvx claude-sandbox shell, then claude-sandbox gh-auth' <<< "$ERR"
long="$(awk 'length > 80' <<< "$ERR")"
assert_eq 'create lines fit 80 columns' '' "$long"
case "$ERR" in *"claude-container:"*) fail "old message prefix: $ERR" ;; *) pass ;; esac

# --- a warning holds the terminal before an agent, never before a shell -----
# script(1) gives the launcher a terminal; one key press lets it continue.
pause_run() {
    : > "$LOG"; rm -f "$TMP/mark"
    printf x | ( cd "$TMP/project" && env -i PATH="$TMP/bin:/usr/bin:/bin" HOME="$TMP" \
        LOG="$LOG" MARK="$TMP/mark" PS="$TMP/ps" IMAGES="$TMP/images" VENVS="$TMP/venvs" CLAUDE_SANDBOX_NESTED=1 \
        FAKE_IMG_VER=99.0.0 timeout 10 script -qec "bash $LAUNCHER $*" /dev/null 2>&1 | tr -d '\r' )
}
out="$(pause_run)"
assert_contains 'warning pauses before an agent' "$out" 'Press any key to continue, Ctrl-C to cancel.'
[ -n "$(exec_line)" ] && pass || fail "agent did not launch after the key press"
out="$(pause_run shell)"
assert_not_contains 'warning does not pause before a shell' "$out" 'Press any key to continue, Ctrl-C to cancel.'
run FAKE_IMG_VER=99.0.0 --
case "$ERR" in *"Press any key"*) fail "paused without a terminal" ;; *) pass ;; esac

# --- clean removes stopped keeper containers; running ones only with --force -
printf '%s\n' claude-sandbox-a-1 claude-sandbox-b-2 claude-sandbox-running-3 > "$TMP/ps"
run -- clean
[ "$RC" = 0 ] && pass || fail "clean rc=$RC: $ERR"
grep -qx 'rm -f claude-sandbox-a-1' "$LOG" && grep -qx 'rm -f claude-sandbox-b-2' "$LOG" && pass || fail "clean did not remove both stopped: $(cat "$LOG")"
grep -q 'rm -f claude-sandbox-running-3' "$LOG" && fail "clean removed a running container without --force" || pass
case "$ERR" in *"kept claude-sandbox-running-3 (running; --force"*) pass ;; *) fail "running container not reported: $ERR" ;; esac
[ -z "$(exec_line)" ] && [ -z "$(create_line)" ] && pass || fail "clean started a session"
case "$ERR" in *"2 container(s) removed, 1 running kept"*) pass ;; *) fail "clean summary: $ERR" ;; esac
run -- clean --force
grep -qx 'rm -f claude-sandbox-running-3' "$LOG" && pass || fail "--force did not remove the running container: $(cat "$LOG")"
case "$ERR" in *"3 container(s) removed, 0 running kept"*) pass ;; *) fail "--force summary: $ERR" ;; esac
grep -q '^rmi ' "$LOG" && fail "clean touched images without --images" || pass
printf '%s\n' ghcr.io/diamondlightsource/claude-sandbox:4.0.0 in-use:1 > "$TMP/images"
run -- clean --force --images
grep -qx 'rmi ghcr.io/diamondlightsource/claude-sandbox:4.0.0' "$LOG" && pass || fail "--images did not rmi: $(cat "$LOG")"
case "$ERR" in *"removed image ghcr.io/diamondlightsource/claude-sandbox:4.0.0"*) pass ;; *) fail "image summary: $ERR" ;; esac
case "$ERR" in *"removed image in-use"*) fail "in-use image reported removed" ;; *) pass ;; esac
# --- clean --venvs prunes venvs whose project container is gone ------------
live="$TMP/ws/live"; gone="$TMP/ws/gone"
live_name="claude-sandbox-live-$(printf '%s' "$live" | cksum | awk '{print $1}')"
printf '%s\n' "$live_name" > "$TMP/ps"            # only the live project's container exists (and is running)
printf '%s\n' "/cache/venv-for$live" "/cache/venv-for$gone" > "$TMP/venvs"
run -- clean
[ "$RC" = 0 ] && pass || fail "clean rc=$RC: $ERR"
grep -q -- "-rf /cache/venv-for$gone" "$LOG" && pass || fail "stale venv not removed: $(grep "^run" "$LOG")"
grep -q -- "-rf /cache/venv-for$live" "$LOG" && fail "live project's venv removed" || pass
case "$ERR" in *"1 venv(s) removed"*) pass ;; *) fail "venvs summary: $ERR" ;; esac
grep -q "run --rm --entrypoint find " "$LOG" && pass || fail "venv listing must bypass the image entrypoint: $(grep "^run" "$LOG")"
grep -q "run --rm --entrypoint rm " "$LOG" && pass || fail "venv removal must bypass the image entrypoint"
run -- clean --venvs; [ "$RC" = 2 ] && pass || fail "--venvs should be gone (rc=$RC)"
run CLAUDE_SANDBOX_CACHE= -- clean
grep -q "^run " "$LOG" && fail "clean ran helper containers with no cache volume" || pass
rm -f "$TMP/ps" "$TMP/venvs"

run -- clean --bogus; [ "$RC" = 2 ] && [ -z "$(grep '^rm ' "$LOG")" ] && pass || fail "clean --bogus accepted (rc=$RC)"
rm -f "$TMP/ps" "$TMP/images"
echo "launcher: $PASSED passed, $FAILED failed"
[ "$FAILED" -eq 0 ]
