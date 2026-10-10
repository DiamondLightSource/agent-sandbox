---
name: claude-sandbox-user
description: >-
  How to work inside a claude-sandbox session: what the jail lets you write,
  what the outer container can do that you cannot, and how to ask the user
  for it. Surface when a command fails with a read-only filesystem, a missing
  package, a gh or glab authentication error, or a permission the jail
  refuses; when the user mentions claude-sandbox, recreate, the outer
  container, or "claude-sandbox shell"; before telling the user to run
  gh auth login or apt-get; and before any attempt to install software or
  fetch binaries to work around a missing tool.
---

# claude-sandbox-user

You are running inside a bubblewrap jail, inside a container that
claude-sandbox created for this project. The user reaches the container
from the host with `claude-sandbox shell`; you never leave the jail, and
that is by design. This skill tells you what the jail gives you and how to
ask for the rest. The published docs cover configuration and the threat
model: https://diamondlightsource.github.io/claude-sandbox/

## Where you are

`IS_SANDBOX=1` in your environment confirms the jail. Inside it:

| Path | You can | Notes |
|---|---|---|
| The project directory | read, write | The only workspace path bound read-write by default. Extra paths come from `allow-write` lines in `/etc/claude-sandbox.conf`, which the user edits from the outer container. |
| `/cache` | read, write | A named volume shared by every project container on this host. Holds the uv cache and the project venv at `/cache/venv-for<project path>`. Survives `--recreate` and `clean`. The outer container sees it too. |
| `/tmp` and `/root` | read, write | Private tmpfs for this session only. Nothing outside the jail can read them, and they are gone when the session ends. |
| `/`, `/usr`, `/etc`, `/opt` | read only | `apt-get` fails here and always will. |
| `~/.claude`, `~/.claude.json` | read, write | Agent state and memory, shared with the host across container recreation. |
| `~/.config/gh`, `~/.config/glab-cli` | read, write | Forge token stores, filled by the user from the outer container. |

The network is open to the internet and DNS but blackholes private
address ranges unless the user has added `allow-ip` entries. Nested
namespaces are refused, so tools that want their own sandbox (Electron,
some test runners) need a no-sandbox flag.

## What only the outer container can do

The outer container runs unsandboxed as root. The user opens it from the
host, in the project directory, with:

```sh
claude-sandbox shell
```

In a devcontainer the user's ordinary terminal is already that shell. From
there they can:

- install packages with `apt-get`, which the jail sees at once with no
  restart;
- authenticate with a forge, see below;
- run `claude-sandbox verify` to check the sandbox;
- edit `/etc/claude-sandbox.conf` to widen writable paths or allow an
  internal IP;
- start VS Code with the `code` shim, when the vscode-headless install
  script has written it.

Anything installed there, and every forge login, is lost when the user runs
`claude-sandbox --recreate` or `claude-sandbox clean`. Project files,
`/cache` and agent settings survive. A tool that was present last session
and is gone now usually means the container was recreated. That is normal,
not a fault to work around.

## Asking the user

When you need one of those things, stop and ask. Give the exact command,
say it runs outside the sandbox, and say what it will change. A script the
user must run has to live somewhere both sides see. For shipped skills,
give the installed path under `/usr/libexec/claude-sandbox/skills/`;
it already exists in the outer container and needs no copy. For scripts
that exist only inside the jail, copy them to `/cache` (or the workspace
if `/cache` is not writable). A path under `~/.claude/skills` is useless
to the user because those mounts exist only inside the jail. Do not
extend such a script beyond package installs.

When the user is away, do the parts that need nothing from the outer
container, then report what is left and the command to run. Do not
substitute your own workaround for the ask.

## Forge authentication

Never tell the user to run `gh auth login` or `glab auth login`. The
sandbox has its own helpers, run from the outer shell:

```sh
claude-sandbox gh-auth
claude-sandbox glab-auth            # gitlab.diamond.ac.uk
claude-sandbox glab-auth gitlab.example.com
```

They prompt for a project-scoped token and store it where the jail can see
it immediately, so you need no restart. If `gh auth status` reports no
login, ask for that command.

A repository in another organisation, or one gh's login cannot push to,
needs a further token: ask the user for `claude-sandbox gh-auth --add NAME`.
git then picks, per repository, the first stored token GitHub lets push,
and `gh` in the jail makes the same choice for the repository it works on
(push rights are what is tested, not pull-request rights). `claude-sandbox gh-auth --status` lists the choices; a wrong
one is cleared with `claude-sandbox gh-auth --forget OWNER/REPO`. Do not set
`GH_TOKEN` yourself or read the token files.

Git inside the jail is already configured. `/etc/claude-gitconfig` sets
the gh and glab credential helpers for HTTPS and rewrites SSH remotes to
HTTPS. Use `gh` for GitHub operations and plain `git push` over HTTPS for
pushes. Never look for SSH keys; there are none, on purpose. A push that
fails with an authentication error means the token is missing or lacks
Contents write permission, and the fix is the helper above, not a token
pasted into a URL.

## Installing software yourself

Inside the jail you can install into the project venv with `uv` or `pip`,
and `npm` into the project. Those are the right tools for project
dependencies and need no one else.

Fetching `.deb` files with `curl`, unpacking them under `/cache` and running
the binaries with `LD_LIBRARY_PATH` also works, and the sandbox permits it:
the result is confined exactly as you are. Treat it as a last resort that
the user must approve first, because it is slow, breaks when archive
versions drift, and hides from the user what is running. Prefer asking for
an apt install in the outer shell. Never present such a stack as an
installation, and never use it to avoid the ask.

## Checking the sandbox

If you or the user doubt the isolation, run the installed battery from
inside the jail:

```sh
claude-sandbox verify
```

Every line should read `PASS`. Quick checks that should always hold:
`touch /usr/bin/probe` fails with a read-only error, and no SSH key exists
under `~/.ssh`. If a check fails, tell the user and stop; do not try to
repair the sandbox from inside it.

## Related skills

- `browser-testing` installs optional Playwright/Chromium tooling through the
  outer container, then tests web interfaces inside the sandbox.
- `verify-sandbox` runs the installed battery followed by an agent-driven
  adversarial audit when the user requests a full isolation review.
- `vscode-headless` runs VS Code inside the jail on a virtual display. Its
  install script is the standard example of something the user runs in the
  outer shell.
