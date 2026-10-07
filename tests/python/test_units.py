"""Unit tests for the pure modules: literal expectations, and the branches
of the argv builder only some hosts reach (a fake host stands in for them).
"""

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

from claude_sandbox.bwrap import (
    ENTRY_GUARD_ENV,
    ENTRY_POINTS,
    HOST,
    Probe,
    bwrap_build,
    path_ahead_of_shadow,
)
from claude_sandbox.config import Config, valid_tcp_port
from claude_sandbox.errors import SandboxError
from claude_sandbox.gitconfig import render_gitconfig
from claude_sandbox.profiles import (
    PROFILES,
    detect_agent,
    filter_chrome_args,
)


@pytest.mark.parametrize(
    ("argv0", "override", "expected"),
    [
        ("/usr/local/bin/claude", "", "claude"),
        ("/usr/local/bin/codex", "", "codex"),
        ("/usr/local/bin/pi", "", "pi"),
        ("/tmp/test-runner", "", "claude"),  # anything else falls back
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
def test_detect_agent(argv0: str, override: str, expected: str) -> None:
    try:
        got = detect_agent(argv0, override)
    except SandboxError as e:
        got = str(e)
    assert got == expected


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
    ("", "", False),  # unset: empty values
    (" lead", "a;b#c", True),
    ('say "hi" \\o/ \t', "line\nbreak", False),
    ('Sam "SJ" Jones', "sam@example.invalid", False),
    ("Sam #1; Jones", "sam@example.invalid", False),
    ("Sam\nJones", "sam@example.invalid", False),
    ("Sam\n[core]\n    hooksPath = /unexpected", "sam@example.invalid", False),
]


@pytest.mark.parametrize(("name", "email", "no_forge"), GIT_IDENTITIES)
def test_gitconfig_identity_round_trips(
    tmp_path: Path, name: str, email: str, no_forge: bool
) -> None:
    """What git reads back is the identity, never a directive of its own."""
    git = shutil.which("git")
    if git is None:
        pytest.skip("needs git to read the file back")
    path = tmp_path / "gitconfig"
    path.write_text(render_gitconfig(name, email, no_forge=no_forge))

    def get(*args: str) -> subprocess.CompletedProcess[str]:
        env = {"PATH": os.path.dirname(git), "HOME": str(tmp_path)}
        return subprocess.run(
            [git, "config", "--file", str(path), *args],
            capture_output=True,
            text=True,
            check=False,
            env=env,
        )

    assert get("user.name").stdout == f"{name}\n"
    assert get("user.email").stdout == f"{email}\n"
    assert get("--get", "core.hooksPath").returncode == 1
    helpers = get("--get-regexp", r"credential\..*helper").stdout.splitlines()
    assert len(helpers) == (0 if no_forge else 2)


def test_gitconfig_text() -> None:
    text = render_gitconfig("Ann", "cr\r", no_forge=False)
    assert text.startswith('[credential "https://github.com"]\n')
    assert text.endswith('[user]\n\tname = Ann\n\temail = "cr\r"\n')
    assert "[credential" not in render_gitconfig("", "", no_forge=True)


class HostWithEverything(Probe):
    """A host that has what CI runners lack: /run/secrets, readable
    /etc/shadow, GPU nodes, a block device and a resolver override."""

    def mode(self, path: str) -> int:
        if path.startswith("/run/"):
            return stat.S_IFDIR
        return {"/dev/nvidia0": stat.S_IFCHR, "/dev/sda": stat.S_IFBLK}.get(path, 0)

    def readable(self, path: str) -> bool:
        return True

    def realpath(self, path: str) -> str:
        return path

    def glob(self, pattern: str) -> list[str]:
        return {"/dev/nvidia*": ["/dev/nvidia-caps", "/dev/nvidia0"]}.get(pattern, [])


def test_host_only_branches() -> None:
    config = Config(gpu=True, allow_devices="/dev/sda")
    env = {"HOME": "/h", "CLAUDE_SANDBOX_JAIL_RESOLV": "/r"}
    claude = PROFILES["claude"]
    argv = bwrap_build(
        claude, config, env, "", "/real", [], probe=HostWithEverything()
    ).argv
    i = argv.index("--dev-bind")
    assert argv[i : i + 14] == [
        "--dev-bind", "/dev/sda", "/dev/sda",
        "--dev-bind", "/dev/nvidia0", "/dev/nvidia0",
        "--tmpfs", "/run/user",
        "--tmpfs", "/run/secrets",
        "--tmpfs", "/run/claude-sandbox",  # the PATH watcher's state
        "--tmpfs", "/h",
    ]  # fmt: skip
    # A GPU glob can list a dangling link; the real probe says "no".
    assert HOST.mode("/dev/no-such-claude-device") == 0
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

    def mode(self, path: str) -> int:
        dirs = {"/", "/w", "/w/bin", "/c", "/c/venv/bin", "/elsewhere/bin"}
        return stat.S_IFDIR if path in dirs else stat.S_IFREG * (path == "/gone")

    def readable(self, path: str) -> bool:
        return False

    def realpath(self, path: str) -> str:
        if path in {"/usr/local/bin", "/gone"}:
            raise FileNotFoundError(2, "No such file or directory")
        return {"/opt/v/bin": "/c/venv/bin"}.get(path, path)


def guard_argv(path: str, allow_write: str = "/c\n/gone") -> list[str]:
    config = Config(allow_write=allow_write)
    env = {"HOME": "/h", "PATH": path}
    claude = PROFILES["claude"]
    return bwrap_build(claude, config, env, "/w", "/real", [], probe=GuardProbe()).argv


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


def test_the_real_binary_is_bound_back_read_only() -> None:
    """A session cannot rewrite the binary later sessions run."""
    claude = PROFILES["claude"]
    argv = bwrap_build(claude, Config(), {"HOME": "/h"}, "", "/real", []).argv
    i = argv.index("/h/.local/bin/claude")
    assert argv[i - 2 : i + 1] == ["--ro-bind", "/real", "/h/.local/bin/claude"]
    for name in ("codex", "pi"):
        assert not PROFILES[name].bind_back  # exec'd in place, under /usr


@pytest.mark.parametrize("entry", ["cache", "./cache", "~/cache", "../x"])
def test_a_relative_allow_write_is_refused(entry: str) -> None:
    # Not skipped: bwrap would resolve it against the workspace, which a
    # session can write, so the conf line could bind a path it never named.
    with pytest.raises(SandboxError, match=f"absolute path: {entry}"):
        guard_argv("/usr/local/bin", allow_write=f"/c\n{entry}")
