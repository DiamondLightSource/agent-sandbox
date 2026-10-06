"""Unit tests for the pure modules, beyond the comparison harness.

Literal expectations, so they outlive the bash (issue #72 phase 5), plus
the branches the harness can reach only on some hosts. Where a table also
asks the bash, it is to keep the literal honest while the bash still ships.
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from claude_sandbox.bwrap import (
    ENTRY_GUARD_ENV,
    ENTRY_POINTS,
    HOST,
    bwrap_argv,
    path_ahead_of_shadow,
)
from claude_sandbox.config import Config, valid_tcp_port
from claude_sandbox.errors import SandboxError
from claude_sandbox.gitconfig import render_gitconfig
from claude_sandbox.profiles import (
    agent_profile,
    detect_agent,
    filter_chrome_args,
)
from test_parity import driver


@pytest.mark.parametrize(
    ("argv0", "override", "expected"),
    [
        ("/usr/local/bin/claude", "", "claude"),
        ("/usr/local/bin/codex", "", "codex"),
        ("/usr/local/bin/pi", "", "pi"),
        ("/tmp/bwrap_argv.sh", "", "claude"),  # anything else falls back
        ("codex/", "", "claude"),
        ("", "", "claude"),
        ("/usr/local/bin/claude", "codex", "codex"),  # the override wins...
        # ...but only inside the closed set.
        (
            "/usr/local/bin/claude",
            "/bin/sh",
            "claude-sandbox: unknown CLAUDE_SANDBOX_AGENT '/bin/sh'"
            " (expected: claude, codex, pi).",
        ),
    ],
)
def test_detect_agent(tmp_path: Path, argv0: str, override: str, expected: str) -> None:
    try:
        got = detect_agent(argv0, override)
    except SandboxError as e:
        got = str(e)
    assert got == expected
    sh = driver(tmp_path, {}, "call", "detect_agent", argv0, override)
    assert os.fsdecode(sh.stdout or sh.stderr).rstrip("\n") == expected


def test_unknown_profile_is_refused(tmp_path: Path) -> None:
    with pytest.raises(SandboxError) as e:
        agent_profile("bash")
    assert str(e.value) == "claude-sandbox: unknown agent 'bash'."
    sh = driver(tmp_path, {}, "call", "agent_profile", "bash")
    assert (sh.returncode, os.fsdecode(sh.stderr)) == (1, f"{e.value}\n")


def test_filter_chrome_args() -> None:
    args = ["--chrome", "-p", "--chrome=x", "", "--chrome"]
    assert filter_chrome_args(args) == ["-p", "--chrome=x", ""]


@pytest.mark.parametrize(
    ("port", "ok"),
    [
        ("1", True),
        ("65535", True),
        ("0", False),
        ("65536", False),
        ("01", False),
        ("+1", False),
        ("1920\n", False),
        ("١٢", False),  # non-ASCII digits
        ("", False),
    ],
)
def test_valid_tcp_port(port: str, ok: bool) -> None:
    assert valid_tcp_port(port) is ok


GIT_IDENTITIES = [
    ("Ann Smith", "ann@example.com", False),
    (None, None, False),  # unset: empty values
    (" lead", "a;b#c", True),
    ('say "hi" \\o/ \t', "line\nbreak", False),
]


@pytest.mark.parametrize(("name", "email", "no_forge"), GIT_IDENTITIES)
def test_gitconfig_matches_bash(
    tmp_path: Path, name: str | None, email: str | None, no_forge: bool
) -> None:
    git = shutil.which("git")
    if git is None:
        pytest.skip("the bash renderer needs git")
    home = tmp_path / "home"
    home.mkdir()
    for key, value in (("user.name", name), ("user.email", email)):
        if value is not None:
            subprocess.run(
                [git, "config", "--file", str(home / ".gitconfig"), key, value],
                check=True,
            )
    env = {
        "HOME": str(home),
        "PATH": f"{os.path.dirname(git)}:/usr/bin:/bin",
        "GIT_CONFIG_NOSYSTEM": "1",
        **({"CLAUDE_SANDBOX_NO_FORGE": "1"} if no_forge else {}),
    }
    out = tmp_path / "gitconfig"
    driver(tmp_path, env, "gitconfig", str(out)).check_returncode()
    rendered = render_gitconfig(name or "", email or "", no_forge=no_forge)
    assert rendered == out.read_text()


def test_gitconfig_text() -> None:
    text = render_gitconfig("Ann", "cr\r", no_forge=False)
    assert text.startswith('[credential "https://github.com"]\n')
    assert text.endswith('[user]\n\tname = Ann\n\temail = "cr\r"\n')
    assert "[credential" not in render_gitconfig("", "", no_forge=True)


class HostWithEverything:
    """A host that has what CI runners lack: /run/secrets, readable
    /etc/shadow, GPU nodes, a block device and a resolver override."""

    def is_dir(self, path: str) -> bool:
        return path.startswith("/run/")

    def is_file(self, path: str) -> bool:
        return False

    def exists(self, path: str) -> bool:
        return False

    def readable(self, path: str) -> bool:
        return True

    def is_char_device(self, path: str) -> bool:
        return path == "/dev/nvidia0"

    def is_block_device(self, path: str) -> bool:
        return path == "/dev/sda"

    def realpath(self, path: str) -> str:
        return path

    def glob(self, pattern: str) -> list[str]:
        return {"/dev/nvidia*": ["/dev/nvidia-caps", "/dev/nvidia0"]}.get(pattern, [])


def test_host_only_branches() -> None:
    config = Config(gpu=True, allow_devices="/dev/sda")
    env = {"HOME": "/h", "CLAUDE_SANDBOX_JAIL_RESOLV": "/r"}
    claude = agent_profile("claude")
    argv = bwrap_argv(claude, config, env, "", "/real", [], probe=HostWithEverything())
    i = argv.index("--dev-bind")
    assert argv[i : i + 12] == [
        "--dev-bind", "/dev/sda", "/dev/sda",
        "--dev-bind", "/dev/nvidia0", "/dev/nvidia0",
        "--tmpfs", "/run/user",
        "--tmpfs", "/run/secrets",
        "--tmpfs", "/h",
    ]  # fmt: skip
    # A GPU glob can list a dangling link; the real probe says "no".
    assert not HOST.is_char_device("/dev/no-such-claude-device")
    i = argv.index("/h/.ICEauthority") + 1
    assert argv[i : i + 12] == [
        "--bind", "/dev/null", "/etc/shadow",
        "--bind", "/dev/null", "/etc/gshadow",
        "--bind", "/dev/null", "/etc/sudoers",
        "--ro-bind", "/r", "/etc/resolv.conf",
    ]  # fmt: skip


# --- the entry-point guard ----------------------------------------------------


def test_path_ahead_of_shadow(tmp_path: Path) -> None:
    t = os.path.realpath(tmp_path)
    for d in ("a", "b", "after"):
        os.mkdir(f"{t}/{d}")
    os.symlink(f"{t}/a", f"{t}/link")
    os.symlink("/usr/local/bin", f"{t}/shadow-link")
    open(f"{t}/file", "w").close()
    entries = [
        "",  # empty: skipped
        "rel",  # relative: skipped
        f"{t}/a",
        f"{t}/link",  # resolves to a, which is already listed
        f"{t}/missing",
        f"{t}/file",
        f"{t}/b/",
        "/usr/local/bin/",  # the shadow's directory ends the list
        f"{t}/after",
    ]
    assert path_ahead_of_shadow(":".join(entries)) == [f"{t}/a", f"{t}/b"]
    # Reached through a symlink, the shadow's directory still ends it.
    assert path_ahead_of_shadow(f"{t}/b:{t}/shadow-link:{t}/a") == [f"{t}/b"]
    # Not on PATH at all: every directory is ahead of it.
    assert path_ahead_of_shadow(f"{t}/b:{t}/a:{t}/b") == [f"{t}/b", f"{t}/a"]
    assert path_ahead_of_shadow("") == []


class GuardProbe(HostWithEverything):
    """No /usr/local/bin on this host, and one writable path that is gone by
    the time it is resolved."""

    def is_dir(self, path: str) -> bool:
        return path in {"/w", "/w/bin", "/c", "/c/venv/bin", "/elsewhere/bin"}

    def exists(self, path: str) -> bool:
        return path in {"/c", "/gone", "/"}

    def readable(self, path: str) -> bool:
        return False

    def realpath(self, path: str) -> str:
        if path in {"/usr/local/bin", "/gone"}:
            raise FileNotFoundError(2, "No such file or directory")
        return {"/opt/v/bin": "/c/venv/bin"}.get(path, path)


def guard_argv(path: str, allow_write: str = "/c\n/gone") -> list[str]:
    config = Config(allow_write=allow_write)
    env = {"HOME": "/h", "PATH": path}
    claude = agent_profile("claude")
    return bwrap_argv(claude, config, env, "/w", "/real", [], probe=GuardProbe())


def guard_binds(argv: list[str]) -> list[str]:
    return [
        argv[i + 2]
        for i in range(len(argv) - 2)
        if argv[i : i + 2] == ["--ro-bind", "/dev/null"]
    ]


def test_entry_guard_binds_each_name_in_each_writable_dir_ahead() -> None:
    argv = guard_argv("/w/bin:/opt/v/bin:/elsewhere/bin:/usr/local/bin:/c")
    expected = [f"{d}/{n}" for d in ("/w/bin", "/c/venv/bin") for n in ENTRY_POINTS]
    assert guard_binds(argv) == expected
    # After every read-write bind, so none of them can cover a guard bind.
    last_rw = max(i for i, a in enumerate(argv) if a == "--bind")
    assert argv.index(expected[0]) > last_rw
    # The last --setenv, so no pass-env name can replace it.
    i = max(j for j, a in enumerate(argv) if a == "--setenv")
    assert argv[i : i + 3] == ["--setenv", ENTRY_GUARD_ENV, "/w/bin:/c/venv/bin"]


def test_entry_guard_with_nothing_writable_ahead() -> None:
    argv = guard_argv("/usr/local/bin:/w/bin:/c/venv/bin")
    assert guard_binds(argv) == []
    assert ENTRY_GUARD_ENV not in argv


def test_entry_guard_under_an_allow_write_of_root() -> None:
    argv = guard_argv("/elsewhere/bin", allow_write="/")
    assert guard_binds(argv) == [f"/elsewhere/bin/{n}" for n in ENTRY_POINTS]
