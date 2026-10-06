# Contribute to claude-sandbox

Report bugs and propose changes through
[GitHub issues](https://github.com/DiamondLightSource/claude-sandbox/issues).
Use [Discussions](https://github.com/DiamondLightSource/claude-sandbox/discussions)
for open-ended questions. Agree the scope of large changes before implementation.

## Development setup

Clone the repository and open its devcontainer for work on the sandbox itself.
To install the checkout you are editing, in a container terminal (not inside
an agent session), as root:

```bash
./install --here
```

`install` is a short Bash bootstrap: it chooses the revision, fetches a
pinned uv if needed, and hands over to the Python installer in
`src/claude_sandbox/installer/`. Without `--here`, it selects a release and
refuses a pinned, non-default or modified checkout.
<!-- TODO(phase5): confirm the bootstrap's behaviour once it is written (issue #72 phase 4, second part) -->

The sandbox is a Python package, `src/claude_sandbox/`, with a root
`pyproject.toml` ({ref}`ADR 26 <adr-python-implementation>`). See
`CLAUDE.md` for the project boundaries and the Python guardrails.
The repository's `.claude/` holds the skills, commands and hooks for developing
this repo; only the top-level `skills/` tree ships to users (see the
`claude-sandbox-shipped-skills` skill).

## Validation

The Python tests, linters and type checker run from the uv development
environment. The development tools (pytest, ruff, pyright) are pinned in
`uv.lock`:

```bash
uv sync
uv run pytest --cov
uv run ruff check
uv run ruff format --check
uv run pyright
```

pytest collects `tests/python/` only. CI requires at least 95% branch
coverage of `src/claude_sandbox/`, and pyright runs in strict mode: fix the
code rather than relaxing the check.

The end-to-end suites are shell scripts that test the installed sandbox
from outside, as a user would. The CI workflow is the complete list; the
core installation and launcher checks include:

<!-- TODO(phase5): confirm this list against ci.yml after the bash is deleted -->

```bash
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

Build the wheel with:

```bash
uv build --wheel -o dist
```

## The Python package

The package uses the standard library only and has no runtime
dependencies. `tests/python/test_wheel.py` imports every module and fails if
one imports anything else. Keep it that way: helpers that handle tokens or
run as root share the package with the launch path.

### The audit core

Three modules hold the security-critical code. Each is meant to be read top
to bottom; don't spread them across more modules.

| Module | What it holds |
|---|---|
| `bwrap.py` | A pure function from agent profile, configuration and environment to the bwrap argv. The only place that adds a mount or an environment variable to it |
| `jail.py` | The egress jail: the namespace holder, `pasta`, DNS forwarding and the loopback relays |
| `shadow.py` | One launch, top to bottom: the recursion guard, the refusals, the configuration and Git config, the directories the binds need, the warnings, and the `script(1)` wrap around the bwrap argv |

Around them:

| Module | What it holds |
|---|---|
| `__main__.py` | Dispatches the shim's `_shadow` call and the jail's `_jail_holder` before the CLI is imported |
| `profiles.py` | One frozen dataclass per agent: the real binary, home paths, injected flags, `--chrome` stripping |
| `config.py` | The `/etc/claude-sandbox.conf` parser and the local-port and callback-port checks |
| `gitconfig.py` | The jail's Git config, returned as text |
| `watch.py` | The PATH watcher (ADR 27) |
| `tools.py` | The fixed system directories the launch path runs its tools from |
| `context.py`, `cli.py` | Where the CLI runs, and the command table |
| `host/` | The host launcher: options, engine calls, `clean` |
| `helpers/` | The in-container helpers: `gh-auth`, `glab-auth`, `verify`, `pi-local`, `doctor`, `alerts`, `version`, `update` |
| `installer/` | The installer's steps and the interpreter provisioning |

`bwrap.py` reads the environment only from the mapping it is given. It
reads the filesystem only through an injectable probe, which can test what
a path is, resolve it the way `realpath -e` does, and list the matches for
a glob such as `/dev/nvidia*`. Its tests are plain pytest on that function.
bubblewrap applies its operations in order, so keep new mounts in the right
section: a mask must follow the bind it covers.

### The launch path

`/usr/local/bin/claude`, `codex` and `pi` are one file,
`.devcontainer/claude-sandbox/claude-shim`:

```bash
#!/bin/bash
# /usr/local/bin/claude (and codex, pi): hand off to the root-owned install.
exec /usr/libexec/claude-sandbox/venv/bin/python -I -m claude_sandbox _shadow "${0##*/}" -- "$@"
```

The shim names the root-owned interpreter by absolute path and runs it with
`-I`. `__main__.py` sees `_shadow` and calls `shadow.main` before anything
else is imported. Never add a `#!/usr/bin/env python3`, an interpreter
found through `PATH`, or a launch from uv's cache: `PATH` in the published
image starts with a directory on the writable `/cache` volume, and
`~/.cache` is writable from the jail. `tests/python/test_hijack.py` plants
`sitecustomize.py`, `usercustomize.py`, a `.pth` file, a `PYTHONPATH` and a
fake `python` earlier on `PATH`, launches, and asserts that none ran.

The same rule covers the tools the launch path runs. `tools.find_tool`
looks only in `/usr/bin`, `/bin`, `/usr/sbin` and `/sbin`, and returns an
absolute path; `script`, `bwrap`, `git`, `unshare`, `pasta`, `ip`, `ss` and
`socat` all come from it. A missing tool is a refusal, never a fallback to
`PATH`.

`shadow.py` handles interrupts with `try`/`finally` and signal handlers:
INT, TERM and HUP unwind, remove what the launch created, and then the
process dies by the same signal so the caller's shell sees it. A signal
that was ignored on entry stays ignored. `tests/python/test_terminal.py`
types Ctrl-C at a real pseudo-terminal while the agent runs;
`tests/python/test_jail_netns.py` does the same through the egress jail.

### The egress jail

The shadow calls `jail.stage_dns` before it builds the bwrap argv, because
`bwrap.py` binds the staged resolver, and then hands the `script(1)`
command to `jail.launch`, which runs it in the jail and exits with its
status. The namespace holder re-enters the package:
`unshare -rn <sys.executable> -I -m claude_sandbox _jail_holder -- COMMAND`,
so it runs the same root-owned interpreter, in isolated mode, that launched
it, and `__main__.py` dispatches `_jail_holder` before importing anything
else. The holder inherits stdin and the process group, so it, `script` and
`bwrap` stay in the terminal's foreground group.

Every side effect in `jail.py` goes through an `Ops` object, so the unit
tests in `tests/python/test_jail.py` replace it and check the argv, the
fail-closed paths, the cleanup and the exit status after each signal. Real
namespaces need `/dev/net/tun` and unprivileged user namespaces, so
`tests/python/test_jail_netns.py` skips elsewhere. Run it in this
repository's image with `tests/jail_python.sh`; the comment at its top gives
the `podman run` command.

### One CLI: host, container and jail

`claude-sandbox` is one argparse command on both sides of the container
boundary. `context.py` decides once where the process runs:

- **JAIL** when `IS_SANDBOX=1`, which the shadow sets inside an agent
  session;
- **CONTAINER** when `/run/.containerenv` or `/.dockerenv` exists;
- **HOST** otherwise, or when `CLAUDE_SANDBOX_NESTED=1` (an engine inside a
  container).

Each command declares where it runs, for example
`@requires(CONTAINER, JAIL, forward_from=HOST)` on `verify`. `cli.py` runs a
command where it is declared, forwards it from `forward_from` into the
project container as `podman exec … /usr/local/bin/claude-sandbox VERB
ARGS` (so it runs the version installed there), and refuses it anywhere
else. Add a command by writing its function in `host/` or `helpers/` with
its `@requires` and listing it in `cli.COMMANDS`; there is no separate list
of verbs to forward.

No program the CLI runs in the container or the jail is found through
`PATH`. The agents and `claude-sandbox` are named by their absolute paths
under `/usr/local/bin`, the `shell` verb starts from `/bin/sh`, and `gh`,
`glab` and `git` come from `tools.find_tool`. Only the engine on the host
(`podman` or `docker`) is found on the user's `PATH`.

On the host the launcher's own options (`--recreate`, `--mount`, ...) come
first and are parsed by hand in `host/options.py`: the first word that is
not one of them ends them, and everything after it goes to the command or
the agent untouched, so `claude-sandbox --resume` is `claude --resume`.

`CLAUDE_SANDBOX_CONTEXT=host|container` is a test seam only: it lets the
helper suites run on a host. It never overrides the jail, and `update`
ignores it: `update` changes the system only where `/run/.containerenv` or
`/.dockerenv` exists, or with `CLAUDE_SANDBOX_HOST_INSTALL=1`, as `install`
does.

### The installer

`install` (the clone route) and `uvx claude-sandbox install` both end in
`src/claude_sandbox/installer/`, run as root.
<!-- TODO(phase5): confirm against the wired-in installer (issue #72 phase 4, second part) -->

- Each step in `steps.py` is a `plan_*` function that reads the filesystem
  and returns actions, and `actions.apply` performs them, so a second
  install writes nothing. The steps take an install prefix and a user home
  (`INSTALL_PREFIX`, `INSTALL_USER_HOME`), so tests run them on a temporary
  tree. Files are created and moved without following symlinks, under
  umask 022.
- `jsonfile.py` reads and writes the JSON settings files in place of `jq`.
  The managed-settings step merges into
  `/etc/claude-code/managed-settings.json`, keeping existing administrator
  policy, and warns and skips a file it cannot parse or write back.
- `provision.py` installs the pinned CPython and the venv under
  `/usr/libexec/claude-sandbox/` with uv. uv's cache is not used, the venv
  is pinned to the resolved patch directory rather than uv's minor-version
  symlink, and the package is installed with `--no-deps`. It removes uv's
  `_virtualenv.pth` and the venv's console scripts, prunes the interpreter
  (Tcl/Tk, idlelib, pip, ensurepip, headers, tests, and the duplicate
  `libpython` when nothing links it) to about 55 MB, byte-compiles it,
  makes it root-owned and not group- or world-writable, and then checks that
  every module of the package imports under `-I`.
- The installer runs nothing found through `PATH`: `git` and uv are named by
  absolute path.

### The entry-point guard and the PATH watcher

{ref}`ADR 27 <adr-outer-path-guard>` guards the outer `PATH` against
executables a session leaves behind. Two parts, both in the package:

- `bwrap.py` adds the entry-point mount guard: read-only binds of
  `/dev/null` over `claude`, `codex`, `pi` and `claude-sandbox` in each
  writable directory ahead of `/usr/local/bin`, after the read-write binds,
  and it masks the watcher's state directory, `/run/claude-sandbox`. It
  also works out which directories the watcher watches
  (`watched_path_dirs`), so the two agree on what is writable. `shadow.py`
  refuses to launch when an entry-point name there is anything but the
  empty file the guard leaves.
- `watch.py` is the watcher. `shadow.py` runs it in a thread around the
  jailed launch, and forks it as a child when the jail is off (the shadow
  then execs `script(1)`). It uses inotify through `ctypes` with a pass
  every second as a fallback, quarantines by clearing execute bits through
  a descriptor opened without following links, and records each action
  under the state directory. Interpreter pruning must keep `_ctypes`.

`claude-sandbox alerts` (`helpers/commands.py`) lists and clears the
alerts, and refuses inside the jail; `claude-sandbox doctor` warns about
them and about entry points ahead of the shadow. The installer places the
prompt hook `/etc/profile.d/claude-sandbox-alerts.sh` and sources it from
the system bash and zsh rc files.

`tests/python/test_watch.py` runs the watcher against real directories.
In this repository's image, `tests/entry_guard.sh` and `tests/watch_e2e.sh`
test the mount guard and the watcher end to end, and battery check 22
asserts the binds from inside the jail.

### The Python installer

`src/claude_sandbox/installer/` is the Python port of `install.sh`. The bash
installer stays the default; `CLAUDE_SANDBOX_IMPL=python` selects the Python
one, for `./install`, `uvx claude-sandbox install`, the in-container
`claude-sandbox update` of a Python install, and the image
(`--build-arg CLAUDE_SANDBOX_IMPL=python`). With the opt-in, `install.sh` is
only a bootstrap:

1. It fetches uv at the version pinned in `provision.py` (`UV_VERSION`) into
   `/usr/libexec/claude-sandbox/uv`, checked against the pinned SHA-256, unless a
   root-owned copy of that version is already there.
2. It has uv install the pinned CPython (`PYTHON_VERSION`) under
   `/usr/libexec/claude-sandbox/python`, never in uv's cache, and runs
   `provision.py` with it. Provisioning builds the venv, pinned to the patch
   directory, copies the `claude_sandbox` package into it, prunes the
   interpreter to about 56 MB, byte-compiles it, makes it root-owned and
   checks the imports with `-I`.
3. It execs `venv/bin/python -I -m claude_sandbox.installer`, which runs
   `install.sh`'s steps in the same order and places the shims as
   `/usr/local/bin/claude`, `codex`, `pi` and `claude-sandbox`.

A default install removes what an opt-in left. The published image's
entrypoint follows whichever installer built the image.

Each file step in `steps.py` is a `plan_*` function that reads the filesystem
and returns actions, and `actions.apply` performs them, so a second install
writes nothing. `system.py` holds the steps that run tools (apt, the probes,
the agent downloads), by absolute path through `tools.find_tool`.
`tests/python/test_installer_parity.py` runs each bash step, and the whole
`main()` with its summary, against its port on the same temporary tree and
requires identical files, modes, warnings and output; the system steps are
unit-tested against stand-ins. `tests/smoke.sh` and `tests/install_modes.sh`
run against either installer:

```bash
CLAUDE_SANDBOX_IMPL=python CLAUDE_SANDBOX_SMOKE=1 bash tests/smoke.sh
```

A smoke run downloads nothing, so it starts the Python installer from the
tree with the interpreter named in `CLAUDE_SANDBOX_SMOKE_PYTHON` (the tests
default it to `python3`).

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
