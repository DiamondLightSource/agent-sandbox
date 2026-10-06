# Verification checks

`claude-sandbox verify` runs the installed 22-check battery and exits nonzero
on failure. The `verify-sandbox` skill adds ten agent-driven adversarial probes
only after all checks pass. See [how to run verification](../how-to/verify-the-sandbox.md).

The implementation is `.devcontainer/claude-sandbox/verify-sandbox-battery.sh`,
installed under `/usr/libexec/claude-sandbox/`. Detailed rationale lives in
`skills/verify-sandbox/references/checks.md`.

## Phase 1 — the 22-check battery

| # | Assertion |
|---|---|
| 01 | `IS_SANDBOX=1` is set; a launch marker, not independent proof of isolation |
| 02 | `NoNewPrivs: 1` prevents setuid escalation |
| 03 | Home contains only allowed entries, the running agent's own state and shared skills; `.config` contains only forge stores |
| 04 | `GH_TOKEN` and `OPENAI_API_KEY` are empty |
| 05 | `DISPLAY` is empty |
| 06 | Effective capabilities (`CapEff`) are zero |
| 07 | PID namespace is isolated, checked through `NSpid` nesting |
| 08 | The IPC namespace entry is present |
| 09 | The UTS namespace entry is present |
| 10 | `/dev` is a fresh `tmpfs`/`devtmpfs` mount |
| 11 | No VS Code IPC or Git sockets are visible in `/tmp` |
| 12 | `/run/user` is empty |
| 13 | `/run/secrets` is empty |
| 14 | `~/.netrc` is empty |
| 15 | `~/.Xauthority` is empty |
| 16 | The curated Git config is selected and supplies `user.email` |
| 17 | Writable workspace scope matches the launch directory or explicit override |
| 18 | The installed wrapper reads config from `/etc/claude-sandbox.conf`, not the workspace |
| 19 | Network routes contain the required RFC1918/CGNAT blackholes and a default route |
| 20 | Representative blocked destinations have no forwardable route; the gateway remains routable |
| 21 | Codex's writable `packages` binary cache is masked or absent; other profiles pass with a note |
| 22 | Each entry-point name (`claude`, `codex`, `pi`, `claude-sandbox`) in a writable `PATH` directory ahead of the shadow is a read-only, non-executable mount point; passes with a note when there is none |

**Checks 19–20 pass with a note when the network jail is deliberately disabled.**
A green battery alone does not establish that network isolation is enabled.
Check 06 inspects effective capabilities, which are zero even when the nested
user namespace retains a full bounding set.

The [defence table](locked-down-defences.md) maps these assertions to controls.
Checks of markers or namespace entries do not prove every aspect of isolation;
the [live procfs test](../explanations/sandbox-internals.md#the-procfs-view) adds
behavioural signal and debugger checks.

## Phase 2 — adversarial breakout probes

After a clean battery, the skill directs the agent to try ten distinct,
reversible attacks beyond those checks: for example, credential recovery,
namespace crossings, unexpected writable paths or forbidden service access.
It stops on a demonstrated escape.

| Result | Meaning |
|---|---|
| `[BLOCKED]` | The attempted breach was prevented |
| `[ESCAPED]` | A demonstrated violation of the threat model; report `SANDBOX LEAKING` |
| `[INCONCLUSIVE]` | No demonstrated breach or block; report `AUDIT INCOMPLETE` and a follow-up |

Fewer than ten completed probes also means an incomplete audit. Only ten
blocked probes after a clean battery produce
`RESULT: SANDBOX OK (22 deterministic + 10 adversarial)`.
The interactive agent's exit status is not a CI result; inspect its report.
