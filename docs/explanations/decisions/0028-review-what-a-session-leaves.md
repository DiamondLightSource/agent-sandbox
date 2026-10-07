(adr-review-what-a-session-leaves)=

# 28. Keep the venv last on PATH and review what a session leaves

Date: 2026-10-07

## Status

Accepted. Supersedes {ref}`ADR 27 <adr-outer-path-guard>` except for its
entry-point mount guard.

## Context

The sandbox contains the agent while it runs, not the code it leaves for
something outside the sandbox to run later: venv packages and `.pth` files
(`uv run`, `pytest`), Git hooks (`git commit`, `pre-commit`), Makefiles and
VS Code tasks. The workspace, the venv and the caches are writable from the
jail on purpose.

ADR 27 covered one slice of this, executables in a venv `bin` first on the
outer PATH that shadow a system command, plus Git hooks, with a watcher
outside the jail: inotify, quarantine, baselines, alerts and a shell prompt
hook. That was about 650 lines in the audit core for one route among many.
An end-of-session report instead would print in a terminal that often closes
with the session (VS Code extension launches).

## Decision

- **The venv goes last on PATH.** The image sets `PATH=$PATH:/opt/venv/bin`,
  as the jail already does, so nothing a session leaves there can shadow a
  system command. Devcontainers that put their venv first should append it.
- **The entry-point mount guard stays.** It is a few lines in `bwrap.py` and
  protects Invariant 1 where a devcontainer still puts a writable directory
  first.
- **The rest is reviewed, not watched.** Code a session leaves is not an
  escape on its own; it is the same exposure as an unreviewed contribution.
  The threat model says to review a session's changes, and `.git/hooks`,
  before running project code outside the jail.
- **Removed:** `watch.py`, the `/run/claude-sandbox` state directory, the
  alerts prompt hook, `claude-sandbox alerts`, doctor's alerts check and the
  jail-off PID 1 special case. The watcher shipped only in the 5.0.0 betas,
  so there is no upgrade step.

## Consequences

- In the image, the venv's `python`, `pytest` and console scripts come after
  any system command of the same name; `uv run` is unchanged.
- A devcontainer that keeps a writable venv first stays exposed to
  shadowing for every name but the sandbox's own four.
- Nothing tells the user what a session left; they review its changes.
