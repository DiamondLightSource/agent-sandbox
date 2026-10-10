"""The sandbox's git config (``GIT_CONFIG_GLOBAL`` inside the jail).

A pure function returning the file's text. The shadow re-renders it on
every launch because VS Code's dev.containers.copyGitConfig fires AFTER
postCreate, so an install-time render can have an empty user.name; by
launch time copyGitConfig has run.

``_value`` writes a value as git itself would, so git reads back exactly
the identity it was given. Standard library only: this module is on the
launch path.

GitHub's helper is the sandbox's own (``helpers.tokens``, ADR 29): with
``useHttpPath`` git tells it the repository, so it can pick among several
stored tokens; with only gh's login it hands every request to
``gh auth git-credential``, as the helper here once was.
"""

from .profiles import LIBEXEC

GITHUB_HELPER = f"{LIBEXEC}/venv/bin/python -I -m claude_sandbox _git_credential"

_FORGE_CREDENTIALS = f"""\
[credential "https://github.com"]
    helper = !{GITHUB_HELPER}
    useHttpPath = true
[credential "https://gitlab.diamond.ac.uk"]
    helper = !glab auth git-credential
"""

_REWRITES = """\
[url "https://github.com/"]
    insteadOf = git@github.com:
    insteadOf = ssh://git@github.com/
[url "https://gitlab.diamond.ac.uk/"]
    insteadOf = git@gitlab.diamond.ac.uk:
    insteadOf = ssh://git@gitlab.diamond.ac.uk/
[init]
    defaultBranch = main
[safe]
    directory = *
"""


def _value(value: str) -> str:
    """A value as ``git config`` writes it (config.c, write_pair).

    Quoting a value that holds a carriage return is git's fix for
    CVE-2025-48384, which older git wrote bare.
    """
    edges = value.startswith(" ") or value.endswith(" ")
    quote = '"' if edges or any(c in value for c in ";#\r") else ""
    escaped = (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\t", "\\t")
    )
    return f"{quote}{escaped}{quote}"


def render_gitconfig(user_name: str, user_email: str, *, no_forge: bool) -> str:
    """The config text for this identity.

    ``user_name`` and ``user_email`` are what ``git config --get`` printed,
    without trailing newlines (empty when unset). The forge credential
    helpers are left out when CLAUDE_SANDBOX_NO_FORGE=1.
    """
    return (
        ("" if no_forge else _FORGE_CREDENTIALS)
        + _REWRITES
        + f"[user]\n\tname = {_value(user_name)}\n\temail = {_value(user_email)}\n"
    )
