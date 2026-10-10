"""Several GitHub tokens at once, chosen per repository (ADR 29).

``gh-auth --add NAME`` stores a further token beside gh's own login, in
``~/.config/gh/scoped/NAME.token`` (container-scoped, Invariant 2). Which
token a repository needs is learnt by trying them, because neither git nor
GitHub can say in advance: git asks credential helpers in order and never
retries a rejected push, and GitHub reports what the *account* may do, not
what a fine-grained token may do.

So the sandbox's git config names one credential helper for github.com
(``_git_credential``, with ``useHttpPath`` so git tells it the repository).
On a request for a repository it has not seen it probes the tokens in order,
gh's own first, with an authenticated ``info/refs?service=git-receive-pack``
(the first leg of a push, which transfers nothing), remembers the first that
GitHub lets push, and answers with it. When none can push it answers with
gh's own token, so fetches work as before. With no named token it hands the
request to ``gh auth git-credential`` unchanged and probes nothing.

The jail's ``gh`` is a shim (``_gh``) that sets ``GH_TOKEN`` from that cache
for the repository in hand, in gh's own environment only, then execs gh.

Standard library only: both entries run from the root-owned interpreter
under ``python -I`` (ADR 26).
"""

import base64
import json
import os
import re
import stat
import subprocess
import sys
import urllib.error
import urllib.request
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import IO, cast
from urllib.parse import urlsplit

from ..tools import TOOL_PATH, find_tool, output, write_atomic

# Where gh is looked for (ADR 26: no executable found through PATH). The
# jail's gh shim is not in it, so the shim never finds itself.
FORGE_PATH = (*TOOL_PATH, "/usr/local/bin")
GITHUB = "https://github.com"
API = "https://api.github.com"
HOST = "github.com"
DEFAULT = "default"
TIMEOUT = 10.0
_NAME = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]*")
_SUFFIX = ".token"
_STATE = "state.json"
# A github.com remote in each spelling git accepts; the rest is OWNER/REPO.
_REMOTE = re.compile(
    r"(?:https://(?:[^@/]+@)?|ssh://git@|git@)github\.com[:/](.+)", re.IGNORECASE
)


def valid_name(name: str) -> bool:
    """A token name: a file name with no path, not hidden, not ``default``."""
    return _NAME.fullmatch(name) is not None and name != DEFAULT


def repo_key(path: str) -> str | None:
    """``owner/repo`` (lower case, as GitHub matches) from a request path."""
    parts = [p for p in path.split("/") if p]
    if len(parts) < 2:
        return None
    owner, repo = parts[0], parts[1].removesuffix(".git")
    return f"{owner}/{repo}".lower() if repo else None


def repo_of(spec: str) -> str | None:
    """The repository a remote URL or gh's ``-R [HOST/]OWNER/REPO`` names,
    when it is on github.com."""
    match = _REMOTE.fullmatch(spec)
    if match:
        return repo_key(match.group(1))
    parts = spec.split("/")
    if len(parts) == 3 and parts[0].lower() == HOST:
        parts = parts[1:]
    return repo_key("/".join(parts)) if len(parts) == 2 else None


class UnsafeStore(Exception):
    """The store's directory is not one a token may be written to."""


