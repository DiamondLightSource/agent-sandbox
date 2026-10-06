"""``gh-auth`` and ``glab-auth``: log a forge CLI in with a pasted PAT.

The token is read unechoed, handed to the forge CLI on stdin (never on a
command line), and kept nowhere else: it lives as long as the container's
own gh or glab config does (Invariant 2: PATs are container-scoped).
"""

import os
import subprocess
import sys
import termios

GITLAB = "gitlab.diamond.ac.uk"


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


def gh_auth() -> int:
    sys.stdout.write(
        GH_TEXT.format(url=_link("https://github.com/settings/personal-access-tokens"))
    )
    token = read_secret("GitHub PAT: ")
    return _steps(
        (["gh", "auth", "login", "--with-token"], f"{token}\n"),
        (["gh", "auth", "setup-git"], None),
        (["gh", "auth", "status"], None),
    )


def glab_auth(hostname: str = GITLAB) -> int:
    """Log glab in, pin the instance to HTTPS and make it glab's default.

    glab has no ``auth login --git-protocol`` and ships ``git_protocol``
    as ssh, which a sandbox has no key for; ``config set`` without
    ``--global`` or ``-h`` writes a repository-local file, or fails
    outside a repository.
    """
    url = _link(f"https://{hostname}/-/user_settings/personal_access_tokens")
    sys.stdout.write(GLAB_TEXT.format(url=url))
    token = read_secret(f"GitLab PAT for {hostname}: ")
    rc = _steps(
        (["glab", "auth", "login", "--stdin", "--hostname", hostname], f"{token}\n"),
        (["glab", "config", "set", "-h", hostname, "git_protocol", "https"], None),
        (["glab", "config", "set", "--global", "host", hostname], None),
    )
    if rc:
        return rc
    print(f"Default glab host set to {hostname}.")
    return run(["glab", "auth", "status"])
