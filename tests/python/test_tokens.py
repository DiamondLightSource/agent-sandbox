"""Several GitHub tokens, chosen per repository (helpers/tokens.py, ADR 29).

The probe is exercised against a local HTTP server standing in for
github.com; no real token or GitHub is involved. gh is a fake script that
logs how it was called.
"""

import base64
import io
import os
import runpy
import shutil
import stat
import subprocess
import sys
import threading
from collections.abc import Callable, Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from claude_sandbox import cli, context
from claude_sandbox.context import CONTAINER
from claude_sandbox.gitconfig import GITHUB_HELPER, render_gitconfig
from claude_sandbox.helpers import auth, tokens
from claude_sandbox.helpers.tokens import DEFAULT, Store

# --- a fake github.com ------------------------------------------------------

# Which tokens may push where; any other token is refused with a 403.
PUSH = {"/acme/app.git": {"tok-acme"}, "/beta/lib.git": {"tok-beta", "tok-acme"}}
EXPIRY = "2026-12-01 00:00:00 UTC"


class FakeGitHub(BaseHTTPRequestHandler):
    seen: list[tuple[str, str]] = []

    def log_message(self, format: str, *args: object) -> None:
        pass

    def do_GET(self) -> None:
        header = self.headers.get("Authorization", "")
        token = (
            base64.b64decode(header.removeprefix("Basic ")).decode().partition(":")[2]
        )
        path, _, query = self.path.partition("?")
        FakeGitHub.seen.append((path, token))
        if path == "/rate_limit":
            # Like api.github.com, which refuses a git/* User-Agent with a 403.
            if self.headers.get("User-Agent", "").startswith("git/"):
                self._answer(403)
            elif token == "bad":
                self._answer(401)
            elif token == "blocked":
                self._answer(403)
            elif token == "noexpiry":
                self._answer(200)
            else:
                self._answer(200, {"GitHub-Authentication-Token-Expiration": EXPIRY})
        elif path == "/moved/app.git/info/refs":
            self._answer(301, {"Location": "http://elsewhere.invalid/acme/app.git"})
        elif path.endswith("/info/refs") and query == "service=git-receive-pack":
            repo = path.removesuffix("/info/refs")
            self._answer(200 if token in PUSH.get(repo, set()) else 403)
        else:
            self._answer(404)

    def _answer(self, code: int, headers: dict[str, str] | None = None) -> None:
        self.send_response(code)
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Length", "0")
        self.end_headers()


@pytest.fixture(scope="module")
def server() -> Iterator[str]:
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), FakeGitHub)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()


@pytest.fixture
def seen() -> list[tuple[str, str]]:
    FakeGitHub.seen = []
    return FakeGitHub.seen


# --- a fake gh ----------------------------------------------------------------

FAKE_GH = """#!/bin/sh
printf '%s\\n' "$*" >> "{log}"
case "$*" in
  "auth token --hostname github.com") [ -f "{dir}/no-default" ] || echo tok-default ;;
  "auth git-credential get") cat > "{log}.stdin"
    printf 'username=u\\npassword=tok-default\\n' ;;
  auth\\ git-credential*) cat > "{log}.stdin" ;;
esac
"""


class Gh:
    def __init__(self, root: Path) -> None:
        self.dir = root / "bin"
        self.dir.mkdir()
        self.log = root / "gh.log"
        self.path = self.dir / "gh"
        self.path.write_text(FAKE_GH.format(log=self.log, dir=root))
        self.path.chmod(0o755)
        self.root = root

    def calls(self) -> list[str]:
        return self.log.read_text().splitlines() if self.log.exists() else []

    def no_default(self) -> None:
        (self.root / "no-default").touch()


