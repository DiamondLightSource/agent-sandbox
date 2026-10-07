#!/usr/bin/env bash
# Exercise the supervisor with a real Unix socket and a fake Codex process.
set -uo pipefail
command -v python3 >/dev/null 2>&1 || { echo 'codex_launch.sh: python3 is required for the Unix-socket test fixture' >&2; exit 1; }
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
source "$REPO_ROOT/tests/lib.sh"
MOCK_DIR="$(mktemp -d)"
register_cleanup "$MOCK_DIR"
export MOCK_DIR
cat >"$MOCK_DIR/codex" <<'MOCK'
#!/usr/bin/env bash
printf '%s\n' "$@" >>"$MOCK_DIR/args"
if [ "$1" = app-server ]; then
    if [ "${MOCK_FAIL:-0}" = 1 ]; then
        echo 'mock startup failure' >&2
        exit 3
    fi
    printf '%s\n' "${!#}" >"$MOCK_DIR/endpoint"
    printf '%s\n' "$PWD" >"$MOCK_DIR/server-cwd"
    exec python3 -c '
import os, signal, socket, sys
from pathlib import Path
def stop(signum, frame):
    if os.environ.get("MOCK_STUBBORN") == "1":
        return
    Path(os.environ["MOCK_DIR"], "stopped").write_text("yes")
    sys.exit(0)
signal.signal(signal.SIGTERM, stop)
server = socket.socket(socket.AF_UNIX)
server.bind(sys.argv[1][7:])
server.listen()
while True:
    signal.pause()
    ' "${!#}"
fi
if [ "${!#}" = "$(cat "$MOCK_DIR/endpoint" 2>/dev/null)" ]; then
    endpoint="${!#}"
    test -S "${endpoint#unix://}" || exit 9
fi
if [ "${MOCK_WAIT:-0}" = 1 ]; then
    touch "$MOCK_DIR/client-ready"
    exec sleep 30
fi
exit 7
MOCK
chmod +x "$MOCK_DIR/codex"
run_launch() {
    "$REPO_ROOT/.devcontainer/claude-sandbox/codex-launch" "$MOCK_DIR/codex" "$@"
}

run_launch -c 'model="test model"' agents --no-alt-screen
assert_eq client-exit 7 "$?"
expect_file "$MOCK_DIR/stopped" 'server was not stopped on client exit'
endpoint="$(cat "$MOCK_DIR/endpoint")"
assert_eq runtime-cleaned no "$(test -e "${endpoint#unix://}" && echo yes || echo no)"
assert_eq config-forwarded 2 "$(grep -cx 'model="test model"' "$MOCK_DIR/args")"
assert_eq local-connection 1 "$(grep -cx -- '--remote' "$MOCK_DIR/args")"

mkdir -p "$MOCK_DIR/work space"
run_launch -C "$MOCK_DIR/work space" agents
assert_eq cd-exit 7 "$?"
assert_eq server-cwd "$MOCK_DIR/work space" "$(cat "$MOCK_DIR/server-cwd")"
( cd "$MOCK_DIR" && run_launch agents '--cd=work space' )
assert_eq relative-cd-exit 7 "$?"
assert_eq relative-server-cwd "$MOCK_DIR/work space" "$(cat "$MOCK_DIR/server-cwd")"
run_launch agents --cd "$MOCK_DIR/missing" >"$MOCK_DIR/cd-error" 2>&1
assert_eq bad-cd-exit 1 "$?"

run_launch --profile foo agents >"$MOCK_DIR/unsupported" 2>&1
assert_eq unsupported-exit 7 "$?"
if [[ "$(cat "$MOCK_DIR/unsupported")" == *'unsupported agents option --profile'* ]]; then
    pass
else
    fail 'unsupported agents options did not explain native fallback'
fi

for args in 'agents --help' 'agents --remote=unix:///explicit' 'exec agents' '--version'; do
    : >"$MOCK_DIR/args"
    # Deliberate splitting of the fixed test cases above.
    # shellcheck disable=SC2086
    run_launch $args
    assert_eq passthrough-exit 7 "$?"
    assert_not_contains passthrough "$(cat "$MOCK_DIR/args")" app-server
done

# Native startup bypasses the shared daemon, whose executable the sandbox hides.
for args in '' 'resume --last' '--no-daemon' '--remote=unix:///explicit'; do
    : >"$MOCK_DIR/args"
    # shellcheck disable=SC2086
    run_launch $args
    case "$args" in --remote*) want=0 ;; *) want=1 ;; esac
    assert_eq "no-daemon[$args]" "$want" "$(grep -cx -- '--no-daemon' "$MOCK_DIR/args")"
done

: >"$MOCK_DIR/args"
MOCK_FAIL=1 run_launch agents >"$MOCK_DIR/failure" 2>&1
assert_eq startup-failure 1 "$?"
assert_contains startup-diagnostic "$(cat "$MOCK_DIR/failure")" 'mock startup failure'
assert_not_contains no-client-on-failure "$(cat "$MOCK_DIR/args")" agents

# Terminating the supervisor must stop both children and remove its socket.
: >"$MOCK_DIR/stopped"
MOCK_WAIT=1 bash -c 'source "$1"; shift; codex_launch "$@"' test \
    "$REPO_ROOT/.devcontainer/claude-sandbox/codex-launch" "$MOCK_DIR/codex" agents &
supervisor=$!
for attempt in {1..100}; do
    [ -f "$MOCK_DIR/client-ready" ] && break
    sleep 0.1
done
expect_file "$MOCK_DIR/client-ready"
kill -TERM "$supervisor"
wait "$supervisor"
assert_eq signal-exit 143 "$?"
assert_eq signal-server-stopped yes "$(cat "$MOCK_DIR/stopped")"
endpoint="$(cat "$MOCK_DIR/endpoint")"
assert_eq signal-runtime-cleaned no "$(test -e "${endpoint#unix://}" && echo yes || echo no)"

# A TERM-resistant server must be killed rather than hang the launcher.
SECONDS=0
MOCK_STUBBORN=1 timeout 8 "$REPO_ROOT/.devcontainer/claude-sandbox/codex-launch" \
    "$MOCK_DIR/codex" agents
assert_eq stubborn-client-exit 7 "$?"
if [ "$SECONDS" -lt 8 ]; then pass; else fail 'server cleanup exceeded deadline'; fi
endpoint="$(cat "$MOCK_DIR/endpoint")"
assert_eq stubborn-runtime-cleaned no "$(test -e "${endpoint#unix://}" && echo yes || echo no)"

finish codex_launch.sh
