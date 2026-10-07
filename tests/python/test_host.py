"""The host launcher against a fake engine.

tests/launcher.sh, run against the Python CLI by test_bash_suites.py, is
the behavioural check; these reach the branches it does not and pin the
pieces that must match the bash exactly (names, the version order).
"""

import os
import pty
import signal
import subprocess
import sys
from pathlib import Path

import pytest

from claude_sandbox.host import launcher, options
from claude_sandbox.host.launcher import Launcher
from claude_sandbox.host.options import Options, Stop

RUNNING = "{{.State.Running}}"
KEEPER = '{{join .Config.Cmd " "}}'


LABEL = f'{{{{index .Config.Labels "{launcher.VERSION_LABEL}"}}}}'


class Engine:
    """A fake podman: containers by name, each a map of inspect format to answer."""

    def __init__(self) -> None:
        self.fail: dict[str, int] = {}
        self.calls: list[list[str]] = []
        self.containers: dict[str, dict[str, str]] = {}
        self.labels: dict[str, str] = {}
        self.listing = {"ps": "", "images": "", "find": ""}
        self.starts = True
        self.status = 0
        self.others = "0"  # live sessions `sessions.py end` reports

    def add(self, name: str, running: bool = False) -> dict[str, str]:
        ctr = {RUNNING: str(running).lower(), KEEPER: launcher.KEEPER_CMD}
        self.containers[name] = ctr | {"{{len .ExecIDs}}": "0", LABEL: "5.0.0"}
        return self.containers[name]

    def run(
        self, argv: list[str], *, stdout: int | None = None, stderr: int | None = None
    ) -> "subprocess.CompletedProcess[str]":
        args = argv[1:]
        self.calls.append(args)
        out, rc = None, 0
        match args:
            case ["container", "inspect", "-f", fmt, name]:
                out = self.containers.get(name, {}).get(fmt)
            case ["container", "inspect", name]:
                rc = 0 if name in self.containers else 1
            case ["image", "inspect", "-f", fmt, _]:
                out = self.labels.get(fmt)
            case ["create", "--name", name, *_]:
                self.add(name)
            case ["start", name]:
                self.containers[name][RUNNING] = str(self.starts).lower()
            case ["rm", "-f", name]:
                self.containers.pop(name, None)
            case ["rmi", image]:
                rc = 1 if "in-use" in image else 0
            case ["ps" | "images" as what, *_]:
                out = self.listing[what]
            case ["run", "--rm", "--entrypoint", "find", *_]:
                out = self.listing["find"]
            case ["exec", _, launcher.PYTHON, "-I", "-c", _, "end", _]:
                out = self.others
            case _:
                pass
        rc = 1 if out is None and args[1:2] == ["inspect"] and "-f" in args else rc
        rc = self.fail.get(args[0], rc)
        return subprocess.CompletedProcess(argv, rc, f"{out or ''}\n", "")

    def interactive(self, argv: list[str]) -> int:
        self.calls.append(argv[1:])
        return self.status

    def called(self, verb: str) -> list[list[str]]:
        return [c for c in self.calls if c[0] == verb]


@pytest.fixture
def engine(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Engine:
    fake = Engine()
    monkeypatch.setattr(launcher, "run", fake.run)
    monkeypatch.setattr(launcher, "interactive", fake.interactive)
    project = tmp_path / "ws" / "proj"
    project.mkdir(parents=True)
    monkeypatch.chdir(project)
    for var in [v for v in os.environ if v.startswith("CLAUDE_SANDBOX_")]:
        monkeypatch.delenv(var)
    for var in ("DISPLAY", "PWD", "XAUTHORITY", "LANG", "TERM"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("CLAUDE_SANDBOX_LAUNCHER_VERSION", "4.7.2")
    return fake


def run(opts: Options | None = None) -> Launcher:
    return Launcher(opts or Options(), dict(os.environ))


# --- the pieces that must match the bash ------------------------------------


@pytest.mark.parametrize("path", ["/tmp/proj", "/a b/ünï", "/"])
def test_names_match_cksum(path: str) -> None:
    crc = subprocess.run(["cksum"], input=path.encode(), capture_output=True)
    name, tag = launcher.names(path)
    assert name.endswith(f"-{int(crc.stdout.split()[0])}")
    assert len(tag) <= 25


def test_slug_maps_every_byte_and_keeps_a_trailing_dash() -> None:
    assert launcher.slug("/x/a b-ü-") == "a-b----"
    assert launcher.basename("/x/y/") == "y"
    assert launcher.basename("/") == "/"


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("4.0.0", "4.0.0-beta.1"),
        ("4.7.2", "4.10.0"),
        ("1.0~rc1", "1.0"),
        ("1.0", "1.0a"),
    ],
)
def test_vercmp_sorts_as_sort_v(a: str, b: str) -> None:
    out = subprocess.run(
        ["sort", "-V"], input=f"{b}\n{a}\n", capture_output=True, text=True
    ).stdout.split()
    assert out == [a, b]
    assert launcher.vercmp(a, b) < 0 < launcher.vercmp(b, a)
    assert launcher.vercmp(a, a) == 0


