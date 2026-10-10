# Install without uv

Prefer the [PyPI quick start](../tutorials/getting-started.md). Use these
fallbacks when uv is unavailable.

## Host launcher

The launcher is an ordinary Python package with no dependencies, and pipx
or pip installs the same `claude-sandbox` command that uvx runs. With
Python 3.11 or later on the host, install it with pipx:

```bash
pipx install claude-sandbox==5.1.1
cd ~/src/my-project
claude-sandbox
```

Or into a venv of its own:

```bash
python3 -m venv ~/.local/share/claude-sandbox-venv
~/.local/share/claude-sandbox-venv/bin/pip install claude-sandbox==5.1.1
cd ~/src/my-project
~/.local/share/claude-sandbox-venv/bin/claude-sandbox
```

The package version selects the matching image, as it does with uv.
Rootless Podman and the other
[host prerequisites](../tutorials/getting-started.md#1-install-on-your-host)
still apply. Upgrade with `pipx upgrade claude-sandbox` (or `pip install
--upgrade` in the venv), then `claude-sandbox --recreate` in each project.
When the image is newer than the launcher, the launcher says so and prints
the `pipx` command.

## Install into a devcontainer

Inside a Debian/Ubuntu devcontainer, as root:

```bash
CSBX_DIR="$(mktemp -d)"
git clone --depth 1 --branch 5.1.1 https://github.com/DiamondLightSource/claude-sandbox "$CSBX_DIR"
bash "$CSBX_DIR/install" --here
claude
```

`install` is a short bootstrap: it fetches a pinned uv (checked against a
pinned SHA-256, and kept under `/usr/libexec/claude-sandbox/uv`), has it
install the sandbox's own pinned Python, then runs the same Python installer
as `uvx claude-sandbox install`. The container does not need uv or Python
beforehand, only `apt-get`; it needs network access during installation, as
it does to fetch the agents.

`--here` installs the chosen checkout. Without it, the installer attempts
to select the newest release and refuses a pinned or modified checkout.
Add `--minimal` (`bash "$CSBX_DIR/install" --here --minimal`) to install for
Claude alone, without Codex or Pi; see
[Claude-only installation](../reference/whats-installed.md#claude-only-installation).
The installed sandbox does not depend on the temporary clone afterwards.

Use the same block in `postCreate.sh` for a pinned team install.
The [team guide](sandbox-a-team-devcontainer.md) covers the tun device,
configuration and verification.
