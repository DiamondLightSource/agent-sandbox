(adr-python-implementation)=

# 26. Reimplement the sandbox in Python that execs bubblewrap

Date: 2026-10-06

## Status

Accepted

Amended 2026-10-06: the CLI uses argparse, and the package has no runtime
dependencies. Typer would have put rich, pygments and click, about 15 MB
(measured in the phase 0 spike on issue #72), into the trusted set of
helpers that handle PATs and run as root.

Supersedes {ref}`ADR 8 <adr-bash-only>` (bash-only). Amends
{ref}`ADR 23 <adr-pypi-front-door>`: the wheel stops bundling bash verbatim
and becomes the implementation. Leaves {ref}`ADR 9 <adr-shadow-on-path>`
(shadow on PATH) and {ref}`ADR 18 <adr-multi-agent-shadow>` (one shadow,
many agents) in force; the shadow keeps its names and its place on PATH, and
only what it hands off to changes.

## Context

ADR 8 chose bash because the tool was "one bash function building a bwrap
argv", and the security surface was "two short bash files … that you can read
top to bottom". At the time each file was about 80 lines.

That premise no longer holds. The implementation is now about 3,900 lines of
bash across several files, plus about 3,300 lines of bash tests:

| File | Lines |
|---|---|
| `.devcontainer/claude-sandbox/claude-shadow` | 1356 |
| `.devcontainer/claude-sandbox/install.sh` and `install` | 947 |
| `container/claude-container` | 620 |
| `.devcontainer/claude-sandbox/claude-sandbox` | 412 |
| battery, `codex-launch`, `pi-run`, `entrypoint.sh` | about 600 |

Most of that code builds argument lists, parses configuration and validates
ports and paths: string and list work that Python does plainly and bash does
with namerefs, `${arr[@]+"${arr[@]}"}` guards, `printf %q` re-quoting through
`script -c`, and `export -f` to carry seven functions into an
`unshare … bash -c` holder. At this size bash is the less auditable choice,
which is the opposite of what ADR 8 intended.

There are also two CLIs that overlap: the host launcher
(`container/claude-container`, run through `uvx claude-sandbox`) and the
in-container `claude-sandbox` helper. The launcher forwards a hand-maintained
list of verbs into the container.

The Python era ended by `bf65407` (issue #14) failed because it spread a small
security core across many modules. This ADR has to avoid repeating that, and
it adds hazards that bash does not have: an interpreter can be redirected by
`PATH`, `PYTHONPATH`, `sitecustomize`, `usercustomize` and `.pth` files.

Options considered:

- **Keep bash and split it into sourced files.** Readability improves a
  little, but the hard constructs remain, and the argv tests stay a bash
  harness that sources functions.
- **Keep the shadow in bash and port only the CLIs.** The shadow is the
  largest file and holds nearly all of the security-critical code (the bwrap
  argv builder and the egress jail), so this ports the easy part and leaves
  the hard part.
- **Port everything to Python, installed as a pinned, root-owned
  interpreter and venv.** Chosen.

## Decision

The sandbox is a Python package, `claude_sandbox`, that builds argument lists
and execs `bwrap`, `unshare`, `pasta` and `script`. It ships as the existing
PyPI wheel. A root `pyproject.toml` and `src/claude_sandbox/` replace
`packaging/pypi/`. Development uses uv, pytest, ruff and pyright, with a
development lockfile.

**One package, one CLI, three contexts.** `context.py` detects once whether
the process is on the HOST, in the CONTAINER (outside the jail) or in the
JAIL. Commands declare where they run, for example
`@requires(CONTAINER, forward_from=HOST)` for `verify`. The host launcher and
the in-container helper become one CLI, `claude-sandbox`, built on argparse;
the package has no runtime dependencies. A command called on the host that
belongs in the container is forwarded by
`podman exec` or `docker exec`. A command that has no meaning in the current
context refuses with a clear message. A forwarded command runs the version
installed in the container, as it does today.

**The shadow becomes a three-line bash shim.** `/usr/local/bin/claude`,
`codex` and `pi` remain one root-owned file installed before the vendor
installers can claim the names (Invariant 1). It is:

```bash
#!/bin/bash
# /usr/local/bin/claude (and codex, pi): hand off to the root-owned install.
exec /usr/libexec/claude-sandbox/venv/bin/python -I -m claude_sandbox _shadow "${0##*/}" -- "$@"
```

Callers (the VS Code extension, hooks, `claude -p` in scripts) keep calling
`claude --resume`; the shim inserts the `--`, so arguments reach the real
agent unchanged. `claude_sandbox/__main__.py` checks for `_shadow` before
importing the CLI, so the launch path imports only what it needs and adds
little startup time.

**The audit core stays in a few files you can read top to bottom.**

- `bwrap.py`: a pure function from (agent profile, config, environment) to
  the bwrap argv. Standard library only, no I/O beyond existence checks.
- `jail.py`: the network namespace holder, pasta, DNS forwarding and relays.
- `shadow.py`: the recursion guard, launch preparation and the exec of
  `script` and `bwrap`.

Profiles, configuration parsing, the host launcher, the installer and the
helper commands live in their own modules around that core. No helper module
contributes binds or environment to the argv except through `bwrap.py`.

**The interpreter cannot be redirected.**

- `install` places a pinned, uv-managed CPython and a venv under
  `/usr/libexec/claude-sandbox/`, root-owned and byte-compiled at install.
  Never run from uv's cache: `~/.cache` is writable from inside the jail.
- Every entry point names that interpreter by absolute path and runs it with
  `-I` (isolated mode), which ignores `PYTHON*` variables, the user
  site-packages directory and the current directory. No
  `#!/usr/bin/env python3`: in the published image `PATH` starts with
  `/opt/venv/bin`, which resolves into `/cache`, which the jail can write.
- A test plants `sitecustomize.py`, `usercustomize.py`, a `.pth` file, a
  `PYTHONPATH` and a fake `python` earlier on `PATH`, then launches, and
  asserts that none of them ran.

**Installation.** `uvx claude-sandbox install`, already the documented route,
installs the wheel into the root-owned venv. The clone route keeps a short
bash bootstrap that fetches uv and hands over to the same Python installer.
Guest devcontainers therefore need network access at install time, which they
already need to fetch the agent binaries.

**What stays in bash.** The integrity battery
(`verify-sandbox-battery.sh`), which probes the jail from inside with shell
commands, and the small in-jail exec wrappers (`codex-launch`, `pi-run`) and
`container/entrypoint.sh`, until there is a reason to change them. The
end-to-end bash tests stay as black-box tests of the new implementation.

**Migration is incremental.** The Python argv builder must produce the same
argv as the bash one across a matrix of configurations before it ships, and
the Python shadow ships behind an opt-in switch before it becomes the
default. The plan is tracked in a GitHub issue.

## Consequences

- `CLAUDE.md`'s Python rules, the `claude-sandbox` skill's "Reversal 1" and
  the `claude-sandbox-container` skill are rewritten when this ADR is
  accepted. The refuse-list changes from "no Python" to: no runtime
  dependencies (the package is the standard library only), no interpreter
  found through `PATH`, no running without `-I`, and no bind or environment
  added outside `bwrap.py`.
- Each guest gains a pinned CPython and venv: 56 MB once pruned, against
  124 MB as uv installs it (measured 2026-10-06, issue #72 phase 4), plus the
  pinned uv the installer keeps to provision it (46 MB).
- The trusted set grows from bash and coreutils to CPython.
- The argv tests become pytest on a pure function. `tests/bwrap_argv.sh` is
  removed once the Python builder is the only one.
- The Dockerfile's `source install.sh` reuse is replaced by calling the
  Python installer.
- The first release with the Python shadow as the default is 5.0.0.
- Risks specific to the port: Python's SIGINT handling while a child owns
  the terminal, keeping the netns holder's stdin on the terminal, and
  replacing the shadow's `trap EXIT` cleanup with `try/finally` and signal
  handlers. Each needs a test before the switch flips.
