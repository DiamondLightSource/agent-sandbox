"""tools.py: find_tool (a fixed search path, never PATH), and the helpers the
shadow, the launcher and the installer share."""

import os
import pty
import stat
import termios
from pathlib import Path

import pytest

from claude_sandbox import tools
from claude_sandbox.tools import TOOL_PATH, find_tool


def test_finds_the_first_executable_file(tmp_path: Path) -> None:
    first, second = tmp_path / "a", tmp_path / "b"
    for d in (first, second):
        d.mkdir()
    (first / "tool").write_text("")  # not executable: skipped
    (second / "tool").write_text("")
    (second / "tool").chmod(0o755)
    (first / "dir").mkdir()  # a directory is not a tool
    search = (str(first), str(second))
    assert find_tool("tool", search=search) == f"{second}/tool"
    assert find_tool("dir", search=search) is None
    assert find_tool("absent", search=search) is None
    assert find_tool("", search=search) is None
    assert find_tool("../b/tool", search=search) is None


def test_ignores_path(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    (tmp_path / "sh").write_text("#!/bin/sh\n")
    (tmp_path / "sh").chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))
    found = find_tool("sh")
    assert found is not None
    assert found.rpartition("/")[0] in TOOL_PATH


def test_output(tmp_path: Path) -> None:
    sh = "/bin/sh"
    assert tools.output([sh, "-c", "echo hi; echo err >&2; exit 3"]) == (3, "hi")
    assert tools.output([sh, "-c", "echo $X"], {"X": "y"}) == (0, "y")
    assert tools.output([str(tmp_path / "absent")]) == (127, "")
    assert tools.output([sh, "-c", "sleep 5"], timeout=0.1) == (127, "")


def test_write_atomic(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "f"
    target.symlink_to(tmp_path / "elsewhere")
    tools.write_atomic(target, b"data", 0o640)
    # The link is replaced, not followed.
    assert not (tmp_path / "elsewhere").exists()
    assert target.read_bytes() == b"data"
    assert stat.S_IMODE(target.stat().st_mode) == 0o640

    def fail(src: str, dst: str) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(os, "replace", fail)
    with pytest.raises(OSError, match="No space"):
        tools.write_atomic(str(target), b"new", 0o644)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["f"]


def test_read_key() -> None:
    master, slave = pty.openpty()
    try:
        before = termios.tcgetattr(slave)
        os.write(master, b"xy")
        assert tools.read_key(slave) == b"x"
        assert termios.tcgetattr(slave) == before
    finally:
        os.close(master)
        os.close(slave)


def test_working_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "real").mkdir()
    (tmp_path / "link").symlink_to(tmp_path / "real")
    monkeypatch.chdir(tmp_path / "link")
    real = os.getcwd()
    link = str(tmp_path / "link")
    assert tools.working_directory({"PWD": link}) == link
    assert tools.working_directory({"PWD": str(tmp_path)}) == real
    assert tools.working_directory({"PWD": "relative"}) == real
    assert tools.working_directory({"PWD": "/no/such/dir"}) == real
    assert tools.working_directory({}) == real
