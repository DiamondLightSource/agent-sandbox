"""The shadow launch path (shadow.py), with injected exec, env and paths.

Every launch ends in an exec; the fake execve raises ``Exec`` instead, so a
test sees exactly what would have run. The bash-equivalence of the same
launches is in test_parity.py; these pin behaviour the harness cannot reach.
"""

import os
import runpy
import shlex
import signal
import stat
import sys
from collections.abc import Callable, Generator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, replace
from functools import partial
from pathlib import Path
from typing import NoReturn

import pytest

from claude_sandbox import cli, jail, shadow, watch
from claude_sandbox.bwrap import bwrap_argv
from claude_sandbox.config import Config, parse_config
from claude_sandbox.errors import SandboxError
from claude_sandbox.profiles import PROFILES, VERIFY_BATTERY
from claude_sandbox.tools import find_tool

REPO = Path(__file__).resolve().parents[2]


class Exec(Exception):
    def __init__(self, path: str, argv: list[str], env: Mapping[str, str]) -> None:
        super().__init__(path)
        self.path, self.argv, self.env = path, list(argv), dict(env)


def fake_execve(path: str, argv: list[str], env: Mapping[str, str]) -> NoReturn:
    raise Exec(path, argv, env)


@dataclass
class Fixture:
    root: Path
    host: shadow.Host
    env: dict[str, str]
    forked: list[watch.Session]  # the jail-off watchers a launch started

    @property
    def home(self) -> Path:
        return self.root / "home"

    def run(self, *args: str, argv0: str = "claude") -> Exec:
        with pytest.raises(Exec) as exc:
            shadow.run(argv0, args, self.env, self.host)
        return exc.value

    def refused(self, *args: str, argv0: str = "claude") -> int:
        with pytest.raises(SystemExit) as exc:
            shadow.run(argv0, args, self.env, self.host)
        assert isinstance(exc.value.code, int)
        return exc.value.code


def executable(path: Path, text: str = "#!/bin/sh\n") -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(0o755)
    return str(path)