def test_project_dir_keeps_the_logical_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / "real").mkdir()
    (tmp_path / "link").symlink_to(tmp_path / "real")
    monkeypatch.chdir(tmp_path / "link")
    logical = str(tmp_path / "link")
    assert launcher.project_dir({"PWD": logical}) == logical
    assert launcher.project_dir({"PWD": "/nonexistent"}) == str(tmp_path / "real")


def test_detect_shell_walks_up_to_a_shell(tmp_path: Path) -> None:
    for pid, comm, ppid in [(50, "uv", 40), (40, "-zsh", 1), (60, "x) y", 9)]:
        (tmp_path / str(pid)).mkdir()
        (tmp_path / str(pid) / "stat").write_text(f"{pid} ({comm}) S {ppid} 1 1")
    (tmp_path / "70").mkdir()
    (tmp_path / "70" / "stat").write_text("70 (odd)")
    env = {"SHELL": "/usr/bin/fish"}
    assert launcher.detect_shell(env, 50, str(tmp_path)) == "zsh"
    assert launcher.detect_shell(env, 60, str(tmp_path)) == "fish"  # 9 is unreadable
    assert launcher.detect_shell(env, 70, str(tmp_path)) == "fish"  # malformed
    assert launcher.detect_shell({}, 1, str(tmp_path)) == "bash"
    assert launcher.detect_shell(env) in launcher.SHELLS | {"fish"}


# --- options ----------------------------------------------------------------


def test_options_then_the_verb_and_its_arguments_untouched(tmp_path: Path) -> None:
    opts = options.parse(
        ["--recreate", "--bridge", "--peers", "--no-peers", "--gpu", "--device",
         "/dev/null", "--mount", str(tmp_path), "--mount-rw", str(tmp_path),
         "--", "pi", "--", "-p"],
        ["pi"], "1",
    )  # fmt: skip
    assert (opts.recreate, opts.host_net, opts.peers, opts.gpu) == (
        True,
        False,
        False,
        True,
    )
    assert opts.devices == ["/dev/null"] and opts.mounts_ro == [str(tmp_path)]
    assert opts.create_opts[-1] == f"--mount-rw {tmp_path}"
    assert (opts.verb, opts.tail) == ("pi", ["--", "-p"])
    assert options.parse(["--resume", "x"], ["pi"], "1").tail == ["--resume", "x"]


@pytest.mark.parametrize(
    ("args", "code", "text"),
    [
        (["--version"], 0, "claude-sandbox 9.9"),
        (["-h"], -1, ""),
        (["--device"], 1, "absolute /dev device path"),
        (["--device", "/dev/pts"], 1, "character or block device"),
        (["--device", "/dev/nonexistent"], 1, "character or block device"),
        (["--mount"], 1, "--mount needs a PATH"),
        (["--mount-rw", "/nonexistent/x"], 1, "No such file or directory"),
        (["--mount", ""], 1, "No such file or directory"),
    ],
)
def test_options_stop(
    capsys: pytest.CaptureFixture[str], args: list[str], code: int, text: str
) -> None:
    with pytest.raises(Stop) as stop:
        options.parse(args, [], "9.9")
    assert stop.value.code == code
    options.report(stop.value)
    out = capsys.readouterr()
    assert text in (out.out if code == 0 else out.err)


# --- create and reuse -------------------------------------------------------


