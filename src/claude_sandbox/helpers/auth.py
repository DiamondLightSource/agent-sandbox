"""``gh-auth`` and ``glab-auth``: log a forge CLI in with a pasted PAT.

The token is read unechoed, handed to the forge CLI on stdin (never on a
command line), and kept nowhere else: it lives as long as the container's
own gh or glab config does (Invariant 2: PATs are container-scoped).
``gh-auth --add NAME`` keeps a further GitHub token beside gh's, in the same
directory (``tokens``, ADR 29).
"""

import os
import subprocess
import sys
import termios

from ..tools import find_tool
from . import tokens

GITLAB = "gitlab.diamond.ac.uk"
# Where gh and glab are looked for (ADR 26: no executable found through PATH).
FORGE_PATH = tokens.FORGE_PATH


def _link(url: str) -> str:
    return f"\033[4;36m{url}\033[0m"


GH_TEXT = """\
Create or renew a fine-grained PAT at:
  {url}

Recommended settings for a sandboxed Claude Code:
  - Resource owner: your user (or org that owns this repo)
  - Repository access: Only select repositories -> just this repo
  - Expiration: short (e.g. 30 days) so a leaked token expires quickly
  - Repository permissions Read/Write:
      Issues, Pull requests
  - Repository permissions Read Only:
      Contents
    (Metadata: Read-only is added automatically)
  - Leave everything else unset / no access

"""

GLAB_TEXT = """\
Create or renew a fine-grained PAT at:
  {url}

Recommended scopes for a sandboxed Claude Code:
  - api, read_repository, write_repository
  - Short expiration so a leaked token expires quickly

"""


def run(argv: list[str], stdin: str | None = None) -> int:
    sys.stdout.flush()
    return subprocess.run(argv, input=stdin, text=True, check=False).returncode


def read_secret(prompt: str) -> str:
    """bash's ``read -sp PROMPT``: the prompt on stderr, the line unechoed."""
    sys.stderr.write(prompt)
    sys.stderr.flush()
    fd = sys.stdin.fileno()
    saved = termios.tcgetattr(fd) if os.isatty(fd) else None
    if saved is not None:
        quiet = termios.tcgetattr(fd)
        quiet[3] &= ~termios.ECHO
        termios.tcsetattr(fd, termios.TCSANOW, quiet)
    try:
        line = sys.stdin.readline()
    finally:
        if saved is not None:
            termios.tcsetattr(fd, termios.TCSANOW, saved)
    if line.endswith("\n"):
        print()
    return line.strip(" \t\n")


def _steps(*steps: tuple[list[str], str | None]) -> int:
    """Run each step, stopping at the first failure (bash's ``set -e``)."""
    rc = 0
    for argv, stdin in steps:
        rc = run(argv, stdin)
        if rc:
            break
    return rc


def _tool(name: str) -> str | None:
    path = find_tool(name, search=FORGE_PATH)
    if path is None:
        print(f"claude-sandbox: {name} is not installed", file=sys.stderr)
    return path


def gh_auth() -> int:
    gh = _tool("gh")
    if gh is None:
        return 1
    sys.stdout.write(
        GH_TEXT.format(url=_link("https://github.com/settings/personal-access-tokens"))
    )
    token = read_secret("GitHub PAT: ")
    return _steps(
        ([gh, "auth", "login", "--with-token"], f"{token}\n"),
        ([gh, "auth", "setup-git"], None),
        ([gh, "auth", "status"], None),
    )


ADD_TEXT = """\
Storing GitHub token "{name}" beside gh's own login. git tries the tokens
on a repository's first push (gh's own first) and keeps the first that
GitHub lets push; `claude-sandbox gh-auth --status` lists what it learnt.

"""


def gh_add(name: str, home: str, *, api: str | None = None) -> int:
    """Store a further GitHub token as ``name``."""
    if not tokens.valid_name(name):
        print(
            f"claude-sandbox: bad token name {name!r}: use letters, digits, '_',"
            f" '.' and '-', not starting with '.' or '-', and not"
            f" '{tokens.DEFAULT}'",
            file=sys.stderr,
        )
        return 2
    sys.stdout.write(
        GH_TEXT.format(url=_link("https://github.com/settings/personal-access-tokens"))
    )
    sys.stdout.write(ADD_TEXT.format(name=name))
    token = read_secret(f"GitHub PAT for {name}: ")
    if not token:
        print("claude-sandbox: no token given; nothing stored", file=sys.stderr)
        return 1
    accepted, expires = tokens.check(token, base=api or tokens.API)
    if accepted is False:
        print(
            "claude-sandbox: GitHub rejected that token; nothing stored",
            file=sys.stderr,
        )
        return 1
    if accepted is None:
        print("claude-sandbox: GitHub did not answer; storing the token unchecked")
    store = tokens.Store(home)
    try:
        store.add(name, token, expires)
    except tokens.UnsafeStore as error:
        print(f"claude-sandbox: {error}; nothing stored", file=sys.stderr)
        return 1
    print(f"Stored {store.dir}/{name}.token (mode 0600).")
    return 0


def gh_status(home: str) -> int:
    """``gh-auth --status``."""
    for line in tokens.status_lines(tokens.Store(home)):
        print(line)
    return 0


def gh_forget(home: str, repo: str | None) -> int:
    """``gh-auth --forget [OWNER/REPO]``: drop cached choices."""
    key = None if repo is None else tokens.repo_of(repo)
    if repo is not None and key is None:
        print(f"claude-sandbox: not a GitHub OWNER/REPO: {repo}", file=sys.stderr)
        return 2
    gone = tokens.Store(home).forget(key)
    print(f"Forgot {gone} cached repositor{'y' if gone == 1 else 'ies'}.")
    return 0


def glab_auth(hostname: str = GITLAB) -> int:
    """Log glab in, pin the instance to HTTPS and make it glab's default.

    glab has no ``auth login --git-protocol`` and ships ``git_protocol``
    as ssh, which a sandbox has no key for; ``config set`` without
    ``--global`` or ``-h`` writes a repository-local file, or fails
    outside a repository.
    """
    glab = _tool("glab")
    if glab is None:
        return 1
    url = _link(f"https://{hostname}/-/user_settings/personal_access_tokens")
    sys.stdout.write(GLAB_TEXT.format(url=url))
    token = read_secret(f"GitLab PAT for {hostname}: ")
    rc = _steps(
        ([glab, "auth", "login", "--stdin", "--hostname", hostname], f"{token}\n"),
        ([glab, "config", "set", "-h", hostname, "git_protocol", "https"], None),
        ([glab, "config", "set", "--global", "host", hostname], None),
    )
    if rc:
        return rc
    print(f"Default glab host set to {hostname}.")
    return run([glab, "auth", "status"])
