(adr-outer-path-guard)=

# 27. Guard the outer PATH against executables a session leaves behind

Date: 2026-10-06

## Status

Accepted. Implemented in 5.0.0 (2026-10-07).

Builds on {ref}`ADR 9 <adr-shadow-on-path>` (the shadow on PATH, Invariant 1)
and {ref}`ADR 26 <adr-python-implementation>` (the Python shadow). Applies to
the Python shadow only.

## Context

The sandbox contains the agent while it runs. It does not contain code the
agent writes that something outside the sandbox runs later. The workspace,
the project venv and the tool caches are writable from the jail on purpose,
and the user, VS Code and the container entrypoint run things from them
outside it.

The sharpest form of that is PATH. In the published image and in DLS
python-copier devcontainers, the container's PATH starts with a project
venv's `bin` (`/opt/venv/bin`, a link to `/cache/venv/bin`, or
`/cache/venv-for<workspace>/bin`), ahead of `/usr/local/bin` and the system
directories, and the shipped conf has `allow-write = /cache` so that `uv`
works in the jail. An executable a session leaves in that `bin` therefore
shadows a system command for every outer shell, VS Code's git panel and the
entrypoint, during the session (the user runs `git push` in another
terminal) and after it. If it is named `claude`, it shadows the sandbox
itself, and the next plain `claude` runs outside the jail (Invariant 1).

Git hooks are the other quiet route: `.git/hooks` is in the writable
workspace, a hook never appears in a diff, and the next outer `git commit`
or `git push` runs it.

Protecting a list of names is whack-a-mole: `git`, `python3`, `ls`, `sudo`
and every other command on PATH is a target. What matters is that a session
added an executable that a later PATH directory also has.

## Decision

Quarantine executables a session adds ahead of system commands on PATH, and
new git hooks, while the session runs; warn in outer shells.

- **Entry-point mount guard.** For every writable directory that precedes
  `/usr/local/bin` on the launching PATH and exists at launch, `bwrap.py`
  read-only binds `/dev/null` over `claude`, `codex`, `pi` and
  `claude-sandbox`, after the read-write binds. Inside the jail those names
  cannot be created, written, replaced or removed. The shadow refuses to
  launch when one of them is anything else there, and battery check 22
  asserts the binds.