def test_create_argv(
    engine: Engine, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    for name in (".gitconfig", ".Xauthority", "sandbox.conf"):
        (home / name).touch()
    monkeypatch.setenv("CLAUDE_SANDBOX_CONF", str(home / "sandbox.conf"))
    monkeypatch.setenv("DISPLAY", ":1")
    monkeypatch.setenv("CLAUDE_SANDBOX_ALLOW_DEVICES", "/dev/full")
    monkeypatch.setenv("CLAUDE_SANDBOX_ALLOW_WRITE", "/srv")
    monkeypatch.setenv("CLAUDE_SANDBOX_EGRESS_JAIL", "0")
    monkeypatch.setenv("CLAUDE_SANDBOX_IMPL", "python")
    monkeypatch.setenv("CLAUDE_SANDBOX_NESTED", "1")
    monkeypatch.setenv("CLAUDE_SANDBOX_CACHE", "")
    opts = Options(gpu=True, devices=["/dev/null"], peers=True)
    opts.mounts_ro, opts.mounts_rw = ["/r"], ["/w"]
    args = run(opts).create_args()
    joined = " ".join(args)
    parent = str(tmp_path / "ws")
    for part in [
        "--device nvidia.com/gpu=all",
        "--device /dev/null --group-add keep-groups",
        "-e CLAUDE_SANDBOX_ALLOW_DEVICES=/dev/full\n/dev/null",
        "-e CLAUDE_SANDBOX_GPU=1",
        f"--mount type=bind,src={parent},dst={parent},bind-propagation=slave",
        "-e DISPLAY=:1",
        f"{home}/.Xauthority:/root/.Xauthority:ro",
        f"{home}/.gitconfig:/root/.gitconfig-host:ro",
        f"{home}/sandbox.conf:/etc/claude-sandbox.conf:ro",
        "--mount type=bind,src=/r,dst=/r,ro,bind-propagation=slave",
        "-e CLAUDE_SANDBOX_ALLOW_WRITE=/srv\n/w",
        "-e CLAUDE_SANDBOX_EGRESS_JAIL=0",
    ]:
        assert part in joined
    assert ":/cache" not in joined and "CLAUDE_SANDBOX_IMPL" not in joined
    assert "CLAUDE_SANDBOX_NESTED" not in joined
    assert args[-4:] == [launcher.IMAGE, "bash", "-c", launcher.KEEPER_CMD]


@pytest.mark.parametrize(
    ("engine_name", "gpu", "expect"),
    [("docker", "1", "--gpus all"), ("/usr/bin/docker", "", "-e CLAUDE_SANDBOX_GPU=2")],
)
def test_create_argv_for_docker(
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    engine_name: str,
    gpu: str,
    expect: str,
) -> None:
    monkeypatch.setenv("CLAUDE_SANDBOX_ENGINE", engine_name)
    monkeypatch.setenv("CLAUDE_SANDBOX_GPU", "2")
    joined = " ".join(run(Options(gpu=bool(gpu), devices=["/dev/null"])).create_args())
    assert expect in joined and "keep-groups" not in joined


def test_gpu_needs_podman_or_docker(
    engine: Engine, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("CLAUDE_SANDBOX_ENGINE", "nerdctl")
    with pytest.raises(SystemExit):
        run(Options(gpu=True)).create_args()
    assert "--gpu requires podman or docker" in capsys.readouterr().err


def test_peers_skip_a_parent_holding_home(
    engine: Engine, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("HOME", str(Path.cwd().parent / "me"))
    assert run(Options(peers=True)).session(["claude"], pause=False) == 0
    assert "did not mount" in capsys.readouterr().err
    assert "src=" not in " ".join(engine.called("create")[0])


def test_session_creates_starts_execs_and_stops(
    engine: Engine, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    paused: list[bool] = []
    monkeypatch.setattr(launcher, "wait_for_key", lambda: paused.append(True))

    def tty(fd: int) -> bool:
        return True

    monkeypatch.setattr(os, "isatty", tty)
    engine.labels["{{.Id}}"] = "new"
    engine.status = 3
    r = run(Options(recreate=True))
    r.warned = True
    assert r.session(["claude", "-p"], pause=True) == 3
    session, end = engine.called("exec")
    # Wrapped by sessions.py (issue #69), which records it, then ended by it.
    code = launcher.session_code()
    assert session[:7] == ["exec", "-it", r.name, launcher.PYTHON, "-I", "-c", code]
    assert session[7] == "start" and session[9:] == ["claude", "-p"]
    assert end == ["exec", r.name, launcher.PYTHON, "-I", "-c", code, "end", session[8]]
    assert engine.called("stop") and paused == [True]
    assert capsys.readouterr().out == launcher.MOUSE_RESET


def test_the_keeper_runs_on_while_another_session_lives(engine: Engine) -> None:
    engine.others = "1"
    assert run().session(["claude"], pause=False) == 0
    assert not engine.called("stop")


def test_a_hangup_still_ends_the_session_and_stops_the_keeper(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The terminal closed: the cleanup runs, detached, and the handlers
    the launcher found are put back."""
    detached: list[int] = []
    monkeypatch.setattr(launcher, "detach", detached.append)

    def hung_up(argv: list[str]) -> int:
        engine.calls.append(argv[1:])
        launcher.hangup(signal.SIGHUP, None)
        raise AssertionError("not reached")

    monkeypatch.setattr(launcher, "interactive", hung_up)
    before = signal.getsignal(signal.SIGTERM)
    assert run().session(["claude"], pause=False) == 129
    assert detached == [129]
    assert engine.called("exec")[1][6] == "end" and engine.called("stop")
    assert signal.getsignal(signal.SIGTERM) == before


@pytest.mark.parametrize("label", ["4.7.1", "4.8.0-beta.1", "<no value>", None])
def test_a_4x_container_is_reused_with_a_loud_warning(
    engine: Engine, capsys: pytest.CaptureFixture[str], label: str | None
) -> None:
    """Made from an image older than 5.0 (or with no label): reused, never
    refused, but the user is told it is the bash sandbox and how to leave."""
    r = run(Options())
    ctr = engine.add(r.name, running=True)
    if label is None:
        del ctr[LABEL]
    else:
        ctr[LABEL] = label
    assert r.session(["claude"], pause=False) == 0
    err = capsys.readouterr().err
    assert "this container runs the 4.x bash sandbox" in err
    assert "cloud metadata services and VPN split routes" in err
    assert "no PATH guard against executables a session leaves" in err
    assert "rebuild     claude-sandbox --recreate" in err
    # No interpreter to track it with: unwrapped, and the old idle test.
    assert engine.called("exec") == [["exec", "-it", r.name, "claude"]]
    assert r.warned and engine.called("stop")


def test_a_5x_container_is_reused_quietly(
    engine: Engine, capsys: pytest.CaptureFixture[str]
) -> None:
    r = run(Options())
    engine.add(r.name, running=True)
    assert r.session(["claude"], pause=False) == 0
    assert "bash sandbox" not in capsys.readouterr().err and not r.warned


def test_reuse_warns_and_recreate_removes(
    engine: Engine, capsys: pytest.CaptureFixture[str]
) -> None:
    r = run(Options(create_opts=["--gpu"]))
    mounts = "{{range .Mounts}}{{println .Destination}}{{end}}"
    engine.add(r.name, running=True).update({
        "{{.Image}}": "old",
        "{{.Created}}": "2026-09-13T10:20:30Z",
        mounts: f"{Path(r.project).parent}\n",
    })  # fmt: skip
    engine.labels["{{.Id}}"] = "new"
    assert r.session(["pi"], pause=True) == 0
    err = capsys.readouterr().err
    assert "older image than the one pulled (created 2026-09-13 10:20)" in err
    assert "mounts the parent directory" in err
    assert "ignored on an existing container: --gpu" in err
    assert "rebuild     claude-sandbox --recreate" in err
    assert not engine.called("create") and not engine.called("start")
    assert run(Options(recreate=True)).session(["pi"], pause=False) == 0
    assert engine.called("rm") and engine.called("create")


def test_a_container_without_the_keeper_is_refused(
    engine: Engine, capsys: pytest.CaptureFixture[str]
) -> None:
    r = run()
    engine.add(r.name)[KEEPER] = "claude"
    with pytest.raises(SystemExit):
        r.session(["claude"], pause=False)
    assert "does not have the expected keeper command" in capsys.readouterr().err


def test_a_container_that_exits_on_start_shows_its_log(
    engine: Engine, capsys: pytest.CaptureFixture[str]
) -> None:
    engine.starts = False
    with pytest.raises(SystemExit):
        run().session(["claude"], pause=False)
    assert "exited during start" in capsys.readouterr().err
    assert engine.called("logs")


def test_a_failed_engine_command_ends_the_run(engine: Engine) -> None:
    engine.fail["create"] = 125
    with pytest.raises(SystemExit) as exc:
        run().session(["claude"], pause=False)
    assert exc.value.code == 125


# --- version warnings, the engine check, clean ------------------------------


@pytest.mark.parametrize(
    ("image", "uvx", "text"),
    [
        ("9.0.0", False, "pin         pipx install --force claude-sandbox==9.0.0"),
        ("9.0.0", True, "pin         uvx claude-sandbox==9.0.0"),
        ("1.0.0", True, "rebuild     uvx claude-sandbox --recreate"),
        ("4.7.2", False, ""),
    ],
)
def test_warn_if_outdated(
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    image: str,
    uvx: bool,
    text: str,
) -> None:
    if uvx:
        monkeypatch.setenv("CLAUDE_SANDBOX_LAUNCHER", "uvx")
    engine.labels[f'{{{{index .Config.Labels "{launcher.VERSION_LABEL}"}}}}'] = image
    r = run()
    r.warn_if_outdated()
    assert text in capsys.readouterr().err and r.warned == bool(text)


def test_engine_must_be_on_path(
    engine: Engine, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("CLAUDE_SANDBOX_ENGINE", "no-such-engine")
    with pytest.raises(SystemExit):
        launcher.launcher(Options())
    assert "no-such-engine not found" in capsys.readouterr().err


def test_clean(engine: Engine, capsys: pytest.CaptureFixture[str]) -> None:
    live = "/ws/live"
    engine.add(launcher.names(live)[0])
    engine.add("claude-sandbox-a-1")
    engine.add("claude-sandbox-run-2", running=True)
    engine.add("claude-sandbox-other-3")[KEEPER] = "sleep infinity"
    engine.listing["ps"] = (
        "claude-sandbox-a-1\nclaude-sandbox-run-2\nclaude-sandbox-other-3"
    )
    engine.listing["images"] = "ghcr.io/x:1\nin-use:2"
    engine.listing["find"] = f"/cache/venv-for{live}\n/cache/venv-for/ws/gone"
    run().clean(force=False, images=True)
    err = capsys.readouterr().err
    assert "removed claude-sandbox-a-1" in err and "kept claude-sandbox-run-2" in err
    assert "1 container(s) removed, 1 running kept" in err
    assert "removed image ghcr.io/x:1" in err and "in-use" not in err
    assert "removed venv for /ws/gone" in err and "1 venv(s) removed" in err
    run().clean(force=True, images=False)
    assert "claude-sandbox-run-2" not in engine.containers


def test_clean_without_a_cache_volume_runs_no_helper_container(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLAUDE_SANDBOX_CACHE", "")
    run().clean(force=False, images=False)
    assert not engine.called("run")


# --- the terminal -----------------------------------------------------------


def test_pause_reads_one_key_from_the_terminal(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    master, slave = pty.openpty()
    with os.fdopen(slave) as tty:
        monkeypatch.setattr(sys, "stdin", tty)
        os.write(master, b"x")
        launcher.wait_for_key()
        assert "Press any key" in capsys.readouterr().err

        def interrupt(fd: int, n: int) -> bytes:
            raise KeyboardInterrupt

        monkeypatch.setattr(os, "read", interrupt)
        with pytest.raises(SystemExit) as exc:
            launcher.wait_for_key()
        assert exc.value.code == 130
    os.close(master)


def test_interactive_waits_out_an_interrupt(monkeypatch: pytest.MonkeyPatch) -> None:
    assert launcher.interactive(["sh", "-c", "exit 3"]) == 3
    # Killed by a signal: 128 + its number, as a shell reports it.
    assert launcher.interactive(["sh", "-c", "kill -TERM $$"]) == 143
    assert launcher.run(["true"]).returncode == 0

    class Child:
        interrupted = False

        def __init__(self, argv: list[str]) -> None:
            pass

        def wait(self) -> int:
            if not Child.interrupted:
                Child.interrupted = True
                raise KeyboardInterrupt
            return 0

    monkeypatch.setattr(subprocess, "Popen", Child)
    assert launcher.interactive(["claude"]) == 0


def test_a_hangup_kills_the_engine_client(monkeypatch: pytest.MonkeyPatch) -> None:
    killed: list[bool] = []

    class Child:
        def __init__(self, argv: list[str]) -> None:
            self.waits = 0

        def wait(self) -> int:
            self.waits += 1
            if self.waits == 1:
                raise launcher.Hangup(signal.SIGTERM)
            return -9

        def kill(self) -> None:
            killed.append(True)

    monkeypatch.setattr(subprocess, "Popen", Child)
    with pytest.raises(launcher.Hangup):
        launcher.interactive(["claude"])
    assert killed == [True]


def test_hangup_raises_once(monkeypatch: pytest.MonkeyPatch) -> None:
    saved = {s: signal.getsignal(s) for s in launcher.HANGUPS}
    try:
        with pytest.raises(launcher.Hangup) as exc:
            launcher.hangup(signal.SIGHUP, None)
        assert exc.value.signum == signal.SIGHUP
        assert all(signal.getsignal(s) == signal.SIG_IGN for s in launcher.HANGUPS)
    finally:
        for s, handler in saved.items():
            signal.signal(s, handler)


def test_detach_leaves_the_parent_and_starts_a_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[object] = []
    monkeypatch.setattr(os, "setsid", lambda: calls.append("setsid"))

    def exit_(rc: int) -> None:
        calls.append(rc)
        raise SystemExit(rc)

    monkeypatch.setattr(os, "_exit", exit_)
    for child in (42, 0):
        monkeypatch.setattr(os, "fork", lambda child=child: child)
        try:
            launcher.detach(129)
        except SystemExit:
            pass
    assert calls == [129, "setsid"]
