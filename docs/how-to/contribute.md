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

Nothing calls these modules yet. The installer, the shadow and the
`uvx claude-sandbox` front door still run the bash, so these modules don't
change sandbox behaviour. They use the standard library only, and
`tests/python/test_wheel.py` fails if one of them imports anything else.
`bwrap.py` reads the environment only from the mapping it is given, and it
reads the filesystem only through an injectable probe that tests whether a
path exists.

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
it. A failure reports the first argv index where the two differ. The harness
is removed with the bash in phase 5.

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
