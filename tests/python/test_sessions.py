"""host/sessions.py, which ends a launcher session's processes in the
container (issue #69), against real processes on this host."""

import os
import subprocess
import time
from pathlib import Path

import pytest

from claude_sandbox.host import sessions


def entry(pid: int) -> str:
    st = sessions.stat(pid)
    assert st
    return f"{pid} {st[1]}\n"


def gone(pid: int) -> bool:
    for _ in range(50):
        if sessions.stat(pid) is None:
            return True
        time.sleep(0.1)
    return False


def test_stat_and_live(tmp_path: Path) -> None:
    me = os.getpid()
    assert sessions.stat(me) == (os.getppid(), entry(me).split()[1])
    assert sessions.live(entry(me)) == me
    assert sessions.live(f"{me} 1\n") is None  # same PID, another process
    assert sessions.live("") is None and sessions.live("x y") is None
    (tmp_path / "7").mkdir()
    (tmp_path / "7" / "stat").write_text("7 (z) Z 1 " + "0 " * 20)
    (tmp_path / "8").mkdir()
    (tmp_path / "8" / "stat").write_text("8 (odd)")
    assert sessions.stat(7, str(tmp_path)) is None  # a zombie
    assert sessions.stat(8, str(tmp_path)) is None  # malformed
    assert sessions.tree(9, str(tmp_path)) == {}


def test_end_hangs_up_the_whole_tree_and_counts_the_rest(tmp_path: Path) -> None:
    """A HUP-ignoring child and one in its own session go too; another
    session, and a stale record, are counted and forgotten respectively."""
    script = (
        "setsid sleep 300 & sh -c 'trap \"\" HUP; sleep 300 & wait' & echo ready; wait"
    )
    ours = subprocess.Popen(["sh", "-c", script], stdout=subprocess.PIPE, text=True)
    other = subprocess.Popen(["sleep", "300"])
    try:
        assert ours.stdout and ours.stdout.readline() == "ready\n"
        time.sleep(0.2)
        members = sessions.tree(ours.pid)
        assert len(members) == 4  # sh, the setsid sleep, sh, its sleep
        (tmp_path / "ours").write_text(entry(ours.pid))
        (tmp_path / "other").write_text(entry(other.pid))
        (tmp_path / "stale").write_text("1 0\n")
        assert sessions.end("ours", str(tmp_path), grace=0.5) == 1
        ours.wait(timeout=5)
        assert all(gone(pid) for pid in members)
        assert other.poll() is None
        assert sorted(p.name for p in tmp_path.iterdir()) == ["other"]
    finally:
        for proc in (ours, other):
            proc.kill()
            proc.wait()
    # Its record says it is gone: nothing to hang up, nothing left.
    assert sessions.end("other", str(tmp_path)) == 0
    assert sessions.end("other", str(tmp_path / "none")) == 0


def test_start_records_this_process_then_execs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ran: list[list[str]] = []

    def execv(path: str, argv: list[str]) -> None:
        ran.append(argv)
        if path == "/missing":
            raise FileNotFoundError(2, "No such file or directory")

    monkeypatch.setattr(os, "execv", execv)
    state = tmp_path / "state"
    sessions.start("abc", ["/bin/true", "x"], str(state))
    assert ran == [["/bin/true", "x"]]
    assert sessions.live((state / "abc").read_text()) == os.getpid()
    with pytest.raises(SystemExit) as exc:
        sessions.start("def", ["/missing"], str(state))
    assert exc.value.code == 127 and not (state / "def").exists()
    assert "cannot run /missing" in capsys.readouterr().err


def test_main(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    calls: list[tuple[str, list[str]]] = []

    def start(sid: str, command: list[str]) -> None:
        calls.append((sid, command))

    def end(sid: str) -> int:
        return 2

    monkeypatch.setattr(sessions, "start", start)
    monkeypatch.setattr(sessions, "end", end)
    assert sessions.main(["start", "a1", "/bin/sh", "-c", "x"]) == 0
    assert calls == [("a1", ["/bin/sh", "-c", "x"])]
    assert sessions.main(["end", "a1"]) == 0
    assert capsys.readouterr().out == "2\n"
    for bad in (["start", "a1"], ["end", "../x"], []):
        assert sessions.main(bad) == 2
    assert "usage" in capsys.readouterr().err
