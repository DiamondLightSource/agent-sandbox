(adr-report-what-a-session-can-see)=

# 29. Report what a session's jail shows of a host path

Date: 2026-10-10

## Status

Accepted

## Context

The VS Code extension (claude-sandbox-vscode) sends Claude the text of the
user's editor selection. It sent it only for files in the folder Claude
started in, because a selection's text reaches Claude without the jail being
asked: highlighting a token in `~/.config/gh/hosts.yml` would otherwise put
it in the session's context, from where egress can carry it out. That also
refused files Claude can read anyway, such as a library checked out beside
the project in a multi-root window (claude-sandbox-vscode issue 19), and it
was wrong the other way when `workspace-root` or `allow-write` make more than
that one folder writable.

The extension cannot work out what the jail shows by itself: that depends on
the conf, the environment, `$HOME`'s bind-backs and the masks, all of which
`bwrap.py` decides, and a copy of those rules would drift from them.

## Decision

- **`python -I -m claude_sandbox _scope CWD -- PATH...`** prints, for each
  path, whether a `claude` session started in CWD could read it and write
  it. It is an internal entry, dispatched beside `_shadow` and stdlib-only,
  and not a `claude-sandbox` command.
- **The answer is read off `bwrap_build`'s argv** for that launch, built from
  the same conf and environment the shadow uses. bwrap applies mounts in
  order, so the last mount at the path or above it decides: a bind of the
  host path onto itself shows it (read-write for `--bind`), and anything
  else (a tmpfs, a fresh `/dev`, `/dev/null`, a bind from another source)
  hides it. An argv option `scope.py` does not know is an error, so a new
  kind of mount cannot be misread as showing a path.
- Each answer also says whether the path is **uniform**: no later mount lands
  inside it. The extension asks about whole folders at once, and treats a
  folder with a mask inside it (`/`, which holds `$HOME`) as unreadable.
- `scope.py` adds nothing to the argv and runs nothing.

## Consequences

- The extension sends a selection's text only for files the jail would let
  Claude read, and opens diffs only for files it could write, whatever the
  conf says.
- A new bwrap option in `bwrap.py` needs adding to `scope.py`'s tables; its
  tests build a real argv, so they fail until it is.
- The answer is about mounts, not file permissions, and about the conf and
  environment at the time of the question: a conf edited after a session
  started is reported for the next launch.
