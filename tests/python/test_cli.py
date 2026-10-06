"""The one CLI: what each command does on the host, and the opt-in."""

import argparse
import os
import runpy
import sys
from pathlib import Path

import pytest

import claude_sandbox
from claude_sandbox import cli, context
from claude_sandbox.context import CONTAINER, HOST, Where
from claude_sandbox.host import commands, launcher
from claude_sandbox.host.options import Options

Sessions = list[tuple[list[str], bool, Options]]
BIN = "/usr/local/bin"


def code(argv: list[str]) -> int:
    """The exit status, whether main returns it or argparse exits with it."""
    try:
        return cli.main(argv)
    except SystemExit as exc:
        assert isinstance(exc.code, int)
        return exc.code


@pytest.fixture
def on_path(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Executables of every name the CLI runs, first on PATH: none may be used."""
    for name in ("claude", "codex", "pi", "claude-sandbox", "sh", "bash"):
        (tmp_path / name).write_text("#!/bin/sh\n")
        (tmp_path / name).chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ['PATH']}")


@pytest.fixture
def sessions(monkeypatch: pytest.MonkeyPatch) -> Sessions:
    """Every session the host commands start, instead of starting it."""
    started: Sessions = []

    class Fake:
        def __init__(self, opts: Options) -> None:
            self.opts = opts

        def session(self, command: list[str], *, pause: bool) -> int:
            started.append((command, pause, self.opts))
            return 7

        def clean(self, force: bool, images: bool) -> None:
            started.append((["clean", str(force), str(images)], False, self.opts))

    monkeypatch.setattr(launcher, "launcher", Fake)
    monkeypatch.setattr(context, "current", lambda: HOST)
    return started


@pytest.mark.parametrize(
    ("argv", "command", "pause"),
    [
        ([], [f"{BIN}/claude"], True),
        (["--recreate", "--resume"], [f"{BIN}/claude", "--resume"], True),
        (["--", "codex"], [f"{BIN}/codex"], True),
        (["pi", "--help", "-p", "hi"], [f"{BIN}/pi", "--help", "-p", "hi"], True),
        (["help"], [f"{BIN}/claude", "help"], True),  # claude's prompt on a host
        (["verify", "--agent", "pi"],
         [f"{BIN}/claude-sandbox", "verify", "--agent", "pi"], False),
        (["pi-local", "--port", "1"],
         [f"{BIN}/claude-sandbox", "pi-local", "--port", "1"], False),
        (["clean", "--images"], ["clean", "False", "True"], False),
    ],
)  # fmt: skip
def test_host_commands(
    sessions: Sessions,
    on_path: None,
    argv: list[str],
    command: list[str],
    pause: bool,
) -> None:
    rc = cli.main(argv)
    assert sessions[0][:2] == (command, pause)
    assert rc == (0 if command[0] == "clean" else 7)
    assert sessions[0][2].recreate == ("--recreate" in argv)


def test_shell_runs_the_shell_you_use(
    sessions: Sessions, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLAUDE_SANDBOX_SHELL", "fish")
    cli.main(["shell", "-c", "ls"])
    assert sessions[0][0] == [
        "/bin/sh",
        "-c",
        launcher.SHELL_SCRIPT,
        "_",
        "fish",
        "-c",
        "ls",
    ]
    monkeypatch.delenv("CLAUDE_SANDBOX_SHELL")
    cli.main(["shell"])
    assert sessions[1][0][4] in launcher.SHELLS


@pytest.mark.parametrize(
    ("argv", "rc", "stream", "text"),
    [
        (["--version"], 0, "out", "claude-sandbox 4.7.2\n"),
        (["--help"], 0, "out", "--mount-rw PATH"),
        (["--mount"], 1, "err", "--mount needs a PATH"),
        (["install"], 2, "err", "install is a uvx verb"),
        (["clean", "--venvs"], 2, "err", "unrecognized arguments: --venvs"),
    ],
)
def test_host_messages(
    sessions: Sessions,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    argv: list[str],
    rc: int,
    stream: str,
    text: str,
) -> None:
    monkeypatch.setenv("CLAUDE_SANDBOX_LAUNCHER_VERSION", "4.7.2")
    assert code(argv) == rc
    out = capsys.readouterr()
    assert text in (out.out if stream == "out" else out.err)
    assert not sessions


@pytest.mark.parametrize(
    ("where", "argv", "rc"),
    [(CONTAINER, ["bogus"], 2), (CONTAINER, ["shell"], 1), (HOST, ["help"], 1)],
)
def test_unknown_and_refused(
    monkeypatch: pytest.MonkeyPatch, where: Where, argv: list[str], rc: int
) -> None:
    # On the host `help` is claude's prompt; refuse it by bypassing that.
    monkeypatch.setattr(context, "current", lambda: where)

    def parse(args: list[str], verbs: list[str], version: str) -> Options:
        return Options(verb=argv[0])

    monkeypatch.setattr(cli.options, "parse", parse)
    assert code(argv) == rc


def test_interrupt_exits_130(monkeypatch: pytest.MonkeyPatch) -> None:
    def interrupt() -> Where:
        raise KeyboardInterrupt

    monkeypatch.setattr(context, "current", interrupt)
    monkeypatch.setattr(sys, "argv", ["claude-sandbox"])
    assert cli.main() == 130


def test_options_come_from_the_cli() -> None:
    with pytest.raises(AssertionError):
        commands.options(argparse.Namespace(opts=None))


# --- the opt-in, and python -m ----------------------------------------------


def test_opt_in_runs_the_python_cli_with_the_front_doors_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[tuple[list[str], str]] = []

    def fake(argv: list[str]) -> int:
        seen.append((argv, os.environ["CLAUDE_SANDBOX_LAUNCHER"]))
        return 5

    monkeypatch.setattr(cli, "main", fake)

    def execvpe(file: str, args: list[str], env: dict[str, str]) -> None:
        pytest.fail("exec'd the bash")

    monkeypatch.setattr(os, "execvpe", execvpe)
    monkeypatch.setattr(sys, "argv", ["claude-sandbox", "verify"])
    for var in ("LAUNCHER", "LAUNCHER_VERSION", "IMAGE"):  # restored after
        monkeypatch.setenv(f"CLAUDE_SANDBOX_{var}", "")
        monkeypatch.delenv(f"CLAUDE_SANDBOX_{var}")
    monkeypatch.setenv("CLAUDE_SANDBOX_IMPL", "python")
    with pytest.raises(SystemExit) as exc:
        claude_sandbox.main()
    assert exc.value.code == 5 and seen == [(["verify"], "uvx")]


def test_python_m_runs_the_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "main", lambda: 9)
    monkeypatch.setattr(sys, "argv", ["claude_sandbox", "x"])
    with pytest.raises(SystemExit) as exc:
        runpy.run_module("claude_sandbox", run_name="__main__")
    assert exc.value.code == 9


def test_every_command_says_where_it_runs() -> None:
    for name, command in cli.COMMANDS.items():
        assert context.requirement(command.fn).contexts, name