@pytest.fixture
def gh(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Gh:
    fake = Gh(tmp_path)
    monkeypatch.setattr(tokens, "FORGE_PATH", (str(fake.dir),))
    return fake


@pytest.fixture
def home(tmp_path: Path) -> Path:
    (tmp_path / "home").mkdir()
    return tmp_path / "home"


def stored(home: Path, **named: str) -> Store:
    store = Store(str(home))
    for name, token in named.items():
        store.add(name, token, None)
    return store


def ask(home: Path, server: str, path: str, op: str = "get") -> tuple[int, str]:
    out = io.StringIO()
    request = f"protocol=https\nhost=github.com\npath={path}\n\n"
    rc = tokens.credential_main(
        [op], stdin=io.StringIO(request), stdout=out, home=str(home), base=server
    )
    return rc, out.getvalue()


# --- names and repositories --------------------------------------------------


@pytest.mark.parametrize("name", ["work", "acme-ro", "a.b_c", "X9"])
def test_valid_names(name: str) -> None:
    assert tokens.valid_name(name)


@pytest.mark.parametrize(
    "name", ["", "default", ".hidden", "-x", "a/b", "..", "a b", "é"]
)
def test_invalid_names(name: str) -> None:
    assert not tokens.valid_name(name)


@pytest.mark.parametrize(
    ("spec", "repo"),
    [
        ("https://github.com/Acme/App.git", "acme/app"),
        ("https://user@github.com/acme/app", "acme/app"),
        ("git@github.com:acme/app.git", "acme/app"),
        ("ssh://git@github.com/acme/app", "acme/app"),
        ("acme/app", "acme/app"),
        ("github.com/acme/app", "acme/app"),
        ("gitlab.example/acme/app", None),
        ("https://gitlab.example/acme/app", None),
        ("acme", None),
        ("acme/.git", None),
        ("", None),
    ],
)
def test_repo_of(spec: str, repo: str | None) -> None:
    assert tokens.repo_of(spec) == repo


# --- the store ------------------------------------------------------------------


def test_add_writes_0600_files_in_a_0700_dir(home: Path) -> None:
    store = Store(str(home))
    store.add("work", "tok-acme", EXPIRY)
    token = home / ".config/gh/scoped/work.token"
    assert token.read_text() == "tok-acme\n"
    assert stat.S_IMODE(token.stat().st_mode) == 0o600
    assert stat.S_IMODE((home / ".config/gh/scoped/state.json").stat().st_mode) == 0o600
    assert stat.S_IMODE(store.dir.stat().st_mode) == 0o700
    assert store.names() == ["work"] and store.token("work") == "tok-acme"
    assert store.state()["expires"] == {"work": EXPIRY}
    store.add("work", "tok-2", None)  # replaced; the old expiry goes
    assert store.state()["expires"] == {} and store.token("work") == "tok-2"


def test_add_refuses_an_unsafe_store_dir(home: Path, tmp_path: Path) -> None:
    store = Store(str(home))
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir(mode=0o700)
    store.dir.parent.mkdir(parents=True)
    store.dir.symlink_to(elsewhere)
    with pytest.raises(tokens.UnsafeStore):
        store.add("a", "tok", None)
    assert list(elsewhere.iterdir()) == []
    store.dir.unlink()
    store.dir.mkdir(mode=0o755)
    store.dir.chmod(0o755)
    with pytest.raises(tokens.UnsafeStore):
        store.add("a", "tok", None)
    store.dir.rmdir()
    store.dir.write_text("")
    with pytest.raises(tokens.UnsafeStore):
        store.add("a", "tok", None)
    assert not (home / ".config/gh/a.token").exists()


def test_add_refuses_a_store_dir_owned_by_another_user(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = Store(str(home))
    store.dir.mkdir(parents=True, mode=0o700)
    monkeypatch.setattr(tokens.os, "geteuid", lambda: os.getuid() + 1)
    with pytest.raises(tokens.UnsafeStore):
        store.add("a", "tok", None)


def test_store_ignores_junk(home: Path) -> None:
    store = Store(str(home))
    assert store.names() == [] and store.token("x") is None
    assert store.state() == {"repos": {}, "expires": {}}
    store.dir.mkdir(parents=True)
    (store.dir / "state.json").write_text("not json")
    assert store.state() == {"repos": {}, "expires": {}}
    (store.dir / "state.json").write_text('{"repos": [], "expires": {"a": "b"}}')
    assert store.state() == {"repos": {}, "expires": {"a": "b"}}
    (store.dir / "state.json").write_text("[]")
    assert store.state() == {"repos": {}, "expires": {}}
    (store.dir / ".hidden.token").write_text("x")
    (store.dir / "default.token").write_text("x")
    (store.dir / "empty.token").write_text("\n")
    assert store.names() == ["empty"] and store.token("empty") is None
    (store.dir / "state.json").write_text('{"repos": {"a/b": {"token": 3}}}')
    assert store.lookup("a/b") is None and store.lookup("c/d") is None


def test_forget(home: Path) -> None:
    store = stored(home, work="tok-acme")
    store.remember("acme/app", "work")
    store.remember("beta/lib", DEFAULT, fallback=True)
    assert store.forget("no/such") == 0
    assert store.forget("acme/app") == 1 and store.lookup("acme/app") is None
    store.remember("acme/app", "work")
    assert store.forget() == 2 and store.state()["repos"] == {}


def test_adding_a_token_retries_the_fallbacks(home: Path) -> None:
    store = stored(home, work="tok-acme")
    store.remember("acme/app", "work")
    store.remember("beta/lib", DEFAULT, fallback=True)
    store.add("more", "tok-beta", None)
    assert store.lookup("acme/app") == "work" and store.lookup("beta/lib") is None


# --- the probe ---------------------------------------------------------------------


def test_probe_against_a_fake_github(server: str, seen: list[tuple[str, str]]) -> None:
    assert tokens.probe("acme/app", "tok-acme", base=server) is True
    assert tokens.probe("acme/app", "tok-beta", base=server) is False
    assert seen == [
        ("/acme/app.git/info/refs", "tok-acme"),
        ("/acme/app.git/info/refs", "tok-beta"),
    ]
    # No answer at all is not a refusal.
    assert tokens.probe("acme/app", "t", base="http://127.0.0.1:9", timeout=2) is None
    # A redirect off the host is not followed, so the token stays here.
    assert tokens.probe("moved/app", "tok-acme", base=server) is False
    assert not any("elsewhere" in p for p, _ in seen)


def test_check(server: str) -> None:
    assert tokens.check("tok-acme", base=server) == (True, EXPIRY)
    assert tokens.check("noexpiry", base=server) == (True, None)
    assert tokens.check("bad", base=server) == (False, None)
    assert tokens.check("blocked", base=server) == (False, None)
    assert tokens.check("t", base="http://127.0.0.1:9", timeout=2) == (None, None)


def test_same_host_redirects_are_followed(server: str) -> None:
    handler = tokens._SameHostRedirects()  # pyright: ignore[reportPrivateUsage]
    req = tokens.urllib.request.Request(f"{server}/a")
    fp = io.BytesIO()
    new = handler.redirect_request(req, fp, 301, "Moved", {}, f"{server}/b")  # type: ignore[arg-type]
    assert new is not None and new.full_url == f"{server}/b"
    # Never down from https to http on the same host.
    req = tokens.urllib.request.Request("https://github.com/a")
    down = handler.redirect_request(req, fp, 301, "Moved", {}, "http://github.com/a")  # type: ignore[arg-type]
    assert down is None


# --- the credential helper -----------------------------------------------------------


def test_default_only_hands_everything_to_gh_and_probes_nothing(
    home: Path,
    gh: Gh,
    server: str,
    seen: list[tuple[str, str]],
    capfd: pytest.CaptureFixture[str],
) -> None:
    for op in ("get", "store", "erase"):
        assert ask(home, server, "acme/app.git", op)[0] == 0
    assert gh.calls() == [
        "auth git-credential get",
        "auth git-credential store",
        "auth git-credential erase",
    ]
    assert "path=acme/app.git" in Path(f"{gh.log}.stdin").read_text()
    assert "password=tok-default" in capfd.readouterr().out
    assert seen == []
    assert not (home / ".config/gh/scoped/state.json").exists()


def test_probe_order_and_first_pass_is_cached(
    home: Path, gh: Gh, server: str, seen: list[tuple[str, str]]
) -> None:
    stored(home, b_beta="tok-beta", a_acme="tok-acme")
    rc, out = ask(home, server, "beta/lib.git")
    assert rc == 0
    # gh's own token first, then the named ones by name: a_acme passes.
    assert [t for _, t in seen] == ["tok-default", "tok-acme"]
    assert "username=x-access-token\npassword=tok-acme\n" in out
    assert Store(str(home)).lookup("beta/lib") == "a_acme"
    # Cache hit: no probe, no gh.
    seen.clear()
    calls = len(gh.calls())
    assert ask(home, server, "/Beta/Lib.git/")[1].endswith("password=tok-acme\n")
    assert seen == [] and len(gh.calls()) == calls


def test_default_that_can_push_is_cached_and_answered_by_gh(
    home: Path,
    gh: Gh,
    server: str,
    seen: list[tuple[str, str]],
    capfd: pytest.CaptureFixture[str],
) -> None:
    stored(home, work="tok-acme")
    PUSH["/mine/repo.git"] = {"tok-default"}
    try:
        assert ask(home, server, "mine/repo.git") == (0, "")
    finally:
        del PUSH["/mine/repo.git"]
    assert [t for _, t in seen] == ["tok-default"]
    assert Store(str(home)).lookup("mine/repo") == DEFAULT
    assert gh.calls()[-1] == "auth git-credential get"
    assert "password=tok-default" in capfd.readouterr().out
    seen.clear()
    ask(home, server, "mine/repo.git")
    assert seen == []


def test_all_fail_falls_back_to_default_and_remembers(
    home: Path,
    gh: Gh,
    server: str,
    seen: list[tuple[str, str]],
    capfd: pytest.CaptureFixture[str],
) -> None:
    stored(home, work="tok-beta")
    assert ask(home, server, "acme/app.git") == (0, "")
    assert [t for _, t in seen] == ["tok-default", "tok-beta"]
    assert gh.calls()[-1] == "auth git-credential get"
    assert "password=tok-default" in capfd.readouterr().out
    state = Store(str(home)).state()["repos"]
    assert state["acme/app"] == {"token": DEFAULT, "fallback": True}
    seen.clear()
    ask(home, server, "acme/app.git")
    assert seen == []


def test_no_answer_falls_back_without_caching(home: Path, gh: Gh) -> None:
    stored(home, work="tok-acme")
    gh.no_default()
    assert ask(home, "http://127.0.0.1:9", "acme/app.git")[0] == 0
    assert Store(str(home)).lookup("acme/app") is None
    assert gh.calls()[-1] == "auth git-credential get"


def test_a_missing_token_file_is_probed_afresh(
    home: Path, gh: Gh, server: str, seen: list[tuple[str, str]]
) -> None:
    store = stored(home, work="tok-acme")
    store.remember("acme/app", "gone")
    assert ask(home, server, "acme/app.git")[1].endswith("password=tok-acme\n")
    assert store.lookup("acme/app") == "work"


def test_erase_drops_the_cached_choice(home: Path, gh: Gh, server: str) -> None:
    store = stored(home, work="tok-acme")
    store.remember("acme/app", "work")
    ask(home, server, "acme/app.git", "erase")
    assert store.lookup("acme/app") is None
    assert gh.calls()[-1] == "auth git-credential erase"


@pytest.mark.parametrize(
    "request_text",
    [
        "protocol=https\nhost=github.com\n\n",  # no path: useHttpPath off
        "protocol=https\nhost=gist.github.com\npath=a/b\n\n",
        "protocol=http\nhost=github.com\npath=acme/app\n\n",
    ],
)
def test_requests_it_cannot_place_go_to_gh(
    home: Path, gh: Gh, server: str, seen: list[tuple[str, str]], request_text: str
) -> None:
    stored(home, work="tok-acme")
    rc = tokens.credential_main(
        ["get"],
        stdin=io.StringIO(request_text),
        stdout=io.StringIO(),
        home=str(home),
        base=server,
    )
    assert rc == 0 and seen == [] and gh.calls() == ["auth git-credential get"]


def test_credential_helper_without_gh(
    home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(tokens, "FORGE_PATH", ("/nonexistent",))
    assert tokens.credential_main(["get"], stdin=io.StringIO(""), home=str(home)) == 1
    assert "gh is not installed" in capsys.readouterr().err


def test_git_uses_the_helper_end_to_end(
    home: Path, gh: Gh, server: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """git, given the rendered config, asks the helper with the path."""
    git = shutil.which("git")
    if git is None:
        pytest.skip("needs git")
    stored(home, work="tok-acme")
    config = tmp_path / "gitconfig"
    text = render_gitconfig("A", "a@b", no_forge=False)
    # The installed interpreter is not here: run this one, against the fake.
    driver = tmp_path / "helper.py"
    driver.write_text(
        "import sys\n"
        f"sys.path.insert(0, {str(Path(tokens.__file__).parents[2])!r})\n"
        "from claude_sandbox.helpers import tokens\n"
        f"tokens.FORGE_PATH = ({str(gh.dir)!r},)\n"
        f"sys.exit(tokens.credential_main(sys.argv[1:], base={server!r}))\n"
    )
    config.write_text(text.replace(GITHUB_HELPER, f"{sys.executable} -I {driver}"))
    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(home),
        "GIT_CONFIG_GLOBAL": str(config),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_TERMINAL_PROMPT": "0",
    }
    out = subprocess.run(
        [git, "credential", "fill"],
        input="url=https://github.com/acme/app.git\n\n",
        capture_output=True,
        text=True,
        env=env,
        check=True,
    ).stdout
    assert "password=tok-acme" in out and "path=acme/app.git" in out


# --- the rendered git config ----------------------------------------------------------


def test_rendered_gitconfig_names_the_helper_with_paths() -> None:
    text = render_gitconfig("A", "a@b", no_forge=False)
    assert text.startswith(
        '[credential "https://github.com"]\n'
        "    helper = !/usr/libexec/claude-sandbox/venv/bin/python -I -m claude_sandbox"
        " _git_credential\n"
        "    useHttpPath = true\n"
    )
    assert "glab auth git-credential" in text
    assert "_git_credential" not in render_gitconfig("A", "a@b", no_forge=True)


# --- the gh shim -------------------------------------------------------------


@pytest.fixture
def checkout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Callable[..., None]:
    """A git checkout as cwd, with the given remotes."""
    git = shutil.which("git")
    if git is None:
        pytest.skip("needs git")
    repo = tmp_path / "checkout"
    repo.mkdir()
    monkeypatch.chdir(repo)
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    subprocess.run([git, "init", "-q"], check=True)

    def remotes(*pairs: tuple[str, str], resolved: str = "") -> None:
        for name, url in pairs:
            subprocess.run([git, "remote", "add", name, url], check=True)
        if resolved:
            subprocess.run(
                [git, "config", f"remote.{resolved}.gh-resolved", "base"], check=True
            )

    return remotes


def test_gh_env_from_origin(home: Path, checkout: Callable[..., None]) -> None:
    store = stored(home, work="tok-acme")
    store.remember("acme/app", "work")
    checkout(("origin", "git@github.com:Acme/app.git"))
    env = {"HOME": str(home), "PATH": "/bin"}
    child = tokens.gh_env(["pr", "create"], env, store)
    assert child == {**env, "GH_TOKEN": "tok-acme"}
    assert "GH_TOKEN" not in env
    # gh auth sees gh's own login; a token or host the caller set stands.
    assert "GH_TOKEN" not in tokens.gh_env(["auth", "status"], env, store)
    assert (
        tokens.gh_env(["pr"], {**env, "GH_TOKEN": "mine"}, store)["GH_TOKEN"] == "mine"
    )
    assert "GH_TOKEN" not in tokens.gh_env(["pr"], {**env, "GITHUB_TOKEN": "x"}, store)
    assert "GH_TOKEN" not in tokens.gh_env(
        ["pr"], {**env, "GH_HOST": "ghe.example"}, store
    )


def test_gh_env_follows_gh_remote_choice(
    home: Path, checkout: Callable[..., None]
) -> None:
    store = stored(home, work="tok-acme", other="tok-beta")
    store.remember("acme/app", "work")
    store.remember("beta/lib", "other")
    checkout(
        ("origin", "https://github.com/beta/lib"),
        ("upstream", "https://github.com/acme/app.git"),
    )
    env = {"HOME": str(home)}
    assert tokens.gh_env(["pr", "list"], env, store)["GH_TOKEN"] == "tok-acme"
    # gh's own choice wins; a mark other than base is not a choice.
    subprocess.run(["git", "config", "remote.upstream.gh-resolved", "x"], check=True)
    assert tokens.gh_env(["pr", "list"], env, store)["GH_TOKEN"] == "tok-acme"
    subprocess.run(["git", "config", "remote.origin.gh-resolved", "base"], check=True)
    assert tokens.gh_env(["pr", "list"], env, store)["GH_TOKEN"] == "tok-beta"


@pytest.mark.parametrize(
    "args",
    [
        ["-R", "acme/app", "pr"],
        ["--repo", "acme/app"],
        ["--repo=github.com/acme/app"],
        ["-Racme/app"],
    ],
)
def test_gh_env_from_the_repo_flag(home: Path, args: list[str]) -> None:
    store = stored(home, work="tok-acme")
    store.remember("acme/app", "work")
    assert tokens.gh_env(args, {}, store)["GH_TOKEN"] == "tok-acme"


def test_gh_env_without_an_entry_is_unchanged(
    home: Path, checkout: Callable[..., None], monkeypatch: pytest.MonkeyPatch
) -> None:
    store = stored(home, work="tok-acme")
    store.remember("beta/lib", DEFAULT, fallback=True)
    env = {"HOME": str(home)}
    # Not a checkout's remote on GitHub, no entry, a default entry, no git.
    checkout(("origin", "https://gitlab.example/acme/app"))
    assert tokens.gh_env(["pr"], env, store) == env
    assert tokens.gh_env(["pr", "-R", "acme/app"], env, store) == env
    with_repo = {**env, "GH_REPO": "beta/lib"}
    assert tokens.gh_env(["pr", "--", "-R", "acme/app"], with_repo, store) == with_repo
    assert tokens.gh_env(["-R"], env, store) == env
    monkeypatch.chdir("/")
    assert tokens.gh_env(["pr"], env, store) == env

    def no_tool(name: str, search: tuple[str, ...] = ()) -> None:
        return None

    monkeypatch.setattr(tokens, "find_tool", no_tool)
    assert tokens.gh_env(["pr"], env, store) == env


def test_gh_main_execs_gh_with_the_token_in_its_env_only(
    home: Path, gh: Gh, server: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = stored(home, work="tok-acme")
    store.remember("acme/app", "work")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GH_REPO", raising=False)
    monkeypatch.delenv("GH_HOST", raising=False)
    execs: list[tuple[str, list[str], dict[str, str]]] = []

    def execve(path: str, argv: list[str], env: dict[str, str]) -> None:
        execs.append((path, argv, env))

    monkeypatch.setattr(os, "execve", execve)
    monkeypatch.setattr(tokens, "GITHUB", server)
    tokens.gh_main(["-R", "acme/app", "pr", "create"])
    tokens.gh_main(["-R", "nobody/x", "pr", "create"])
    (path, argv, env), (_, _, plain) = execs
    assert path == str(gh.path) and argv == [path, "-R", "acme/app", "pr", "create"]
    assert env["GH_TOKEN"] == "tok-acme"
    assert "GH_TOKEN" not in plain
    # The session's own environment is untouched (battery check 04).
    assert "GH_TOKEN" not in os.environ


def test_gh_probes_a_repository_git_never_pushed_to(
    home: Path, gh: Gh, server: str, seen: list[tuple[str, str]]
) -> None:
    """The fork case: git pushed to the fork, gh opens the PR upstream."""
    store = stored(home, fork="tok-beta", up="tok-acme")
    store.remember("me/app", "fork")
    env = {"HOME": str(home)}
    gh_path = str(gh.path)
    child = tokens.gh_env(
        ["pr", "create", "-R", "acme/app"], env, store, gh_path, base=server
    )
    assert child["GH_TOKEN"] == "tok-acme"
    assert [t for _, t in seen] == ["tok-default", "tok-beta", "tok-acme"]
    assert store.lookup("acme/app") == "up"
    # Cached now: no second probe. The fork keeps its own token.
    seen.clear()
    assert (
        tokens.gh_env(["-R", "acme/app"], env, store, gh_path, base=server)["GH_TOKEN"]
        == "tok-acme"
    )
    assert (
        tokens.gh_env(["-R", "me/app"], env, store, gh_path, base=server)["GH_TOKEN"]
        == "tok-beta"
    )
    assert seen == []
    # No token can push: gh's own login, remembered as a fallback.
    assert "GH_TOKEN" not in tokens.gh_env(
        ["-R", "nobody/x"], env, store, gh_path, base=server
    )
    assert store.lookup("nobody/x") == DEFAULT


def test_gh_with_only_gh_login_probes_nothing(
    home: Path, gh: Gh, server: str, seen: list[tuple[str, str]]
) -> None:
    store = Store(str(home))
    env = {"HOME": str(home)}
    assert (
        tokens.gh_env(["-R", "acme/app"], env, store, str(gh.path), base=server) == env
    )
    assert seen == [] and gh.calls() == []


def test_gh_main_without_gh(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(tokens, "FORGE_PATH", ("/nonexistent",))
    assert tokens.gh_main(["pr"]) == 127
    assert "gh is not installed" in capsys.readouterr().err


def test_gh_shim_file_runs_the_root_owned_interpreter() -> None:
    shim = Path(__file__).parents[2] / ".devcontainer/claude-sandbox/gh-shim"
    lines = shim.read_text().splitlines()
    assert lines[0] == "#!/bin/bash"
    python = "/usr/libexec/claude-sandbox/venv/bin/python"
    assert lines[-1] == f'exec {python} -I -m claude_sandbox _gh -- "$@"'
    assert shim.stat().st_mode & 0o111


def test_main_dispatches_the_entries(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, list[str]]] = []

    def credential_main(args: list[str]) -> int:
        calls.append(("credential", args))
        return 3

    def gh_main(args: list[str]) -> int:
        calls.append(("gh", args))
        return 4

    monkeypatch.setattr(tokens, "credential_main", credential_main)
    monkeypatch.setattr(tokens, "gh_main", gh_main)
    for argv, code in (
        (["_git_credential", "get"], 3),
        (["_gh", "--", "pr", "--", "x"], 4),
    ):
        monkeypatch.setattr(sys, "argv", ["claude_sandbox", *argv])
        with pytest.raises(SystemExit) as done:
            runpy.run_module("claude_sandbox", run_name="__main__")
        assert done.value.code == code
    assert calls == [("credential", ["get"]), ("gh", ["pr", "--", "x"])]


# --- gh-auth --add / --status / --forget -----------------------------------


@pytest.fixture
def run(monkeypatch: pytest.MonkeyPatch, home: Path, server: str) -> Callable[..., int]:
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(tokens, "API", server)
    secrets: list[str] = []

    def secret(prompt: str) -> str:
        return secrets.pop(0)

    monkeypatch.setattr(auth, "read_secret", secret)

    def main(*argv: str, secret: tuple[str, ...] = ()) -> int:
        secrets[:] = list(secret)
        monkeypatch.setattr(context, "current", lambda: CONTAINER)
        return cli.main(list(argv))

    return main


def test_gh_auth_add(
    run: Callable[..., int], home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert (
        run("gh-auth", "--add", "a", "--add", "b", secret=("tok-acme", "noexpiry")) == 0
    )
    store = Store(str(home))
    assert store.names() == ["a", "b"] and store.token("b") == "noexpiry"
    assert stat.S_IMODE((store.dir / "a.token").stat().st_mode) == 0o600
    assert store.state()["expires"] == {"a": EXPIRY}
    out = capsys.readouterr().out
    assert "Stored" in out and "tok-acme" not in out


@pytest.mark.parametrize("name", ["default", "../x", ".x"])
def test_gh_auth_add_refuses_a_bad_name(
    run: Callable[..., int], home: Path, name: str, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run("gh-auth", "--add", name, secret=("tok-acme",)) == 2
    assert "bad token name" in capsys.readouterr().err
    assert not (home / ".config/gh/scoped").exists()


def test_gh_auth_add_refuses_empty_and_rejected_tokens(
    run: Callable[..., int], home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run("gh-auth", "--add", "a", secret=("",)) == 1
    assert run("gh-auth", "--add", "a", secret=("bad",)) == 1
    err = capsys.readouterr().err
    assert "no token given" in err and "rejected" in err
    assert Store(str(home)).names() == []


def test_gh_auth_add_refuses_an_unsafe_store(
    run: Callable[..., int], home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (home / ".config/gh/scoped").mkdir(parents=True, mode=0o755)
    (home / ".config/gh/scoped").chmod(0o755)
    assert run("gh-auth", "--add", "a", secret=("tok-acme",)) == 1
    assert "nothing stored" in capsys.readouterr().err
    assert not (home / ".config/gh/scoped/a.token").exists()


def test_gh_auth_add_stores_unchecked_when_github_is_silent(
    home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def secret(prompt: str) -> str:
        return "tok"

    monkeypatch.setattr(auth, "read_secret", secret)
    assert auth.gh_add("a", str(home), api="http://127.0.0.1:9") == 0
    assert "unchecked" in capsys.readouterr().out
    assert Store(str(home)).token("a") == "tok"


def test_gh_auth_status_and_forget(
    run: Callable[..., int], home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    store = stored(home, work="tok-acme", other="tok-beta")
    store.add("work", "tok-acme", EXPIRY)
    store.remember("acme/app", "work")
    store.remember("acme/lib", "work")
    store.remember("beta/lib", DEFAULT, fallback=True)
    store.remember("old/one", "gone")
    capsys.readouterr()
    assert run("gh-auth", "--status") == 0
    out = capsys.readouterr().out.splitlines()
    assert out == [
        "default: gh's own login (gh auth status shows its account and expiry)",
        "    repos: beta/lib (no token could push)",
        "other: expiry unknown",
        "    repos: none cached yet",
        f"work: expires {EXPIRY}",
        "    repos: acme/app, acme/lib",
        "gone: token file missing (probed afresh on next use)",
        "    repos: old/one",
    ]
    assert not any("tok-" in line for line in out)
    assert run("gh-auth", "--forget", "https://github.com/Acme/App") == 0
    assert store.lookup("acme/app") is None and store.lookup("acme/lib") == "work"
    assert run("gh-auth", "--forget", "nonsense") == 2
    assert run("gh-auth", "--forget") == 0
    assert store.state()["repos"] == {}
    assert "Forgot 1 cached repository." in capsys.readouterr().out


def test_gh_auth_options_are_exclusive(run: Callable[..., int]) -> None:
    with pytest.raises(SystemExit) as done:
        run("gh-auth", "--status", "--forget")
    assert done.value.code == 2
