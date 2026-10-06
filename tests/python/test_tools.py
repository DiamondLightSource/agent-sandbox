"""tools.find_tool: a fixed search path, never PATH."""

from pathlib import Path

import pytest

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
