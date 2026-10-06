#!/usr/bin/env bash
# Run ONLY in a disposable container with the sandbox and Pi already installed.
# The Container workflow runs this against the actual built image on both archs.
set -euo pipefail
test -x /usr/libexec/claude-sandbox/pi-dist/pi
rg --version >/dev/null
fdfind --version >/dev/null
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
tmp="$(mktemp -d)"
server=""
cleanup() {
    if [ -n "$server" ]; then kill -TERM -- "-$server" 2>/dev/null || true; wait "$server" 2>/dev/null || true; fi
    rm -rf "$tmp"
}
trap cleanup EXIT
mkdir -p /work "$HOME/.pi/agent"
printf 'SEARCH_TOOL_OK\n' > /work/search-fixture.txt
git config --global user.name 'Pi integration test'
git config --global user.email 'pi-test@example.invalid'
touch /tmp/outer-only
cp "$REPO_ROOT/tests/fixtures/pi-model-server.sh" "$tmp/http"
printf local-test > /tmp/pi-model-id
printf 32768 > /tmp/pi-model-context
setsid socat TCP4-LISTEN:1920,bind=127.0.0.1,reuseaddr,fork "EXEC:bash $tmp/http" &
server=$!
for _ in {1..100}; do
    if ss -H -ltn 'sport = :1920' | grep -q '127.0.0.1:'; then break; fi
    sleep 0.05
done
cd /work
unset CLAUDE_SANDBOX_LOCAL_MODEL_PORT
# Avoid catalog/version traffic in this test. The production profile leaves
# catalog refresh enabled so cloud logins can discover their available models.
export PI_OFFLINE=1 CLAUDE_SANDBOX_PASS_ENV=PI_OFFLINE
run_pi() {
    local rc=0
    rm -f /work/tool-proof /work/battery /tmp/pi-search-results.json
    # Keep the terminal's foreground process group under podman -t. Plain timeout
    # creates a background group, so script(1)'s terminal setup stops on SIGTTOU.
    timeout --foreground 60 pi "$@" --no-session --tools bash,find,grep \
        --no-extensions --no-skills --no-prompt-templates --no-themes \
        -p 'Find and search the fixture, then run the sandbox checks using your bash tool.' > "$tmp/output" 2>&1 || rc=$?
    cat "$tmp/output"
    test "$rc" -eq 0
    cat /work/battery
    test "$(cat /work/tool-proof)" = PI_TOOL_OK
    grep -q LOCAL_MODEL_OK "$tmp/output"
    jq -e 'length == 3 and .[0].content == "search-fixture.txt" and (.[1].content | contains("SEARCH_TOOL_OK"))' /tmp/pi-search-results.json >/dev/null
}
# Pi 1.0 rejects --provider without --model; --model lllm2 selects the
# provider's one discovered model, so no model ID is hard-coded here.
run_pi --model lllm2
jq -e '.providers.lllm2.models[0].id == "local-test" and .providers.lllm2.models[0].contextWindow == 32768' "$HOME/.pi/agent/models.json" >/dev/null
# A saved selection of yesterday's model must not require manual ID changes.
printf '{"defaultProvider":"lllm2","defaultModel":"local-test"}\n' > "$HOME/.pi/agent/settings.json"
printf renamed-model > /tmp/pi-model-id
printf 65536 > /tmp/pi-model-context
# No model flag: Pi must fall back from the stale saved default to the
# rediscovered model.
run_pi
jq -e '.providers.lllm2.models[0].id == "renamed-model" and .providers.lllm2.models[0].contextWindow == 65536' "$HOME/.pi/agent/models.json" >/dev/null
cp "$HOME/.pi/agent/models.json" "$tmp/before-offline"
printf should-not-discover > /tmp/pi-model-id
CLAUDE_SANDBOX_LOCAL_MODEL_PORT=0 timeout --foreground 20 pi --version > "$tmp/disabled" 2>&1
cmp "$tmp/before-offline" "$HOME/.pi/agent/models.json"
kill -TERM -- "-$server"
wait "$server" 2>/dev/null || true
server=""
timeout --foreground 20 pi --version > "$tmp/offline" 2>&1
cmp "$tmp/before-offline" "$HOME/.pi/agent/models.json"
! grep -q 'could not discover' "$tmp/offline"
echo 'pi_e2e.sh: Pi discovered and switched local models, streamed real tools through the jail, and started offline; all sandbox checks passed'
