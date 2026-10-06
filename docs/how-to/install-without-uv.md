# Install without uv

Prefer the [PyPI quick start](../tutorials/getting-started.md). Use these
fallbacks when uv is unavailable.

## Host launcher

<!-- TODO(phase5): confirm that pipx and pip installs of the wheel run the launcher as uvx does -->

The launcher is an ordinary Python package with no dependencies. With
Python 3.11 or later on the host, install it with pipx:

```bash
pipx install claude-sandbox==5.0.0
cd ~/src/my-project
claude-sandbox
```

Or into a venv of its own:

```bash
python3 -m venv ~/.local/share/claude-sandbox-venv
~/.local/share/claude-sandbox-venv/bin/pip install claude-sandbox==5.0.0
cd ~/src/my-project
~/.local/share/claude-sandbox-venv/bin/claude-sandbox
```

The package version selects the matching image, as it does with uv.
Rootless Podman and the other
[host prerequisites](../tutorials/getting-started.md#1-install-on-your-host)
still apply. Upgrade with `pipx upgrade claude-sandbox` (or `pip install
--upgrade` in the venv), then `claude-sandbox --recreate` in each project.

## Install into a devcontainer

Inside a Debian/Ubuntu devcontainer, as root:

```bash
CSBX_DIR="$(mktemp -d)"
git clone --depth 1 --branch 5.0.0 https://github.com/DiamondLightSource/claude-sandbox "$CSBX_DIR"
bash "$CSBX_DIR/install" --here
claude
```

<!-- TODO(phase5): confirm against the bootstrap (issue #72 phase 4, second part) -->
`install` is a short bootstrap: it fetches a pinned uv when the container
has none, then runs the same Python installer as `uvx claude-sandbox
install`. The container therefore needs network access during installation,
as it does to fetch the agents.

`--here` installs the chosen checkout. Without it, the installer attempts
to select the newest release and refuses a pinned or modified checkout.
The installed sandbox does not depend on the temporary clone afterwards.

Use the same block in `postCreate.sh` for a pinned team install.
The [team guide](sandbox-a-team-devcontainer.md) covers the tun device,
configuration and verification.
