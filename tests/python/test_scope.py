"""What a session's jail shows of host paths (scope.py), for the VS Code
extension: read off the bwrap argv, never guessed."""

import json
import runpy
import sys
from collections.abc import Mapping
from pathlib import Path

import pytest

from claude_sandbox import scope
from claude_sandbox.bwrap import Probe
from claude_sandbox.errors import SandboxError
from claude_sandbox.scope import Mount, View, main, mounts, session_argv, view

ARGV = [
    "bwrap",
    "--ro-bind", "/", "/",
    "--dev", "/dev",
    "--ro-bind", "/proc", "/proc",
    "--tmpfs", "/tmp",
    "--tmpfs", "/home/u",
    "--bind", "/home/u/.cache", "/home/u/.cache",
    "--bind", "/ws", "/ws",
    "--ro-bind", "/dev/null", "/ws/claude",
    "--bind-try", "/dev/null", "/home/u/.netrc",
    "--ro-bind", "/staged/resolv", "/etc/resolv.conf",
    "--cap-drop", "ALL",
    "--unshare-pid",
    "--clearenv",
    "--setenv", "HOME", "/home/u",
    "--", "/home/u/.local/bin/claude", "--tmpfs", "/ws",
]  # fmt: skip


class Paths(Probe):
    """Nothing resolves, but /link, a symlink to /ws."""

    def realpath(self, path: str) -> str:
        if path == "/link" or path.startswith("/link/"):
            return "/ws" + path.removeprefix("/link")
        raise OSError(2, "No such file or directory")


@pytest.mark.parametrize(
    ("path", "want"),
    [
        ("/ws", View(True, True)),
        ("/ws/src/a.py", View(True, True)),
        ("/ws/claude", View(False, False)),  # /dev/null over it
        ("/other/project", View(True, False)),  # the read-only root
        ("/home/u", View(False, False)),  # the tmpfs over $HOME
        ("/home/u/.ssh/id_rsa", View(False, False)),
        ("/home/u/.cache/x", View(True, True)),  # bound back
        ("/home/u/.netrc", View(False, False)),
        ("/tmp/x", View(False, False)),
        ("/dev/sda", View(False, False)),  # a fresh /dev
        ("/etc/resolv.conf", View(False, False)),  # another file shown there
        ("/proc/1", View(True, False)),
        ("/link/a.py", View(True, True)),  # resolved first
        ("/ws/../home/u/.ssh", View(False, False)),
        ("relative/a.py", View(False, False)),
    ],
)
def test_the_last_mount_over_a_path_decides(path: str, want: View) -> None:
    assert view(ARGV, path, Paths()) == want


def test_mounts_stop_at_the_command() -> None:
    found = mounts(ARGV)
    assert found[0] == Mount("--ro-bind", "/", "/")
    assert Mount("--tmpfs", "", "/tmp") in found
    assert found[-1] == Mount("--ro-bind", "/staged/resolv", "/etc/resolv.conf")


def test_an_unknown_option_or_a_short_one_is_an_error() -> None:
    with pytest.raises(SandboxError, match="unknown bwrap option --overlay"):
        mounts(["bwrap", "--overlay", "/a", "/b"])
    with pytest.raises(SandboxError, match="--bind is missing"):
        mounts(["bwrap", "--bind", "/a"])


def test_no_mount_at_all_shows_nothing() -> None:
    assert view(["bwrap", "--", "x"], "/ws", Paths()) == View(False, False)


def test_every_option_the_real_argv_uses_is_known(tmp_path: Path) -> None:
    conf = tmp_path / "conf"
    conf.write_text("allow-write = /var\n")
    env = {"HOME": str(tmp_path), "PATH": "/usr/bin", "TERM": "xterm"}
    argv = session_argv("/workspaces/foo", env, str(conf))
    assert mounts(argv)  # no SandboxError
    assert view(argv, "/var/lib").write
    assert not view(argv, str(tmp_path / ".ssh")).read
    assert view(argv, "/usr/bin/env") == View(True, False)


def test_workspace_root_widens_what_can_be_written(tmp_path: Path) -> None:
    env = {"HOME": str(tmp_path), "PATH": "/usr/bin"}
    conf = tmp_path / "conf"
    conf.write_text("")
    narrow = session_argv(str(tmp_path), env, str(conf))
    wide = session_argv(
        str(tmp_path), {**env, "CLAUDE_SANDBOX_WORKSPACE_ROOT": "/usr"}, str(conf)
    )
    assert view(narrow, "/usr/lib") == View(True, False)
    assert view(wide, "/usr/lib") == View(True, True)


def test_main_prints_a_line_per_path(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    env = {"HOME": str(tmp_path), "PATH": "/usr/bin"}
    conf = str(tmp_path / "none")
    assert main(["/no/ws", "--", str(tmp_path), "/usr", "x"], env, conf) == 0
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert lines == [
        {"path": str(tmp_path), "read": False, "write": False},  # $HOME
        {"path": "/usr", "read": True, "write": False},
        {"path": "x", "read": False, "write": False},
    ]


def test_main_refuses_bad_usage_and_a_refused_launch(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    env = {"HOME": str(tmp_path)}
    for args in ([], ["/ws"], ["/ws", "/x"], ["ws", "--"]):
        assert main(args, env) == 2
    assert "usage:" in capsys.readouterr().err
    conf = tmp_path / "conf"
    conf.write_text("allow-write = relative\n")
    assert main(["/ws", "--", "/ws"], env, str(conf)) == 1
    assert "allow-write needs an absolute path" in capsys.readouterr().err


def test_main_reads_the_environment_by_default(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def fixed(cwd: str, env: Mapping[str, str], conf: str) -> list[str]:
        assert env.get("PATH")  # os.environ's
        return ARGV

    monkeypatch.setattr(scope, "session_argv", fixed)
    assert main(["/ws", "--", "/ws/a"]) == 0
    assert json.loads(capsys.readouterr().out)["write"] is True


def test_the_front_door_dispatches_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[list[str]] = []

    def fake(args: list[str]) -> int:
        seen.append(args)
        return 3

    monkeypatch.setattr(scope, "main", fake)
    monkeypatch.setattr(sys, "argv", ["claude_sandbox", "_scope", "/ws", "--", "/a"])
    with pytest.raises(SystemExit) as done:
        runpy.run_module("claude_sandbox", run_name="__main__")
    assert done.value.code == 3
    assert seen == [["/ws", "--", "/a"]]