- **A live watcher in the launcher** (`watch.py`). Outside the jail, for as
  long as the session runs, it watches every writable directory (inside a
  read-write bind) that precedes the last system command directory
  (`/usr/sbin`, `/usr/bin`, `/sbin`, `/bin`) on PATH, and the workspace's git
  hooks directory (worktrees included). inotify, through the standard
  library's `ctypes`, wakes it at once; a pass every second finds directories
  that appear later and is all there is where inotify is unavailable.
  - In a PATH directory, an executable, or a link to one, whose name a later
    PATH directory also has is a shadow. The watcher clears its execute bits
    through a descriptor opened without following links; a link is removed
    and its target recorded. A re-`chmod +x` is quarantined again.
  - In the hooks directory, any new or changed executable but `*.sample`
    loses its execute bits. A directory the repository's `core.hooksPath`
    names is watched the same way when it lies inside a read-write root;
    the watcher reads the setting with `git config --get` (git from the fixed
    tool path, a scrubbed environment, fsmonitor off), and a change of the
    setting during the session is an alert of its own.
  - What was present and unchanged when the session started is left alone,
    so the venv's own `python3` stays. A changed file is judged again.
  - One allowance: a link named `python`, `python3` or `python3.N` whose
    final target is an executable named `python*` outside every read-write
    root of the jail is not a shadow. That is what `uv venv` makes against
    `/usr/bin/python3` or a root-owned uv-managed Python, so a session can
    recreate the venv. A regular file of that name, a link into anything the
    session can write, and every other name (`pip`, console scripts) are
    judged as usual.
  - A second allowance, checked rather than assumed (added 2026-10-07):
    `tox-uv` and other projects that depend on PyPI's `uv` package put `uv`
    and `uvx` in the venv's `bin`, ahead of `/usr/bin/uv`, so every `uv
    sync` that rebuilt the venv would alert. A file of either name is
    quarantined at once like any shadow, but its alert is held while a
    thread compares it with the binary in the official `uv` wheel on PyPI.
    The version comes from the venv's `uv-*.dist-info`, only a hint the
    session can write. The watcher fetches `pypi.org/pypi/uv/<version>/json`
    over HTTPS with `urllib`, downloads this machine's wheel (architecture
    and libc) into the state directory, checks it against PyPI's sha256,
    and caches the sha256 of its `uv` and `uvx` by the wheel's digest, so a
    version is downloaded once. A byte-identical file gets its execute bits
    back, through a checked directory descriptor and without following
    links, becomes the baseline (for the session and the next launch), and
    is logged in `/run/claude-sandbox/verified`, not in the alerts. A
    mismatch, no network, a check still running when the session ends, or
    any error: it stays quarantined and alerts as before. The two names and
    the PyPI rule are constants in `watch.py`; nothing the session can write
    widens them.
- **A scan at launch.** Before the agent starts, each watched directory is
  compared with a baseline the previous launch kept under
  `/run/claude-sandbox` (root-owned); a shadow that is not in it is
  quarantined and reported as a launch warning, so the pause shows it. The
  first launch only records the baseline.
- **With the jail off** the shadow execs `script(1)`, so a forked child
  watches instead. It leaves the terminal's session and stops within a
  second of the launch ending. When the shadow is PID 1 (the image's
  default command) the orphaned watcher would be reparented to `script`
  itself, so there `script` runs as a child and the watcher is a thread,
  as with the jail on.
- **Surfacing.** Each action is a line in `/run/claude-sandbox/alerts`
  (`/tmp/claude-sandbox/alerts` if `/run` cannot be written), which
  `bwrap.py` masks inside the jail. A root-owned
  `/etc/profile.d/claude-sandbox-alerts.sh`, sourced from the system bash
  and zsh rc files, prints new alerts in red at every outer prompt; with
  none it costs two `test -s` builtins. The jailed launch prints the same
  summary when the session ends. `claude-sandbox alerts` lists them, and
  `--clear` empties the list and takes what the directories hold now as the
  new baseline, so a file the user has reviewed and restored stands.
  `claude-sandbox doctor` reports the entry points and any alerts.

Options rejected:

- **Reorder PATH only.** Putting the system directories first can't be
  enforced in a guest devcontainer without editing its `devcontainer.json`,
  which the installer refuses to do, and the venv first on PATH is what the
  project expects.
- **Narrow `allow-write`.** Dropping `/cache` breaks `uv sync`, `uv add` and
  the shared uv cache in the jail.
- **A protected-tool list.** Whack-a-mole, as above; kept only for the
  sandbox's own four names, where a mount can stop the write altogether.
- **Detect only at session exit.** Too late for an outer shell the user is
  using while the session runs.

## Consequences

- An in-session install of a console script that shadows a system tool (a
  package whose script is also in `/usr/bin`) is quarantined. Restore it and
  run `claude-sandbox alerts --clear`, or install outside the jail.
- The interpreter allowance holds only where the interpreter is out of the
  session's reach. In the published image uv's Pythons are under
  `/usr/libexec/claude-sandbox/python` (the sandbox's own, root-owned and
  not bound read-write), and in a DLS
  python-copier devcontainer `uv venv` links to the system `/usr/bin/python3`.
  But a copier project whose `requires-python` the system Python does not
  meet gets a uv-managed Python in uv's default `~/.local/share/uv/python`,
  which the jail can write (`~/.local/share` is bound read-write). There a
  venv the session recreates loses its `python` links to quarantine.
  Recreate such a venv outside the jail. Trusting that directory would trust
  an interpreter the session can rewrite, so it is not done; binding it
  read-only into the jail would let the allowance apply.
- The uv allowance trusts PyPI and its TLS: a uv release, or the `uv`
  project's PyPI account, that was itself compromised would be let stand.
  It makes the watcher reach `pypi.org` and `files.pythonhosted.org` from
  outside the jail, and lets a session choose which uv release it
  downloads (one ~20 MB wheel per version, then cached). Between the hash
  and the `chmod` the session could rewrite the file in place; the file is
  left alone if its size or ctime moved while it was read, and any later
  write is judged again, but a write inside the same timestamp tick is not
  seen until then. Where the check cannot complete, as offline, uv in the
  venv alerts as before.
- The entry-point binds leave empty, non-executable files named `claude`,
  `codex`, `pi` and `claude-sandbox` in the guarded directories, and a
  session cannot remove the venv's `bin` while they are mounted.
- The system rc files gain a hook, installed and removed by the installer
  between markers.
- inotify is Linux only, through `ctypes`; elsewhere, or if `_ctypes` is
  missing, the watcher polls every second. Phase 4's interpreter pruning
  must keep `_ctypes`.
- Only the Python shadow does this; the bash shadow is being retired.
- Not covered: a directory that does not exist at launch has no mount guard
  (the watcher and the next launch's checks still apply); a change of
  `core.hooksPath` between sessions (only one during a session alerts);
  `.git/config` settings other than `core.hooksPath`; and code in the
  workspace, the venv's `site-packages` or the caches that the user runs
  outside the sandbox. Review that like any contribution.
- The threat model gains a section, "What a session leaves behind".
