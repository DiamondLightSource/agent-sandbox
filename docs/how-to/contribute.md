# Contribute to claude-sandbox

Report bugs and propose changes through
[GitHub issues](https://github.com/DiamondLightSource/claude-sandbox/issues).
Use [Discussions](https://github.com/DiamondLightSource/claude-sandbox/discussions)
for open-ended questions. Agree the scope of large changes before implementation.

## Development setup

Clone the repository and open its devcontainer for work on the sandbox itself.
To install the checkout you are editing:

```bash
./install --here
```

Without `--here`, the installer selects a release and refuses a pinned,
non-default or modified checkout.

The sandbox is migrating from Bash to a Python package
({ref}`ADR 26 <adr-python-implementation>`, issue #72). Until that finishes,
the Bash is what runs: the wheel bundles it and its entry point executes it.
See `CLAUDE.md` for the project boundaries and the Python guardrails.
The repository's `.claude/` holds the skills, commands and hooks for developing
this repo; only the top-level `skills/` tree ships to users (see the
`claude-sandbox-shipped-skills` skill).

## Validation

The suites are shell scripts; the CI workflow is the complete list.
Core installation and launcher checks include:

```bash
CLAUDE_SANDBOX_SMOKE=1 bash tests/bwrap_argv.sh
CLAUDE_SANDBOX_SMOKE=1 bash tests/smoke.sh
bash tests/install_ref.sh
bash tests/launcher.sh
bash tests/shadow_launch.sh
bash tests/shadow_git.sh
bash tests/verify.sh
bash tests/install_modes.sh
```

Run installation tests as root inside the development container.
The smoke flag confines fixture installations to temporary directories.
Network tests need namespaces and capabilities unavailable inside an agent
sandbox; run those from an ordinary container terminal as described in CI.

## Python development

The package lives in `src/claude_sandbox/` with a root `pyproject.toml`.
The development tools (pytest, ruff, pyright) are pinned in `uv.lock`:

```bash
uv sync
uv run pytest --cov
uv run ruff check
uv run ruff format --check
uv run pyright
```

pytest collects `tests/python/` only; the shell suites above run directly.
CI requires at least 95% branch coverage of `src/claude_sandbox/`.
Build the wheel with:

```bash
uv build --wheel -o dist
```

### The Python port and the comparison harness

ADR 26 replaces the bash with Python one phase at a time (issue #72). The
first phase ports the pure parts of `claude-shadow` into
`src/claude_sandbox/`:

| Module | Ported from | What it holds |
|---|---|---|
| `profiles.py` | `detect_agent`, `agent_profile`, `agent_exec_argv`, `filter_chrome_args` | One frozen dataclass per agent: the real binary, home paths, injected flags, `--chrome` stripping |
| `config.py` | `parse_config`, `resolve_workspace_root`, the port helpers | The `/etc/claude-sandbox.conf` parser and the local-port and callback-port checks |
| `bwrap.py` | `bwrap_argv_build` | A pure function from profile, config and environment to the bwrap argv |
| `gitconfig.py` | `render_gitconfig` | The jail's git config, returned as text |

Phase 2 adds the launch path that uses them:

| Module | Ported from | What it holds |
|---|---|---|
| `shadow.py` | the launch body, `configure_launch`, `sandbox_launch` | One launch, top to bottom: the recursion guard, the refusals, the conf and git config, the directories the binds need, the warnings, and the `script(1)` wrap around the bwrap argv |
| `jail.py` | `netns_launch`, `netns_holder`, `jail_stage_dns` and the relay helpers | The egress jail: the namespace holder, pasta, DNS forwarding and the loopback relays |
| `tools.py` | | The fixed system directories the launch path runs its tools from; nothing is found through `PATH` |
| `__main__.py` | | Dispatches the shim's `_shadow` call and the jail's `_jail_holder` before importing anything outside the standard library |

By default nothing calls these modules: the installer, the shadow and the
`uvx claude-sandbox` front door still run the bash, so the modules don't
change sandbox behaviour unless you opt in to the Python shadow (see below).
They use the standard library only, and
`tests/python/test_wheel.py` fails if one of them imports anything else.
`bwrap.py` reads the environment only from the mapping it is given. It
reads the filesystem only through an injectable probe, which can test what
a path is, resolve it the way `realpath -e` does, and list the matches for
a glob such as `/dev/nvidia*`.

The comparison harness checks that the port matches the bash. For each
scenario it builds a temporary directory tree and runs both builders against
that tree with the same environment. The bash side is
`tests/python/argv_driver.sh`, which sources `claude-shadow` with
`CLAUDE_SHADOW_SOURCE_ONLY=1` and prints the argv separated by NUL bytes.
The resulting argv, or the refusal message, must be identical. Run it with
the rest of the suite, or on its own:

```bash
uv run pytest tests/python/test_parity.py
```

It needs only bash, coreutils and git. Each scenario names the
`tests/bwrap_argv.sh` cases it stands for. If you change `bwrap_argv_build`
or `parse_config`, make the same change in the Python and add a scenario for
it. A failure reports the first argv index where the two differ.

The same file compares whole launches. `tests/python/launch_driver.sh`
installs a copy of `claude-shadow` under a fixture directory, as
`tests/verify.sh` does, and runs it; the Python shadow runs against the same
paths. The exit status, the warnings, the command that is executed (with the
`script -c` command compared as the words bash reads back), the git config
and every path the launch creates must match.

Where the Python differs from the bash on purpose, for example by not
expanding pass-env names as globs against the workspace, the difference is
listed in `KNOWN_DIVERGENCES` in `tests/python/test_parity.py` with its
reason. A listed scenario must still differ, so once the bash is fixed the
entry fails and can be removed. The harness is removed with the bash in
phase 5.

### Trying the Python shadow

The Python shadow is opt-in until phase 5, and it is chosen when the sandbox
is installed, never when an agent starts. With `CLAUDE_SANDBOX_IMPL=python`
the installer places the three-line shim from ADR 26 at
`/usr/local/bin/claude`, `codex` and `pi`. It also installs a CPython
interpreter and a venv holding `src/claude_sandbox`, both root-owned, under
`/usr/libexec/claude-sandbox/`. Fetching the interpreter needs `uv` and
network access. In a devcontainer terminal of this repository (not inside an
agent session), run:

```bash
CLAUDE_SANDBOX_IMPL=python ./install --here
head -3 /usr/local/bin/claude    # the shim, not the bash shadow
```

To use it from the start in this repository's devcontainer, set
`CLAUDE_SANDBOX_IMPL=python` in the host environment that VS Code starts
from, then rebuild the container. `devcontainer.json` passes the variable to
`postCreate`. To go back to the bash shadow, run `./install --here` without
the variable, or rebuild without it. The installer then puts the bash shadow
back and removes the interpreter.
`claude-sandbox update` installs a published release, which won't carry the
opt-in until a release that includes the Python shadow ships.

The Python shadow runs the egress jail by default, as the bash shadow does,
and refuses to launch if the jail cannot start.

### The egress jail in Python

`jail.py` ports `netns_launch`, `netns_holder`, `jail_stage_dns` and the
relay helpers. The shadow calls `stage_dns` before it builds the bwrap argv,
because `bwrap.py` binds the staged resolver, and then hands the `script(1)`
command to `launch`, which runs it in the jail and exits with its status.
The bash ran the namespace holder as `unshare -rn bash -c` with `export -f`.
The Python holder re-enters the package instead:
`unshare -rn <sys.executable> -I -m claude_sandbox _jail_holder -- COMMAND`,
so it runs the same root-owned interpreter, in isolated mode, that launched
it, and `__main__.py` dispatches `_jail_holder` before importing anything
else. The holder inherits stdin and the process group, so it, `script` and
`bwrap` stay in the terminal's foreground group.

Every side effect in `jail.py` goes through an `Ops` object, so the unit
tests in `tests/python/test_jail.py` replace it and check the argv, the fail-closed
paths, the cleanup and the exit status after each signal. Real namespaces
need `/dev/net/tun` and unprivileged user namespaces, so
`tests/python/test_jail_netns.py` skips elsewhere. Run it in this
repository's image with `tests/jail_python.sh`; the comment at its top gives
the `podman run` command.

### One CLI: host, container and jail

Phase 3 of issue #72 ports the host launcher (`container/claude-container`)
and the in-container helper (`.devcontainer/claude-sandbox/claude-sandbox`)
into one command, `claude-sandbox`, built on argparse. Like the rest of the
package it uses the standard library only.

`context.py` decides once where the process runs:

- **JAIL** when `IS_SANDBOX=1`, which the shadow sets inside an agent
  session;
- **CONTAINER** when `/run/.containerenv` or `/.dockerenv` exists;
- **HOST** otherwise, or when `CLAUDE_SANDBOX_NESTED=1` (an engine inside a
  container, as the bash launcher allows).

Each command declares where it runs, for example
`@requires(CONTAINER, JAIL, forward_from=HOST)` on `verify`. `cli.py` runs a
command where it is declared, forwards it from `forward_from` into the
project container as `podman exec … claude-sandbox VERB ARGS` (so it runs
the version installed there), and refuses it anywhere else. That
declaration replaces the bash launcher's hand-kept list of forwarded verbs.

| Where | What it holds |
|---|---|
| `cli.py` | The command table, the parser for each context, and the dispatch |
| `host/options.py` | The launcher's own options (`--recreate`, `--mount`, ...), parsed as the bash parses them |
| `host/launcher.py` | The engine calls: create, reuse, the session, `clean`, the version warning |
| `host/commands.py` | The host commands: `claude`, `codex`, `pi`, `shell`, `clean` |
| `helpers/commands.py` | The helpers and where they run: `gh-auth`, `glab-auth`, `verify`, `pi-local`, `doctor`, `version`, `update`, `install`, `help` |
| `helpers/auth.py`, `doctor.py`, `pi_local.py` | The longer helpers |

The bash stays the default. To try the Python CLI from a checkout, run the
wheel's entry point with the opt-in:

```bash
CLAUDE_SANDBOX_IMPL=python uv run claude-sandbox --help
CLAUDE_SANDBOX_IMPL=python uvx --from dist/claude_sandbox-*.whl claude-sandbox
```

`install` still runs the bash installer either way. `uv run python -m
claude_sandbox` runs the CLI directly, without the front door's
environment. To try the helpers on a host, set
`CLAUDE_SANDBOX_CONTEXT=container`, the seam the tests use.

`tests/launcher.sh` and `tests/doctor.sh` run against the Python CLI as
well as the bash: `tests/python/test_bash_suites.py` points them at a
wrapper through `CLAUDE_SANDBOX_TEST_LAUNCHER` and `CLAUDE_SANDBOX_TEST_CLI`.

## Build the docs locally

The isolated docs dependencies are listed in `docs/requirements.txt`.
For a live preview:

```bash
uvx --with-requirements docs/requirements.txt --from sphinx-autobuild \
  sphinx-autobuild docs build/html --port 8000
```

For a build with warnings treated as errors:

```bash
uvx --with-requirements docs/requirements.txt --from sphinx \
  sphinx-build -b html -W --keep-going docs build/html
```

Open `build/html/index.html`. For layout or CSS changes, have the user review
the preview in a real browser at multiple widths before merging.