class Store:
    """The named tokens and the repository -> token cache, both under the
    container-scoped gh directory, both mode 0600."""

    def __init__(self, home: str) -> None:
        self.dir = Path(home) / ".config/gh/scoped"

    def names(self) -> list[str]:
        try:
            files = os.listdir(self.dir)
        except OSError:
            return []
        names = (f.removesuffix(_SUFFIX) for f in files if f.endswith(_SUFFIX))
        return sorted(n for n in names if valid_name(n))

    def token(self, name: str) -> str | None:
        try:
            return (self.dir / f"{name}{_SUFFIX}").read_text().strip() or None
        except OSError:
            return None

    def state(self) -> dict[str, dict[str, object]]:
        try:
            data: object = json.loads((self.dir / _STATE).read_text())
        except (OSError, ValueError):
            return {"repos": {}, "expires": {}}
        parts = cast(dict[str, object], data) if isinstance(data, dict) else {}
        return {
            k: cast(dict[str, object], v) if isinstance(v, dict) else {}
            for k in ("repos", "expires")
            for v in (parts.get(k),)
        }

    def _save(self, state: dict[str, dict[str, object]]) -> None:
        self.dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        data = json.dumps(state, indent=1, sort_keys=True) + "\n"
        write_atomic(self.dir / _STATE, data.encode(), 0o600)

    def lookup(self, repo: str) -> str | None:
        """The token name cached for ``repo``, if any."""
        entry = self.state()["repos"].get(repo)
        name = cast(dict[str, object], entry).get("token") if entry else None
        return name if isinstance(name, str) else None

    def remember(self, repo: str, name: str, *, fallback: bool = False) -> None:
        state = self.state()
        state["repos"][repo] = {"token": name, "fallback": fallback}
        self._save(state)

    def forget(self, repo: str | None = None) -> int:
        """Drop ``repo``'s entry, or every entry: how many went."""
        state = self.state()
        repos = state["repos"]
        gone = list(repos) if repo is None else [r for r in (repo,) if r in repos]
        for r in gone:
            del repos[r]
        if gone:
            self._save(state)
        return len(gone)

    def safe_dir(self) -> None:
        """Make the store's directory, or refuse one that is a link, not
        ours or not 0700. ``--add`` runs outside the jail, but the jail can
        write here, and could point the store somewhere that outlives the
        container (Invariant 2)."""
        self.dir.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.dir.mkdir(mode=0o700)
        except FileExistsError:
            pass
        st = os.lstat(self.dir)
        if (
            stat.S_ISLNK(st.st_mode)
            or not stat.S_ISDIR(st.st_mode)
            or st.st_uid != os.geteuid()
            or stat.S_IMODE(st.st_mode) != 0o700
        ):
            raise UnsafeStore(
                f"{self.dir} is not a directory of mode 0700 owned by this user;"
                " remove it and try again"
            )

    def add(self, name: str, token: str, expires: str | None) -> None:
        """Store ``token`` as ``name``; repositories no token could push to
        are tried again, since this one may."""
        self.safe_dir()
        write_atomic(self.dir / f"{name}{_SUFFIX}", f"{token}\n".encode(), 0o600)
        state = self.state()
        repos = state["repos"]
        for repo in [
            r for r, e in repos.items() if cast(dict[str, object], e).get("fallback")
        ]:
            del repos[repo]
        state["expires"].pop(name, None)
        if expires:
            state["expires"][name] = expires
        self._save(state)


class _SameHostRedirects(urllib.request.HTTPRedirectHandler):
    """Follow a redirect only within the host: urllib would otherwise carry
    the Authorization header to wherever it points."""

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: IO[bytes],
        code: int,
        msg: str,
        headers: object,
        newurl: str,
    ) -> urllib.request.Request | None:
        # Scheme too: never from https to http with the Authorization header.
        if urlsplit(newurl)[:2] != urlsplit(req.full_url)[:2]:
            return None
        return super().redirect_request(req, fp, code, msg, headers, newurl)  # type: ignore[arg-type]


def _get(url: str, token: str, timeout: float) -> tuple[int | None, Mapping[str, str]]:
    """GET ``url`` with ``token``: the status and headers, or None and no
    headers when there was no answer."""
    basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    request = urllib.request.Request(
        url,
        # Not git/*: api.github.com answers that User-Agent with a 403.
        headers={"Authorization": f"Basic {basic}", "User-Agent": "claude-sandbox"},
    )
    opener = urllib.request.build_opener(_SameHostRedirects)
    try:
        with opener.open(request, timeout=timeout) as response:
            return response.status, dict(response.headers)
    except urllib.error.HTTPError as error:
        error.close()
        return error.code, dict(error.headers or {})
    except (OSError, ValueError):
        return None, {}


