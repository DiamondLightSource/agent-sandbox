# Locked-down defences

The main isolation controls and the [verification checks](verification-checks.md)
that inspect them. A passing check is evidence for its stated assertion,
not a complete proof against every attack on that interface.

## Defence → primitive → check

<!-- TODO(phase5): confirm the entry-point guard row (last) against fix/entry-point-guard, including whether check 22 covers it -->

| Defence | bwrap primitive | Verify |
|---|---|---|
| Sandbox is actually entered | `IS_SANDBOX=1` sentinel | check 01 |
| Setuid escalation blocked | `NO_NEW_PRIVS` (set by bwrap before exec) | check 02 |
| Home credentials masked | `--tmpfs /root`, then selected agent state, shared skills, forge stores and tool data bound back | check 03 |
| Host env vars scrubbed | `--clearenv` + explicit allow-list | checks 04, 05 |
| No effective capabilities | `--cap-drop ALL` | check 06 |
| PID namespace (kill/ptrace scoping) | `--unshare-pid` | check 07 |
| SysV IPC namespace | `--unshare-ipc` | check 08 |
| UTS namespace | `--unshare-uts` | check 09 |
| TIOCSTI terminal injection blocked | `--dev /dev` + `script(1)` pty wrap | check 10 |
| VS Code IPC bridges masked | `--tmpfs /tmp` | check 11 |
| User runtime dir masked | `--tmpfs /run/user` | check 12 |
| Docker/Compose secrets masked | `--tmpfs /run/secrets` | check 13 |
| `.netrc` defence in depth | `--bind-try /dev/null /root/.netrc` | check 14 |
| `.Xauthority` defence in depth | `--bind-try /dev/null /root/.Xauthority` | check 15 |
| Curated gitconfig in effect | `GIT_CONFIG_GLOBAL=/etc/claude-gitconfig`, `GIT_CONFIG_SYSTEM=/dev/null` | check 16 |
| Chrome browser-extension RPC channel disabled | shadow injects `--no-chrome` and strips user `--chrome` so Claude Code never writes its `NativeMessagingHosts` manifest | check 03 (regression manifests as browser dirs under `~/.config`) |
| Entry-point names stay the shadow's (Invariant 1) | `--ro-bind /dev/null` over `claude`, `codex`, `pi` and `claude-sandbox` in each writable `PATH` directory ahead of `/usr/local/bin` (Python shadow), and a launch-time refusal when one is present there | check 22 |
| Lateral-movement egress isolation | netns + `pasta` routing allowlist around bwrap; blocks RFC1918, CGNAT, connected subnets and link-local ({ref}`adr-network-egress-jail`) | checks 19–20 inspect blackhole routes and representative destinations; a disabled jail is reported as a pass with a note |
| Launch code cannot be redirected | not a bwrap primitive: the shim runs `/usr/libexec/claude-sandbox/venv/bin/python -I` by absolute path, and the launch path runs its tools from fixed system directories | CI only (`tests/python/test_hijack.py`) |
| Executables a session adds ahead of system commands on PATH, and new Git hooks, quarantined | not a bwrap primitive: a watcher outside the jail while the session runs; warnings in outer shells and at session end | check 22 |

## Reading the results

Check 06 inspects effective capabilities (`CapEff=0`), not the bounding set.
Checks 19–20 inspect network routes but report a deliberately disabled jail
as a pass with a note. Read those notes before concluding that network
isolation is enabled.

Missing namespace or network-jail prerequisites cause launch to fail.
`--die-with-parent` also terminates bubblewrap when its parent dies.
See [Architecture](../explanations/architecture.md) for the launch sequence and
[Sandbox internals](../explanations/sandbox-internals.md) for the interpreter
and the entry-point guard.
