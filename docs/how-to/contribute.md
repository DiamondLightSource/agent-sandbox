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

The sandbox implementation is Bash. Python is limited to the docs toolchain,
the test socket fixture, and the PyPI entry point that bundles and executes
the Bash files. See `CLAUDE.md` for the project boundaries.
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

For packaging changes:

```bash
uv build --wheel -o dist
```

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
