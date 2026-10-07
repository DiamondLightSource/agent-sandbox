"""The port's process risks (ADR 26): signals, the terminal, exec.

Each test runs ``shadow.main`` in its own process through shadow_driver.py,
with the real script(1) and a fake bwrap that runs the agent unsandboxed.
The pseudo-terminal tests give that process a controlling terminal with
the stdlib ``pty`` module, so Ctrl-C is a byte typed at a terminal, as it
is for a user, not a signal sent by the test.

With the egress jail off the shadow execs script(1) and is gone before the
agent starts, so SIGINT while the agent owns the terminal is script's and
the agent's business; the test pins that nothing in the Python process is
left in the way. The egress jail keeps a parent process alive; its terminal
tests are in test_jail_netns.py.
"""

import json
import os
import pty
import select
import shutil
import signal
import subprocess
import sys
import termios
import time
from dataclasses import dataclass
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
SRC = HERE.parents[1] / "src"
DRIVER = HERE / "shadow_driver.py"

AGENT = """#!/bin/bash
grep '^SigIgn' /proc/self/status
trap 'echo GOT-INT; exit 42' INT
echo AGENT-READY
if [ "${1:-}" = wait ]; then while :; do sleep 0.1; done; fi
exit 42
"""
FAKE_BWRAP = '#!/bin/bash\nwhile [ "$1" != -- ]; do shift; done\nshift\nexec "$@"\n'

pytestmark = pytest.mark.skipif(
    shutil.which("script") is None, reason="needs script(1) from util-linux"
)


@dataclass
class Launch:
    root: Path
    argv: list[str]
    env: dict[str, str]

    def leftovers(self) -> list[str]:
        return sorted(p.name for p in (self.root / "etc").iterdir())


def launch(
    root: Path,
    *args: str,
    persistent: bool = True,
    sig: int = 0,
    agent: str = "pi",
    bwrap: str = FAKE_BWRAP,
) -> Launch:
    for path, text in (
        ("bin/bwrap", bwrap),
        ("bin/git", "#!/bin/sh\nexit 1\n"),
        ("agent", AGENT),
    ):
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_text(text)
        (root / path).chmod(0o755)
    (root / "etc").mkdir(exist_ok=True)
    (root / "etc/claude-sandbox.conf").write_text("egress-jail = 0\n")
    (root / "skills").mkdir(exist_ok=True)
    (root / "home").mkdir(exist_ok=True)
    if persistent and not (root / "home/.pi").exists():
        (root / "shared").mkdir()
        (root / "home/.pi").symlink_to(root / "shared")
    spec = {
        "src": str(SRC),
        "real": str(root / "agent"),
        "conf": str(root / "etc/claude-sandbox.conf"),
        "gitconfig": str(root / "etc/claude-gitconfig"),
        "skills": str(root / "skills"),
        "tools": str(root / "bin"),
        "state": str(root / "state"),
        "signal": sig,
    }
    argv = [sys.executable, "-I", str(DRIVER), json.dumps(spec), agent, *args]
    env = {
        "HOME": str(root / "home"),
        "PATH": f"{root / 'bin'}:/usr/bin:/bin",
        "TERM": "dumb",
    }
    return Launch(root, argv, env)


def in_pty(ln: Launch, script: list[tuple[bytes, bytes]]) -> tuple[bytes, int, int]:
    """Run ``ln`` on a new terminal; type each reply once its prompt shows.

    Returns the output, the wait status, and the terminal's attributes after
    the process exits.
    """
    pid, master = pty.fork()
    if pid == 0:  # pragma: no cover - the child execs at once
        os.chdir(ln.root)
        os.execve(ln.argv[0], ln.argv, ln.env)
    out = b""
    deadline = time.monotonic() + 20
    pending = list(script)
    while time.monotonic() < deadline:
        if pending and pending[0][0] in out:
            os.write(master, pending.pop(0)[1])
        ready, _, _ = select.select([master], [], [], 0.1)
        if not ready:
            continue
        try:
            chunk = os.read(master, 4096)
        except OSError:  # EIO: the terminal's last reader has gone
            break
        if not chunk:
            break
        out += chunk
    attrs = termios.tcgetattr(master)
    _, status = os.waitpid(pid, 0)
    os.close(master)
    return out, status, int(attrs[3])


def ignored_signals(out: bytes) -> int:
    line = next(x for x in out.splitlines() if x.startswith(b"SigIgn:"))
    return int(line.split()[1], 16)


def test_pause_waits_for_a_key_then_launches(tmp_path: Path) -> None:
    out, status, _ = in_pty(
        launch(tmp_path, persistent=False), [(b"Press any key", b"x")]
    )
    assert b"is not host-mounted" in out
    assert b"AGENT-READY" in out
    assert os.waitstatus_to_exitcode(status) == 42  # script --return
    # Python ignores SIGPIPE and SIGXFSZ; the agent must not inherit that.
    mask = ignored_signals(out)
    for sig in (signal.SIGPIPE, signal.SIGXFSZ):
        assert not mask & (1 << (sig - 1)), sig


def test_ctrl_c_at_the_pause_cancels_and_restores_the_terminal(
    tmp_path: Path,
) -> None:
    out, status, lflag = in_pty(
        launch(tmp_path, persistent=False), [(b"Press any key", b"\x03")]
    )
    assert b"AGENT-READY" not in out
    assert os.waitstatus_to_exitcode(status) == -signal.SIGINT
    assert lflag & termios.ECHO and lflag & termios.ICANON


def test_ctrl_c_while_the_agent_owns_the_terminal_reaches_the_agent(
    tmp_path: Path,
) -> None:
    out, status, _ = in_pty(launch(tmp_path, "wait"), [(b"AGENT-READY", b"\x03")])
    assert b"Press any key" not in out
    assert b"GOT-INT" in out
    assert os.waitstatus_to_exitcode(status) == 42


@pytest.mark.parametrize("sig", [signal.SIGINT, signal.SIGTERM, signal.SIGHUP])
def test_a_signal_mid_launch_cleans_up_and_kills(tmp_path: Path, sig: int) -> None:
    ln = launch(tmp_path, sig=sig)
    proc = subprocess.run(
        ln.argv, env=ln.env, cwd=tmp_path, capture_output=True, check=False
    )
    assert proc.returncode == -sig, proc.stderr
    # The git config's temporary file is gone; the real one never landed.
    assert ln.leftovers() == ["claude-sandbox.conf"]


@pytest.mark.parametrize("agent", ["claude", "codex", "pi"])
def test_arguments_survive_the_terminal_shell(tmp_path: Path, agent: str) -> None:
    """script(1) runs the bwrap command through a shell: every argument and
    forwarded value reaches bwrap exactly as built, newlines and `$(...)`
    included."""
    capture = tmp_path / "argv"
    recorder = f"#!/bin/bash\nprintf '%s\\0' \"$@\" > {capture}\n"
    prompt, value = "first line\nsecond line\n", "first value\nsecond value\n"
    ln = launch(tmp_path, prompt, "", "literal $(false)", agent=agent, bwrap=recorder)
    ln.env |= {"MULTILINE_VALUE": value, "CLAUDE_SANDBOX_PASS_ENV": "MULTILINE_VALUE"}
    proc = subprocess.run(
        ln.argv,
        env=ln.env,
        cwd=tmp_path,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    got = capture.read_bytes().decode().split("\0")[:-1]
    assert got[-3:] == [prompt, "", "literal $(false)"]
    i = got.index("MULTILINE_VALUE")
    assert got[i - 1 : i + 2] == ["--setenv", "MULTILINE_VALUE", value]