def probe(
    repo: str, token: str, *, base: str = GITHUB, timeout: float = TIMEOUT
) -> bool | None:
    """Would GitHub let ``token`` push to ``repo``? None: no answer."""
    status, _ = _get(
        f"{base}/{repo}.git/info/refs?service=git-receive-pack", token, timeout
    )
    return None if status is None else status == 200


def check(
    token: str, *, base: str = API, timeout: float = TIMEOUT
) -> tuple[bool | None, str | None]:
    """Does GitHub accept ``token`` (None: no answer), and when it expires,
    from the header GitHub sends for a token with an expiry."""
    status, headers = _get(f"{base}/rate_limit", token, timeout)
    if status is None:
        return None, None
    lower = {k.lower(): v for k, v in headers.items()}
    return status == 200, lower.get("github-authentication-token-expiration")


def default_token(gh: str) -> str | None:
    """gh's own token for github.com, as ``gh auth token`` prints it."""
    rc, out = output([gh, "auth", "token", "--hostname", HOST])
    return (out.strip() or None) if rc == 0 else None


def choose(store: Store, repo: str, gh: str, *, base: str = GITHUB) -> str:
    """The name of the token to use for ``repo``: the cached one, else the
    first that can push (cached), else ``default`` (cached as a fallback
    only when every probe had an answer)."""
    names = store.names()
    cached = store.lookup(repo)
    if cached == DEFAULT or (cached is not None and cached in names):
        return cached
    answered = True
    for name in (DEFAULT, *names):
        token = default_token(gh) if name == DEFAULT else store.token(name)
        if token is None:
            continue
        can_push = probe(repo, token, base=base)
        if can_push:
            store.remember(repo, name)
            return name
        answered = answered and can_push is not None
    if answered:
        store.remember(repo, DEFAULT, fallback=True)
    return DEFAULT


def _request(text: str) -> dict[str, str]:
    """git's credential request: ``key=value`` lines."""
    pairs = (line.partition("=") for line in text.splitlines())
    return {key: value for key, sep, value in pairs if sep}


def credential_main(
    args: Sequence[str],
    *,
    stdin: IO[str] = sys.stdin,
    stdout: IO[str] = sys.stdout,
    home: str | None = None,
    base: str = GITHUB,
) -> int:
    """git's credential helper for github.com (``get``, ``store``, ``erase``)."""
    op = args[0] if args else ""
    text = stdin.read()
    gh = find_tool("gh", search=FORGE_PATH)
    if gh is None:
        sys.stderr.write("claude-sandbox: gh is not installed\n")
        return 1
    store = Store(home or os.environ.get("HOME", "/"))
    request = _request(text)
    ours = request.get("protocol") == "https" and request.get("host") == HOST
    repo = repo_key(request.get("path", "")) if ours else None
    if op == "erase" and repo:
        store.forget(repo)  # GitHub refused it: probe afresh next time
    if op == "get" and repo and store.names():
        name = choose(store, repo, gh, base=base)
        token = None if name == DEFAULT else store.token(name)
        if token:
            stdout.write(
                f"protocol=https\nhost={HOST}\nusername=x-access-token\npassword={token}\n"
            )
            return 0
    stdout.flush()
    done = subprocess.run(
        [gh, "auth", "git-credential", *args], input=text, text=True, check=False
    )
    return done.returncode


def _repo_flag(args: Sequence[str]) -> str | None:
    """The value of gh's ``-R``/``--repo``, before any ``--``."""
    for i, arg in enumerate(args):
        if arg == "--":
            break
        if arg in ("-R", "--repo") and i + 1 < len(args):
            return args[i + 1]
        if arg.startswith("--repo="):
            return arg.removeprefix("--repo=")
        if arg.startswith("-R") and len(arg) > 2:
            return arg[2:].removeprefix("=")
    return None