@pytest.fixture
def fx(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Fixture:
    libexec = tmp_path / "libexec"
    profiles = {
        "claude": replace(
            PROFILES["claude"], real=executable(libexec / "claude", "real claude")
        ),
        "codex": replace(
            PROFILES["codex"],
            real=executable(libexec / "codex-dist/bin/codex"),
            exec_via=executable(libexec / "codex-launch"),
        ),
        "pi": replace(PROFILES["pi"], real=executable(libexec / "pi-run")),
    }
    (tmp_path / "etc").mkdir()
    (tmp_path / "etc/claude-sandbox.conf").write_text("egress-jail = 0\n")
    (tmp_path / "home").mkdir()
    (tmp_path / "work").mkdir()
    (tmp_path / "skills/alpha").mkdir(parents=True)
    tools = tmp_path / "tools"
    executable(tools / "script")
    executable(tools / "bwrap")
    identity = {"user.name": "A U Thor", "user.email": "a@example.invalid"}
    forked: list[watch.Session] = []
    host = shadow.Host(
        config_path=str(tmp_path / "etc/claude-sandbox.conf"),
        gitconfig_path=str(tmp_path / "etc/claude-gitconfig"),
        shipped_skills_dir=str(tmp_path / "skills"),
        profiles=profiles,
        execve=fake_execve,
        git_config_get=lambda key, env: identity[key],
        mountinfo=str(tmp_path / "mountinfo"),
        find_tool=lambda name: find_tool(name, search=(str(tools),)),
        state_dir=str(tmp_path / "state"),
        fork_watcher=forked.append,
    )
    (tmp_path / "mountinfo").write_text("")
    monkeypatch.chdir(tmp_path / "work")
    env = {"HOME": str(tmp_path / "home"), "PATH": str(tmp_path / "bin")}
    return Fixture(tmp_path, host, env, forked)


def test_launch_wraps_the_bwrap_argv_in_script(
    fx: Fixture, capsys: pytest.CaptureFixture[str]
) -> None:
    ex = fx.run("--chrome", "a b", "")
    script = str(fx.root / "tools/script")
    assert ex.path == script
    assert ex.argv[:6] == [script, "--return", "-q", "-E", "never", "-c"]
    assert ex.argv[7:] == ["/dev/null"]
    env = parse_config(fx.host.config_path, fx.env)
    expected = bwrap_argv(
        fx.host.profiles["claude"],
        Config.from_env(env),
        env,
        str(fx.root / "work"),
        fx.host.profiles["claude"].real,
        ["--chrome", "a b", ""],
        shipped_skills_dir=fx.host.shipped_skills_dir,
        gitconfig_path=fx.host.gitconfig_path,
        state_dir=fx.host.state_dir,
    )
    i = expected.index(fx.host.state_dir)  # created, then masked
    assert expected[i - 1] == "--tmpfs"
    # The jail is off: a child watches while script(1) runs.
    assert [s.roots[-1] for s in fx.forked] == [str(fx.root / "work")]
    # bwrap by absolute path, so the inner shell looks nothing up.
    assert shlex.split(ex.argv[6]) == [str(fx.root / "tools/bwrap"), *expected[1:]]
    assert expected[-3:] == ["--no-chrome", "a b", ""]
    assert ex.env == {**env, "SHELL": "/bin/bash"}
    # What the builder binds exists now.
    for rel in (".config/gh", ".config/glab-cli", ".claude/skills", ".agents/skills"):
        assert (fx.home / rel).is_dir(), rel
    assert (fx.home / ".claude.json").is_file()
    gitconfig = Path(fx.host.gitconfig_path)
    assert stat.S_IMODE(gitconfig.stat().st_mode) == 0o644
    assert "\tname = A U Thor\n" in gitconfig.read_text()
    assert "gh auth git-credential" in gitconfig.read_text()
    assert [p.name for p in gitconfig.parent.iterdir()] != []  # no temp left
    assert sorted(p.name for p in gitconfig.parent.iterdir()) == [
        "claude-gitconfig",
        "claude-sandbox.conf",
    ]
    assert "~/.claude is not host-mounted" in capsys.readouterr().err


def test_no_forge_skips_credential_dirs_and_helpers(fx: Fixture) -> None:
    Path(fx.host.config_path).write_text("egress-jail = 0\nno-forge\n")
    fx.run()
    assert not (fx.home / ".config").exists()
    assert "credential" not in Path(fx.host.gitconfig_path).read_text()


def test_codex_and_pi_create_their_own_state(fx: Fixture) -> None:
    fx.run(argv0="/usr/local/bin/codex")
    fx.run(argv0="pi")
    assert (fx.home / ".codex/skills").is_dir()
    assert (fx.home / ".pi/agent/skills").is_dir()
    assert not (fx.home / ".claude.json").exists()


def test_inherited_gitconfig_path_never_reaches_the_builder(fx: Fixture) -> None:
    fx.env["CLAUDE_SANDBOX_GITCONFIG_PATH"] = "/evil/gitconfig"
    argv = shlex.split(fx.run().argv[6])
    assert "/evil/gitconfig" not in argv


@dataclass
class Launched(Exception):
    """jail.launch was called (it never returns), with these arguments."""

    config: Config
    env: dict[str, str]
    command: list[str]


def jailed(
    fx: Fixture,
    monkeypatch: pytest.MonkeyPatch,
    staged: jail.StagedDns,
    args: Sequence[str] = (),
) -> Launched:
    def launch(config: Config, env: Mapping[str, str], command: list[str]) -> NoReturn:
        # As the real one does, the launch removes the staged file on exit.
        if "CLAUDE_SANDBOX_JAIL_RESOLV" in env:
            os.remove(env["CLAUDE_SANDBOX_JAIL_RESOLV"])
        raise Launched(config, dict(env), list(command))

    def stage_dns(*args: object) -> jail.StagedDns:
        return staged

    monkeypatch.setattr(jail, "stage_dns", stage_dns)
    monkeypatch.setattr(jail, "launch", launch)
    Path(fx.host.config_path).write_text("")  # the jail is on by default
    with pytest.raises(Launched) as exc:
        shadow.run("claude", args, fx.env, fx.host)
    return exc.value


def test_the_path_watcher_around_a_launch(
    fx: Fixture, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    venv, system = fx.root / "work/venv/bin", fx.root / "sys"
    executable(system / "git")
    venv.mkdir(parents=True)
    fx.env["PATH"] = f"{venv}:{system}"
    fx.run()  # the first launch keeps a baseline
    executable(venv / "git")  # left by something since
    fx.run()
    assert "claude-sandbox: quarantined at launch: cleared the execute bits of" in (
        capsys.readouterr().err
    )
    assert not os.access(venv / "git", os.X_OK)
    assert len(fx.forked) == 2  # each jail-off launch had a watcher
    # Jailed, the watcher runs around the jail; never for the battery.
    watched: list[watch.Session] = []

    @contextmanager
    def watching(
        session: watch.Session, report: Callable[[str], None]
    ) -> Generator[None]:
        watched.append(session)
        yield

    fx.host = replace(fx.host, watching=watching)
    staged = jail.StagedDns(None)
    jailed(fx, monkeypatch, staged)
    assert [s.path for s in watched] == [fx.env["PATH"]]
    call = jailed(fx, monkeypatch, staged, ["--sandbox-verify"])
    assert call.command[6].endswith("verify-sandbox-battery.sh")
    assert len(watched) == 1 and len(fx.forked) == 2


def test_a_jailed_launch_stages_dns_then_goes_through_the_jail(
    fx: Fixture, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    resolv = fx.root / "claude-jail-resolv.x"
    resolv.write_text("nameserver 192.0.2.53\n")
    staged = jail.StagedDns(str(resolv), ("egress jail — a warning",))
    call = jailed(fx, monkeypatch, staged)
    assert call.config.egress_jail == ""
    assert call.env["CLAUDE_SANDBOX_JAIL_RESOLV"] == str(resolv)
    assert call.env["SHELL"] == "/bin/bash"
    # The staged resolver is bound by bwrap.py, and the warning was shown.
    argv = shlex.split(call.command[6])
    assert argv[argv.index(str(resolv)) - 1 :][:3] == [
        "--ro-bind", str(resolv), "/etc/resolv.conf"
    ]  # fmt: skip
    assert capsys.readouterr().err.endswith(
        "\nclaude-sandbox: egress jail — a warning\n"
    )


def test_ctrl_c_at_the_pause_removes_the_staged_resolver(
    fx: Fixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    resolv = fx.root / "claude-jail-resolv.x"
    resolv.write_text("nameserver 192.0.2.53\n")

    def ctrl_c(self: shadow.Terminal, verify: bool) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(shadow.Terminal, "pause", ctrl_c)
    with pytest.raises(KeyboardInterrupt):
        jailed(fx, monkeypatch, jail.StagedDns(str(resolv)))
    assert not resolv.exists()


def test_an_unstaged_resolver_is_never_bound(
    fx: Fixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    fx.env["CLAUDE_SANDBOX_JAIL_RESOLV"] = "/etc/hosts"
    call = jailed(fx, monkeypatch, jail.StagedDns(None))
    assert "CLAUDE_SANDBOX_JAIL_RESOLV" not in call.env
    assert "/etc/resolv.conf" not in shlex.split(call.command[6])


def test_recursion_guard_execs_the_agent_without_bwrap(fx: Fixture) -> None:
    fx.env["IS_SANDBOX"] = "1"
    ex = fx.run("--chrome", "-p", "hi")
    inner = str(fx.home / ".local/bin/claude")
    assert (ex.path, ex.argv) == (inner, [inner, "--no-chrome", "-p", "hi"])
    assert ex.env == fx.env
    codex = fx.host.profiles["codex"]
    ex = fx.run("--chrome", argv0="codex")
    assert ex.argv == [codex.exec_via, codex.real, "--chrome"]
    ex = fx.run("--sandbox-verify", argv0="pi")
    assert ex.argv == ["/bin/bash", VERIFY_BATTERY]
    # Nothing was written on the way.
    assert list(fx.home.iterdir()) == []


def test_sandbox_verify_runs_the_battery_without_the_persistence_check(
    fx: Fixture, capsys: pytest.CaptureFixture[str]
) -> None:
    argv = shlex.split(fx.run("--sandbox-verify").argv[6])
    assert argv[-2:] == ["/bin/bash", VERIFY_BATTERY]
    assert capsys.readouterr().err == ""
    assert fx.refused("--sandbox-verify", "x") == 2


def test_refusals_before_launch(
    fx: Fixture, capsys: pytest.CaptureFixture[str]
) -> None:
    fx.env["CLAUDE_SANDBOX_AGENT"] = "bash"
    with pytest.raises(SandboxError, match="unknown CLAUDE_SANDBOX_AGENT"):
        shadow.run("claude", [], fx.env, fx.host)
    del fx.env["CLAUDE_SANDBOX_AGENT"]

    Path(fx.host.profiles["pi"].real).chmod(0o644)
    assert fx.refused(argv0="pi") == 1
    assert "real Pi binary missing at" in capsys.readouterr().err

    Path(fx.host.profiles["pi"].real).write_text(shadow.SHIM)
    Path(fx.host.profiles["pi"].real).chmod(0o755)
    assert fx.refused(argv0="pi") == 1
    assert "is a copy of this shadow" in capsys.readouterr().err

    Path(fx.host.config_path).write_text("egress-jail = 0\nlocal-model-port = 99999\n")
    assert fx.refused() == 1
    assert "local-model-port must be 1–65535" in capsys.readouterr().err

    Path(fx.host.config_path).write_text("egress-jail = 0\nallow-device = /etc\n")
    with pytest.raises(SandboxError, match="allow-device"):
        shadow.run("claude", [], fx.env, fx.host)

    Path(fx.host.config_path).write_text("egress-jail = 0\n")
    for tool in ("script", "bwrap"):
        (fx.root / "tools" / tool).rename(fx.root / tool)
        assert fx.refused() == 127
        assert f"{tool} not found in /usr/bin:/bin" in capsys.readouterr().err
        (fx.root / tool).rename(fx.root / "tools" / tool)


@pytest.mark.parametrize("kind", ["executable", "link", "directory"])
def test_an_entry_point_ahead_of_the_shadow_refuses(
    fx: Fixture, capsys: pytest.CaptureFixture[str], kind: str
) -> None:
    venv = fx.root / "venv/bin"
    venv.mkdir(parents=True)
    fx.env["PATH"] = f"{venv}:/usr/local/bin"
    entry = venv / "codex"
    if kind == "executable":
        executable(entry)
    elif kind == "link":
        entry.symlink_to(executable(fx.root / "elsewhere"))
    else:
        entry.mkdir()
    assert fx.refused(argv0="claude") == 1
    err = capsys.readouterr().err
    assert f"{entry} is ahead of /usr/local/bin/codex on PATH" in err
    assert "Remove it, and review the session that created it" in err
    assert entry.exists()  # left for a person to look at
    assert not Path(fx.host.gitconfig_path).exists()  # before any launch step


def test_entry_points_the_guard_left_or_behind_the_shadow_launch(
    fx: Fixture,
) -> None:
    venv, after = fx.root / "venv/bin", fx.root / "after"
    venv.mkdir(parents=True)
    fx.env["PATH"] = f"{venv}:/usr/local/bin:{after}"
    # What the guard's bind leaves: an empty file without execute bits.
    for name in ("claude", "codex", "pi", "claude-sandbox"):
        (venv / name).touch(0o644)
    executable(after / "claude")  # a lookup reaches the shadow first
    fx.run()
    # Inside a sandbox the recursion guard execs the agent: the PATH in there
    # is the sandbox's own.
    executable(venv / "claude")
    fx.env["IS_SANDBOX"] = "1"
    fx.run()


def test_tools_never_come_from_path(fx: Fixture) -> None:
    """ADR 26: no executable is found through PATH."""
    planted, system = fx.root / "on-path", fx.root / "system"
    for tool in ("script", "bwrap", "git"):
        executable(planted / tool)
        executable(system / tool)
    fx.env["PATH"] = str(planted)
    # The real lookup, searching a stand-in for TOOL_PATH: CI runners lack bwrap.
    fx.host = replace(fx.host, find_tool=partial(find_tool, search=(str(system),)))
    ex = fx.run()
    assert str(planted) not in ex.path
    assert str(planted) not in ex.argv[6]
    assert ex.path == str(system / "script")


def test_unreadable_conf_refuses(
    fx: Fixture, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def unreadable(path: str, env: Mapping[str, str]) -> dict[str, str]:
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(shadow, "parse_config", unreadable)
    assert fx.refused() == 1
    assert "claude-sandbox.conf: Permission denied" in capsys.readouterr().err


def test_unknown_name_warns(fx: Fixture, capsys: pytest.CaptureFixture[str]) -> None:
    fx.run(argv0="claude-dev")
    assert "invoked as 'claude-dev', which names no known agent" in (
        capsys.readouterr().err
    )
    fx.env["CLAUDE_SANDBOX_AGENT"] = "claude"
    fx.run(argv0="claude-dev")
    assert "invoked as" not in capsys.readouterr().err


def test_exec_failure_statuses(fx: Fixture, capsys: pytest.CaptureFixture[str]) -> None:
    def enoent(path: str, argv: list[str], env: Mapping[str, str]) -> NoReturn:
        raise FileNotFoundError(2, "No such file or directory")

    def eacces(path: str, argv: list[str], env: Mapping[str, str]) -> NoReturn:
        raise PermissionError(13, "Permission denied")

    fx.host = replace(fx.host, execve=enoent)
    assert fx.refused() == 127
    fx.host = replace(fx.host, execve=eacces)
    assert fx.refused() == 126
    assert "Permission denied" in capsys.readouterr().err


def test_exec_restores_the_signals_python_ignores(fx: Fixture) -> None:
    signal.signal(signal.SIGPIPE, signal.SIG_IGN)
    try:
        fx.run()
        assert signal.getsignal(signal.SIGPIPE) == signal.SIG_DFL
        assert signal.getsignal(signal.SIGXFSZ) == signal.SIG_DFL
    finally:
        signal.signal(signal.SIGPIPE, signal.SIG_IGN)


def test_gitconfig_write_failures_leave_no_temp_file(
    fx: Fixture, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def fail(src: str, dst: str) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(os, "replace", fail)
    assert fx.refused() == 1
    assert "No space left on device" in capsys.readouterr().err
    assert sorted(p.name for p in (fx.root / "etc").iterdir()) == [
        "claude-sandbox.conf"
    ]
    fx.host = replace(fx.host, gitconfig_path=str(fx.root / "no/such/dir/gitconfig"))
    assert fx.refused() == 1
    assert "cannot write" in capsys.readouterr().err


def test_home_creation_failure_refuses(
    fx: Fixture, capsys: pytest.CaptureFixture[str]
) -> None:
    (fx.home / ".config").write_text("a file, not a dir")
    assert fx.refused() == 1
    assert "cannot create" in capsys.readouterr().err


def test_shipped_skills_warn_only_when_they_mask_something(
    fx: Fixture, capsys: pytest.CaptureFixture[str]
) -> None:
    (fx.root / "skills/beta/scripts").mkdir(parents=True)
    (fx.root / "skills/.hidden").mkdir()
    (fx.root / "skills/not-a-skill").touch()
    skills = fx.home / ".claude/skills"
    for name in ("alpha", "beta", "gamma"):
        (skills / name).mkdir(parents=True)
    (skills / "beta/SKILL.md").touch()
    (skills / "gamma/SKILL.md").touch()
    fx.run()
    assert capsys.readouterr().err.count("shadowed") == 1  # beta only
    (skills / "beta/SKILL.md").unlink()
    (skills / "beta").rmdir()
    (skills / "beta").symlink_to(skills / "gamma")
    (skills / "alpha").rmdir()
    (skills / "alpha").touch()
    fx.run()
    err = capsys.readouterr().err
    assert "~/.claude/skills/alpha is shadowed" in err
    assert "~/.claude/skills/beta is shadowed" in err


def test_unshareable_skills_dir_warns_and_launches(
    fx: Fixture, capsys: pytest.CaptureFixture[str]
) -> None:
    (fx.home / ".agents").symlink_to(fx.root / "gone")
    fx.run()
    assert "cannot create ~/.agents/skills" in capsys.readouterr().err


def test_persistent_config_does_not_warn(
    fx: Fixture, capsys: pytest.CaptureFixture[str]
) -> None:
    shared = fx.root / "shared dir"
    shared.mkdir()
    (fx.home / ".claude").symlink_to(shared)
    fx.run()
    assert "not host-mounted" not in capsys.readouterr().err
    (fx.home / ".claude").unlink()
    (fx.home / ".claude").mkdir()
    mount = str(fx.home / ".claude").replace(" ", "\\040")
    Path(fx.host.mountinfo).write_text(f"1 2 0:1 / {mount} rw - tmpfs t rw\n")
    fx.home.rename(fx.root / "home dir")
    fx.env["HOME"] = str(fx.root / "home dir")
    mount = str(fx.root / "home dir/.claude").replace(" ", "\\040")
    Path(fx.host.mountinfo).write_text(f"short\n1 2 0:1 / {mount} rw - tmpfs t rw\n")
    fx.run()
    assert "not host-mounted" not in capsys.readouterr().err


def test_is_mountpoint() -> None:
    assert shadow.is_mountpoint("/proc")
    assert not shadow.is_mountpoint("/proc/self/../self/..", "/nonexistent")
    assert not shadow.is_mountpoint("/no/such/path")


def test_original_environ(tmp_path: Path) -> None:
    block = tmp_path / "environ"
    block.write_bytes(b"A=1\0B=x=y\0A=2\0junk\0=nameless\0C=\xff\0")
    assert shadow.original_environ(str(block)) == {
        "A": "1",
        "B": "x=y",
        "C": os.fsdecode(b"\xff"),
    }
    assert shadow.original_environ(str(tmp_path / "absent")) == dict(os.environ)
    assert shadow.original_environ()["PATH"] == os.environ["PATH"]


def test_working_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "real").mkdir()
    (tmp_path / "link").symlink_to(tmp_path / "real")
    monkeypatch.chdir(tmp_path / "link")
    real = os.getcwd()
    assert shadow.working_directory({"PWD": str(tmp_path / "link")}) == str(
        tmp_path / "link"
    )
    assert shadow.working_directory({"PWD": str(tmp_path)}) == real
    assert shadow.working_directory({"PWD": "relative"}) == real
    assert shadow.working_directory({"PWD": "/no/such/dir"}) == real
    assert shadow.working_directory({}) == real


def test_git_identity_comes_from_the_fixed_tool_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = tmp_path / "gitconfig"
    cfg.write_text("[user]\n\tname = Real Name\n")
    env = {"PATH": os.environ["PATH"], "GIT_CONFIG_GLOBAL": str(cfg)}
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    assert shadow.read_git_config("user.name", env) == "Real Name"
    assert shadow.read_git_config("user.email", env) == ""
    env["PATH"] = str(tmp_path)  # PATH plays no part
    assert shadow.read_git_config("user.name", env) == "Real Name"

    def nowhere(name: str) -> None:
        return None

    monkeypatch.setattr(shadow, "find_tool", nowhere)  # no git at all
    assert shadow.read_git_config("user.name", env) == ""


def test_pause_needs_a_person_at_the_terminal(
    capsys: pytest.CaptureFixture[str],
) -> None:
    term = shadow.Terminal()
    term.pause(verify=False)
    term.warn("hello")
    term.pause(verify=True)
    term.pause(verify=False)  # pytest's stdin is not a terminal
    assert capsys.readouterr().err == "claude-sandbox: hello\n"


def test_main_reports_and_dies_like_the_bash(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    died: list[int] = []

    def die_by(signum: int) -> NoReturn:
        died.append(signum)
        raise SystemExit(128 + signum)

    def raising(exc: BaseException) -> None:
        def run(*args: object) -> NoReturn:
            raise exc

        monkeypatch.setattr(shadow, "run", run)

    monkeypatch.setattr(shadow, "die_by", die_by)
    saved = {s: signal.getsignal(s) for s in (signal.SIGTERM, signal.SIGHUP)}
    try:
        raising(SandboxError("claude-sandbox: no."))
        with pytest.raises(SystemExit) as exc:
            shadow.main("claude", [])
        assert exc.value.code == 1
        assert capsys.readouterr().err == "claude-sandbox: no.\n"
        assert signal.getsignal(signal.SIGTERM) is shadow.raise_signalled
        for raised, signum in (
            (KeyboardInterrupt(), signal.SIGINT),
            (shadow.Signalled(signal.SIGHUP), signal.SIGHUP),
        ):
            raising(raised)
            with pytest.raises(SystemExit):
                shadow.main("claude", [])
            assert died[-1] == signum
    finally:
        for signum, handler in saved.items():
            signal.signal(signum, handler)


def test_signal_helpers(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(shadow.Signalled) as exc:
        shadow.raise_signalled(signal.SIGTERM, None)
    assert exc.value.signum == signal.SIGTERM
    sent: list[int] = []

    def kill(pid: int, signum: int) -> None:
        sent.append(signum)

    monkeypatch.setattr(os, "kill", kill)
    saved = signal.getsignal(signal.SIGUSR1)
    try:
        with pytest.raises(SystemExit) as exit_:
            shadow.die_by(signal.SIGUSR1)
    finally:
        signal.signal(signal.SIGUSR1, saved)
    assert (sent, exit_.value.code) == ([signal.SIGUSR1], 128 + signal.SIGUSR1)


def test_battery_check_18_finds_its_pins() -> None:
    """verify-sandbox-battery.sh check 18 greps these lines (Invariant 4)."""
    battery = (
        REPO / ".devcontainer/claude-sandbox/verify-sandbox-battery.sh"
    ).read_text()
    assert shadow.SHIM.splitlines()[-1] in battery
    pkg = REPO / "src/claude_sandbox"
    assert (
        'CONFIG_PATH = "/etc/claude-sandbox.conf"\n' in (pkg / "config.py").read_text()
    )
    source = (pkg / "shadow.py").read_text()
    assert "config_path: str = CONFIG_PATH" in source
    assert "parse_config(host.config_path" in source
    for line in source.splitlines():
        assert not ("parse_config(" in line and ".devcontainer" in line), line


def test_shim_file_is_the_shim_the_shadow_knows() -> None:
    shim = REPO / ".devcontainer/claude-sandbox/claude-shim"
    assert shim.read_text() == shadow.SHIM
    assert os.access(shim, os.X_OK)


def test_dunder_main_dispatches(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    calls: list[tuple[str, list[str]]] = []

    def shadow_main(name: str, args: list[str]) -> None:
        calls.append((name, args))

    def cli_main() -> int:
        calls.append(("cli", []))
        return 0

    def holder_main(args: list[str]) -> None:
        calls.append(("holder", args))

    monkeypatch.setattr(shadow, "main", shadow_main)
    monkeypatch.setattr(jail, "holder_main", holder_main)
    monkeypatch.setattr(cli, "main", cli_main)
    for argv in (
        ["_shadow", "pi", "--", "--", "x"],
        ["_jail_holder", "--", "script"],
    ):
        monkeypatch.setattr(sys, "argv", ["claude_sandbox", *argv])
        runpy.run_module("claude_sandbox", run_name="__main__")
    monkeypatch.setattr(sys, "argv", ["claude_sandbox", "--help"])
    with pytest.raises(SystemExit) as done:
        runpy.run_module("claude_sandbox", run_name="__main__")
    assert done.value.code == 0
    assert calls == [
        ("pi", ["--", "x"]),
        ("holder", ["--", "script"]),
        ("cli", []),
    ]
    monkeypatch.setattr(sys, "argv", ["claude_sandbox", "_shadow", "pi", "x"])
    with pytest.raises(SystemExit) as exc:
        runpy.run_module("claude_sandbox", run_name="__main__")
    assert exc.value.code == 2
    assert "usage:" in capsys.readouterr().err