def _remote_repo() -> str | None:
    """The github.com repository gh would pick from this checkout's remotes:
    one marked ``gh-resolved``, else upstream, github, origin."""
    git = find_tool("git")
    if git is None:
        return None
    rc, out = output(
        [git, "config", "--get-regexp", r"^remote\..*\.(url|gh-resolved)$"]
    )
    if rc != 0:
        return None
    urls: dict[str, str] = {}
    resolved: list[str] = []
    for line in out.splitlines():
        key, _, value = line.partition(" ")
        remote, _, field = key.removeprefix("remote.").rpartition(".")
        if field == "url":
            urls.setdefault(remote, value)
        elif value == "base":
            resolved.append(remote)
    for remote in (*resolved, "upstream", "github", "origin"):
        repo = repo_of(urls.get(remote, ""))
        if repo:
            return repo
    return None


def gh_env(
    args: Sequence[str],
    env: Mapping[str, str],
    store: Store,
    gh: str | None = None,
    *,
    base: str = GITHUB,
) -> dict[str, str]:
    """gh's environment: ``env``, plus ``GH_TOKEN`` for the repository in
    hand when a named token is chosen for it. Never for ``gh auth``, nor
    over a token or host the caller set.

    With named tokens stored and ``gh`` given, a repository git has not
    pushed to (the upstream of a fork, say) is probed and cached as git's
    helper does; the probe measures push rights, not pull-request rights."""
    child = dict(env)
    if args[:1] == ["auth"] or env.get("GH_TOKEN") or env.get("GITHUB_TOKEN"):
        return child
    if env.get("GH_HOST", HOST).lower() != HOST:
        return child
    spec = _repo_flag(args) or env.get("GH_REPO")
    repo = repo_of(spec) if spec else _remote_repo()
    if repo and gh and store.names():
        name: str | None = choose(store, repo, gh, base=base)
    else:
        name = store.lookup(repo) if repo else None
    token = store.token(name) if name and name != DEFAULT else None
    if token:
        child["GH_TOKEN"] = token
    return child


def gh_main(args: Sequence[str]) -> int:
    """The jail's ``gh``: the real one, with the repository's token."""
    gh = find_tool("gh", search=FORGE_PATH)
    if gh is None:
        sys.stderr.write("claude-sandbox: gh is not installed\n")
        return 127
    store = Store(os.environ.get("HOME", "/"))
    env = gh_env(args, os.environ, store, gh, base=GITHUB)
    os.execve(gh, [gh, *args], env)
    return 127  # pragma: no cover - execve does not return


def status_lines(store: Store) -> list[str]:
    """``gh-auth --status``: each token, its expiry and its repositories."""
    state = store.state()
    expires = state["expires"]
    by_name: dict[str, list[str]] = {}
    for repo, entry in sorted(state["repos"].items()):
        e = cast(dict[str, object], entry)
        label = f"{repo} (no token could push)" if e.get("fallback") else repo
        by_name.setdefault(str(e.get("token")), []).append(label)
    lines = [f"{DEFAULT}: gh's own login (gh auth status shows its account and expiry)"]
    for name in store.names():
        when = expires.get(name)
        lines.append(
            f"{name}: expires {when}"
            if isinstance(when, str)
            else f"{name}: expiry unknown"
        )
    out: list[str] = []
    for line in lines:
        name = line.partition(":")[0]
        out.append(line)
        repos = by_name.pop(name, [])
        out.append(
            f"    repos: {', '.join(repos)}" if repos else "    repos: none cached yet"
        )
    for name, repos in sorted(by_name.items()):
        out.append(f"{name}: token file missing (probed afresh on next use)")
        out.append(f"    repos: {', '.join(repos)}")
    return out
