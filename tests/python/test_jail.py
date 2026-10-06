"""Unit tests for jail.py, with every side effect replaced by FakeOps.

The real namespaces, pasta and relays are exercised in a container by
tests/jail_python.sh; these pin the argv, the order, the fail-closed paths,
the cleanup and the signal statuses.
"""

import dataclasses
import os
import signal
import subprocess
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import NoReturn

import pytest

from claude_sandbox import jail
from claude_sandbox.config import Config
from claude_sandbox.errors import SandboxError
from claude_sandbox.jail import Handler, Ops, stage_dns
from test_parity import driver

PY = "/usr/libexec/claude-sandbox/venv/bin/python"
JAIL_DIR = "/tmp/claude-jail.T"
READY = f"{JAIL_DIR}/ready"
RELAY = f"{JAIL_DIR}/relay"
COMMAND = ["script", "--return", "-q", "-E", "never", "-c", "agent", "/dev/null"]
DEFAULT_ROUTE = "default via 10.0.2.2 dev eth0 proto static metric 100\n"
LINK_ROUTES = "10.0.2.0/24 proto kernel scope link src 10.0.2.15\n\n"


Setup = Callable[["FakeOps"], object]


def given(setup: Setup) -> Setup:
    """Types a parametrized setup lambda."""
    return setup


class FakeProc:
    """Alive for ``polls`` calls to poll(), then exits with ``rc``."""

    def __init__(self, pid: int, rc: int = 0, polls: int = 1000) -> None:
        self.pid = pid
        self.rc = rc
        self.polls = polls
        self.done = False

    def poll(self) -> int | None:
        if not self.done and self.polls > 0:
            self.polls -= 1
            return None
        self.done = True
        return self.rc

    def wait(self) -> int:
        self.done = True
        return self.rc


class Exec(Exception):
    pass


class FakeOps(Ops):
    executable = PY

    def __init__(self) -> None:
        self.log: list[tuple[object, ...]] = []
        self.missing: set[str] = set()
        self.files: set[str] = {"/dev/net/tun", READY}
        self.fail: set[str] = set()  # joined argv that exit 1
        self.listening: set[str] = set()  # ports ss reports
        self.procs: dict[int, FakeProc] = {}
        self.next_rc = 0  # the next spawned process's status
        self.next_polls = 1000
        self.spawn_error: set[str] = set()  # argv[0] that cannot start
        self.handlers: dict[int, Handler | int] = {}
        self.on_sleep: Callable[[int], None] = lambda n: None
        self.sleeps = 0
        self.env: dict[str, str] = {}
        self.netns_ready = True

    def which(self, name: str, env: Mapping[str, str]) -> str | None:
        return None if name in self.missing else f"/usr/bin/{name}"

    def exists(self, path: str) -> bool:
        return path in self.files

    def is_file(self, path: str) -> bool:
        return path in self.files

    def is_socket(self, path: str) -> bool:
        return path.endswith(".sock") and path not in self.missing

    def read(self, path: str) -> str:
        if path == "/proc/self/ns/net":
            return "net:[1]"
        if path.endswith("/ns/net"):
            return "net:[2]" if self.netns_ready else "net:[1]"
        return "0 1000 1\n"

    def run(
        self,
        argv: Sequence[str],
        env: Mapping[str, str],
        *,
        quiet: bool = False,
        stderr_to: str | None = None,
    ) -> tuple[int, str]:
        self.log.append(("run", *argv))
        line = " ".join(argv)
        if line in self.fail:
            return 1, ""
        if argv[:2] == ["ss", "-H"]:
            port = argv[-1].rpartition(":")[2]
            return 0, f"LISTEN 0 5 127.0.0.1:{port}\n" * (port in self.listening)
        if line == "ip route show default":
            return 0, DEFAULT_ROUTE
        if line.startswith("ip -o route show dev"):
            return 0, LINK_ROUTES
        return 0, ""

    def spawn(
        self, argv: Sequence[str], env: Mapping[str, str], *, relay: bool = False
    ) -> FakeProc:
        if argv[0] in self.spawn_error:
            raise PermissionError(13, "Permission denied")
        pid = 100 + len(self.procs)
        self.log.append(("spawn", pid, relay, *argv))
        self.env = dict(env)
        proc = self.procs[pid] = FakeProc(pid, self.next_rc, self.next_polls)
        return proc

    def execvpe(self, argv: Sequence[str], env: Mapping[str, str]) -> NoReturn:
        self.log.append(("exec", *argv))
        if argv[0] in self.spawn_error:
            raise FileNotFoundError(2, "No such file or directory")
        raise Exec

    def kill(self, pid: int, sig: int, *, group: bool = False) -> None:
        self.log.append(("kill", pid, sig, group))
        proc = self.procs[pid]
        proc.done, proc.rc = True, -sig

    def sleep(self, seconds: float) -> None:
        self.sleeps += 1
        self.on_sleep(self.sleeps)

    def mkdtemp(self) -> str:
        self.log.append(("mkdtemp",))
        return JAIL_DIR

    def mkdir(self, path: str) -> None:
        self.log.append(("mkdir", path))

    def touch(self, path: str) -> None:
        self.log.append(("touch", path))

    def rmtree(self, path: str) -> None:
        self.log.append(("rmtree", path))

    def remove(self, path: str) -> None:
        self.log.append(("remove", path))

    def signal(self, sig: int, handler: Handler | int) -> None:
        self.handlers[sig] = handler

    def fire(self, sig: int) -> None:
        handler = self.handlers[sig]
        assert callable(handler)
        handler(sig, None)

    def environ(self) -> dict[str, str]:
        return dict(self.env)

    def stderr(self, message: str) -> None:
        self.log.append(("stderr", message))

    def kinds(self, *kinds: str) -> list[tuple[object, ...]]:
        return [entry for entry in self.log if entry[0] in kinds]

    def errors(self) -> list[object]:
        return [entry[1] for entry in self.kinds("stderr")]


def launch(ops: FakeOps, env: Mapping[str, str] | None = None, **conf: str) -> int:
    config = dataclasses.replace(Config(), **conf)
    with pytest.raises(SystemExit) as e:
        jail.launch(config, env or {}, COMMAND, ops=ops)
    assert isinstance(e.value.code, int)
    return e.value.code


PASTA = [
    "/usr/bin/pasta",
    "--config-net",
    "--ipv4-only",
    "--no-map-gw",
    "-t", "none",
    "-u", "none",
    "-T", "none",
    "-U", "none",
    "--dns-forward", "192.0.2.53",
    "--quiet",
    "--log-file", "/tmp/claude-pasta.log",
    "100",
]  # fmt: skip


def test_launch_runs_the_holder_then_attaches_pasta() -> None:
    ops = FakeOps()
    ops.next_rc = 7
    env = {"PATH": "/usr/bin", "CLAUDE_JAIL_RELAY_DIR": "/forged"}
    resolv = "/tmp/claude-jail-resolv.abc"
    assert launch(ops, {**env, "CLAUDE_SANDBOX_JAIL_RESOLV": resolv}) == 7
    holder = ["/usr/bin/unshare", "-rn", PY, "-I", "-m", "claude_sandbox"]
    assert ops.log == [
        ("mkdtemp",),
        ("spawn", 100, False, *holder, "_jail_holder", "--", *COMMAND),
        ("run", *PASTA),
        ("touch", READY),
        ("rmtree", JAIL_DIR),
        ("remove", resolv),
    ]
    # The holder learns where the handshake file is, and nothing a caller
    # set for the relays survives.
    assert ops.env == {
        "PATH": "/usr/bin",
        "CLAUDE_SANDBOX_JAIL_RESOLV": resolv,
        "CLAUDE_JAIL_READY": READY,
    }
    assert set(ops.handlers) == {signal.SIGINT, signal.SIGTERM, signal.SIGHUP}


def test_launch_starts_both_relay_directions() -> None:
    ops = FakeOps()
    ops.listening = {"53692"}  # taken: fails soft
    ops.on_sleep = lambda n: ops.listening.add("1455")  # the relay comes up
    rc = launch(
        ops,
        local_model_port="1920",
        local_port_entries="8000,1920",
        callback_port_entries="53692\n1455",
        allow_ip="203.0.113.7",
    )
    assert rc == 0
    spawns = ops.kinds("spawn")
    assert spawns[:2] == [
        (
            "spawn", 100, True, "/usr/bin/socat",
            f"UNIX-LISTEN:{RELAY}/1920.sock,mode=0600,fork", "TCP4:127.0.0.1:1920",
        ),
        (
            "spawn", 101, True, "/usr/bin/socat",
            f"UNIX-LISTEN:{RELAY}/8000.sock,mode=0600,fork", "TCP4:127.0.0.1:8000",
        ),
    ]  # fmt: skip
    assert spawns[2] == (
        "spawn", 102, True, "/usr/bin/socat",
        "TCP4-LISTEN:1455,bind=127.0.0.1,reuseaddr,fork",
        f"UNIX-CONNECT:{RELAY}/in-1455.sock",
    )  # fmt: skip
    assert ops.errors() == [
        "claude-sandbox: callback-port 53692 is already in use on this host;"
        " browser logins on that port will not reach this session."
    ]
    assert ops.env == {
        "CLAUDE_JAIL_READY": READY,
        "CLAUDE_JAIL_RELAY_DIR": RELAY,
        "CLAUDE_JAIL_LOCAL_PORTS": "1920 8000 ",
        "CLAUDE_JAIL_CALLBACK_PORTS": "1455 ",
        "CLAUDE_SANDBOX_ALLOW_IP": "203.0.113.7",
    }
    # Each relay's whole process group is stopped, newest first.
    kills = [(pid, sig, group) for _, pid, sig, group in ops.kinds("kill")]
    assert kills == [(p, signal.SIGTERM, True) for p in (102, 101, 100)]
    assert ops.log[-1] == ("rmtree", JAIL_DIR)


def test_callback_relay_that_never_listens_fails_soft() -> None:
    ops = FakeOps()
    assert launch(ops, callback_port_entries="1455") == 0
    assert ops.errors() == [
        "claude-sandbox: callback-port 1455 relay failed to start;"
        " browser logins on that port will not reach this session."
    ]
    assert ops.env["CLAUDE_JAIL_CALLBACK_PORTS"] == ""


@pytest.mark.parametrize(
    ("setup", "conf", "message"),
    [
        (given(lambda o: o.missing.add("unshare")), {}, "needs unshare (util-linux)"),
        (
            given(lambda o: o.missing.add("pasta")),
            {},
            "needs pasta (apt-get install passt)",
        ),
        (
            given(lambda o: o.files.discard("/dev/net/tun")),
            {},
            "needs /dev/net/tun — add --device=/dev/net/tun to the container",
        ),
        (
            given(lambda o: setattr(o, "executable", "python3")),
            {},
            "— cannot locate the sandbox's own Python interpreter",
        ),
        (
            given(lambda o: o.missing.add("socat")),
            {"local_model_port": "1920"},
            "needs socat for loopback relays",
        ),
        (
            given(lambda o: o.missing.add(f"{RELAY}/1920.sock")),
            {"local_model_port": "1920"},
            "— loopback relay for port 1920 failed to start",
        ),
        (
            given(lambda o: o.spawn_error.add("/usr/bin/unshare")),
            {},
            "— cannot start the holder: [Errno 13] Permission denied",
        ),
        (
            given(lambda o: setattr(o, "next_polls", 0)),
            {},
            "— holder netns never appeared",
        ),
        (
            given(lambda o: setattr(o, "netns_ready", False)),
            {},
            "— holder netns never appeared",
        ),
        (
            given(lambda o: o.fail.add(" ".join(PASTA))),
            {},
            "— pasta failed to attach to the netns (see /tmp/claude-pasta.log)",
        ),
    ],
)
def test_launch_fails_closed(setup: Setup, conf: dict[str, str], message: str) -> None:
    ops = FakeOps()
    setup(ops)
    assert launch(ops, None, **conf) == 1
    assert ops.errors() == [f"claude-sandbox: egress jail {message}"]
    # The holder never got the go-ahead, and whatever started was stopped.
    assert ("touch", READY) not in ops.log
    assert all(proc.done for proc in ops.procs.values())


def test_invalid_ports_are_refused_before_anything_starts() -> None:
    ops = FakeOps()
    rc = launch(
        ops, local_model_port="70000", callback_port_entries="x 1920"
    )  # 1920 is not outbound here: the model port is invalid, not 1920
    assert rc == 1
    assert ops.errors() == [
        "claude-sandbox: local-model-port must be 1–65535 (or 0 to disable).",
        "claude-sandbox: callback-port entries must be 1–65535, got 'x'.",
    ]
    assert ops.kinds("mkdtemp", "spawn", "run") == []


@pytest.mark.parametrize(
    ("sig", "expected"),
    [(signal.SIGINT, 130), (signal.SIGTERM, 143), (signal.SIGHUP, 143)],
)
def test_signal_while_the_agent_runs(sig: int, expected: int) -> None:
    ops = FakeOps()
    ops.on_sleep = lambda n: ops.fire(sig) if n == 3 else None
    ops.next_polls = 1000
    assert launch(ops, local_model_port="1920") == expected
    # A second signal during cleanup changes nothing.
    ops.fire(signal.SIGTERM)
    assert ops.kinds("kill") == [
        ("kill", 100, signal.SIGTERM, True),
        ("kill", 101, signal.SIGTERM, False),
    ]
    assert ops.log[-1] == ("rmtree", JAIL_DIR)


def test_signal_before_the_holder_is_ready() -> None:
    ops = FakeOps()
    ops.netns_ready = False
    ops.on_sleep = lambda n: ops.fire(signal.SIGINT)
    assert launch(ops) == 130
    assert ("touch", READY) not in ops.log
    assert ops.kinds("kill") == [("kill", 100, signal.SIGTERM, False)]


def test_holder_killed_by_a_signal_reports_128_plus_n() -> None:
    ops = FakeOps()
    ops.next_rc, ops.next_polls = -signal.SIGKILL, 2
    assert launch(ops) == 137


def test_only_a_staged_resolver_is_removed() -> None:
    ops = FakeOps()
    launch(ops, {"CLAUDE_SANDBOX_JAIL_RESOLV": "/etc/hosts"})
    assert ops.kinds("remove") == []


# --- the holder ----------------------------------------------------------------

ROUTES = [
    "ip link set lo up",
    "ip route show default",
    "ip -o route show dev eth0 scope link",
    "ip route replace blackhole 10.0.2.0/24",
    "ip route replace blackhole 10.0.0.0/8",
    "ip route replace blackhole 172.16.0.0/12",
    "ip route replace blackhole 192.168.0.0/16",
    "ip route replace blackhole 100.64.0.0/10",
    "ip route replace unreachable 169.254.0.0/16",
    "ip route replace 10.0.2.2/32 dev eth0",
    "ip route replace default via 10.0.2.2 dev eth0",
    "ip route replace 192.0.2.53/32 via 10.0.2.2",
]


def hold(ops: FakeOps, *argv: str) -> int:
    with pytest.raises(SystemExit) as e:
        jail.holder_main(argv, ops=ops)
    assert isinstance(e.value.code, int)
    return e.value.code


def runs(ops: FakeOps) -> list[str]:
    return [" ".join(str(a) for a in entry[1:]) for entry in ops.kinds("run")]


def test_holder_locks_routes_then_execs() -> None:
    ops = FakeOps()
    ops.env = {"CLAUDE_JAIL_READY": READY, "CLAUDE_SANDBOX_ALLOW_IP": "203.0.113.7/24"}
    with pytest.raises(Exec):
        jail.holder_main(["--", *COMMAND], ops=ops)
    assert runs(ops) == [*ROUTES, "ip route replace 203.0.113.7/32 via 10.0.2.2"]
    assert ops.log[-1] == ("exec", *COMMAND)
    # Started by Python, it resets what Python changed before exec.
    for sig in (signal.SIGINT, signal.SIGPIPE, signal.SIGXFSZ):
        assert ops.handlers[sig] == signal.SIG_DFL


@pytest.mark.parametrize("index", range(3, 11))
def test_holder_fails_closed_on_each_load_bearing_route(index: int) -> None:
    ops = FakeOps()
    ops.env = {"CLAUDE_JAIL_READY": READY}
    ops.fail.add(ROUTES[index])
    assert hold(ops, "--", *COMMAND) == 1
    assert runs(ops) == ROUTES[: index + 1]
    (message,) = ops.errors()
    assert isinstance(message, str)
    assert message.startswith("claude-sandbox: egress jail — failed to ")
    assert message.endswith(" (fail-closed)")
    assert ops.kinds("exec", "spawn") == []


def test_holder_route_messages() -> None:
    """The bash wording, for the ones whose wording differs."""
    got: list[object] = []
    for index in (8, 9, 10, 7):
        ops = FakeOps()
        ops.env = {"CLAUDE_JAIL_READY": READY}
        ops.fail.add(ROUTES[index])
        hold(ops, "--", "true")
        got += ops.errors()
    prefix = "claude-sandbox: egress jail — failed to "
    assert got == [
        f"{prefix}mark 169.254.0.0/16 unreachable (fail-closed)",
        f"{prefix}pin gateway 10.0.2.2 on-link (fail-closed)",
        f"{prefix}restore default via 10.0.2.2 (fail-closed)",
        f"{prefix}blackhole 100.64.0.0/10 CGNAT (fail-closed)",
    ]


def test_holder_soft_failures_still_launch() -> None:
    ops = FakeOps()
    ops.env = {"CLAUDE_JAIL_READY": READY, "CLAUDE_SANDBOX_ALLOW_IP": "\n203.0.113.7"}
    ops.fail |= {ROUTES[11], "ip route replace 203.0.113.7/32 via 10.0.2.2"}
    with pytest.raises(Exec):
        jail.holder_main(["--", *COMMAND], ops=ops)
    assert ops.errors() == [
        "claude-sandbox: egress jail — could not route DNS forwarder 192.0.2.53",
        "claude-sandbox: egress jail — could not route allow-ip 203.0.113.7",
    ]


@pytest.mark.parametrize(
    ("setup", "message"),
    [
        (
            given(lambda o: o.fail.add("ip link set lo up")),
            "— could not bring up loopback",
        ),
        (given(lambda o: o.files.discard(READY)), "— pasta never signalled ready"),
        (
            given(lambda o: o.fail.add("ip route show default")),
            "— no default route after pasta attach",
        ),
    ],
)
def test_holder_refuses_before_routing(setup: Setup, message: str) -> None:
    ops = FakeOps()
    ops.env = {"CLAUDE_JAIL_READY": READY}
    setup(ops)
    assert hold(ops, "--", "true") == 1
    assert ops.errors() == [f"claude-sandbox: egress jail {message}"]


def test_holder_needs_a_gateway_and_a_device(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("test_jail.DEFAULT_ROUTE", "default dev eth0\n")
    ops = FakeOps()
    ops.env = {"CLAUDE_JAIL_READY": READY}
    assert hold(ops, "--", "true") == 1
    assert ops.errors() == [
        "claude-sandbox: egress jail — no default route via/dev after pasta attach"
    ]


def test_holder_usage_and_exec_failure() -> None:
    ops = FakeOps()
    assert hold(ops, "true") == 2
    ops = FakeOps()
    ops.env = {"CLAUDE_JAIL_READY": READY}
    ops.spawn_error.add("nonesuch")
    assert hold(ops, "--", "nonesuch") == 127
    assert ops.errors() == ["claude-sandbox: nonesuch: No such file or directory"]


def holder_with_relays(ops: FakeOps) -> None:
    ops.env = {
        "CLAUDE_JAIL_READY": READY,
        "CLAUDE_JAIL_RELAY_DIR": RELAY,
        "CLAUDE_JAIL_LOCAL_PORTS": "1920 8000 ",
        "CLAUDE_JAIL_CALLBACK_PORTS": "1455 ",
    }
    ops.listening = {"1920", "8000"}


def test_holder_runs_inner_relays_and_the_agent() -> None:
    ops = FakeOps()
    holder_with_relays(ops)
    ops.next_rc = -signal.SIGINT  # each spawn exits this way when polled out
    ops.next_polls = 2
    assert hold(ops, "--", *COMMAND) == 130
    assert ops.kinds("spawn") == [
        (
            "spawn", 100, True, "socat",
            "TCP4-LISTEN:1920,bind=127.0.0.1,reuseaddr,fork",
            f"UNIX-CONNECT:{RELAY}/1920.sock",
        ),
        (
            "spawn", 101, True, "socat",
            "TCP4-LISTEN:8000,bind=127.0.0.1,reuseaddr,fork",
            f"UNIX-CONNECT:{RELAY}/8000.sock",
        ),
        (
            "spawn", 102, True, "socat",
            f"UNIX-LISTEN:{RELAY}/in-1455.sock,mode=0600,fork", "TCP4:127.0.0.1:1455",
        ),
        ("spawn", 103, False, *COMMAND),
    ]  # fmt: skip
    assert [entry[1] for entry in ops.kinds("kill")] == [102, 101, 100]


def test_holder_interrupted_stops_the_agent() -> None:
    ops = FakeOps()
    holder_with_relays(ops)
    ops.on_sleep = lambda n: ops.fire(signal.SIGHUP)
    assert hold(ops, "--", "true") == 143
    assert ops.kinds("kill")[-1] == ("kill", 103, signal.SIGTERM, False)


@pytest.mark.parametrize(
    ("setup", "message"),
    [
        (
            given(lambda o: o.listening.discard("8000")),
            "— loopback listener for port 8000 failed to start",
        ),
        (
            given(lambda o: o.spawn_error.add("socat")),
            "— loopback listener for port 1920 failed to start",
        ),
        (
            given(lambda o: o.missing.add(f"{RELAY}/in-1455.sock")),
            "— callback relay for port 1455 failed to start",
        ),
    ],
)
def test_holder_relay_failures_are_fatal(setup: Setup, message: str) -> None:
    ops = FakeOps()
    holder_with_relays(ops)
    setup(ops)
    assert hold(ops, "--", "true") == 1
    assert ops.errors() == [f"claude-sandbox: egress jail {message}"]
    assert not any(entry[3:] == ("true",) for entry in ops.kinds("spawn"))


def test_holder_agent_that_cannot_start() -> None:
    ops = FakeOps()
    holder_with_relays(ops)
    ops.spawn_error.add("/not/executable")
    assert hold(ops, "--", "/not/executable") == 126


# --- DNS staging (the bash's scenario 13, through the comparison driver) --------


@pytest.mark.parametrize(
    "resolv",
    [
        "nameserver 8.8.8.8\n",  # routable: still overridden (#11)
        "nameserver 127.0.0.53\n",  # loopback stub
        "nameserver 127.0.0.53\nnameserver 192.168.1.1\n",  # mixed
        "",  # empty: forwarder, with a warning
        "search a.example b.example\nnameserver 8.8.8.8\noptions timeout:1\n",
        # beyond the bash suite: indentation, a keyword with no space after
        # it, a last line with no newline, and CRLF.
        " domain x\ndomain\n\tsearch y\r\n#options z\noptions ndots:2",
    ],
)
def test_stage_dns_matches_the_bash(tmp_path: Path, resolv: str) -> None:
    conf = tmp_path / "resolv.conf"
    conf.write_text(resolv)
    (tmp_path / "py").mkdir()
    (tmp_path / "sh").mkdir()
    staged = stage_dns(resolv_conf=str(conf), tmpdir=str(tmp_path / "py"))
    assert staged.path is not None
    assert os.path.dirname(staged.path) == str(tmp_path / "py")
    got = Path(staged.path).read_bytes()

    sh = driver(
        tmp_path,
        {"TMPDIR": str(tmp_path / "sh"), "PATH": "/usr/bin:/bin"},
        "stage_dns",
        str(conf),
    )
    assert sh.returncode == 0, sh.stderr
    (bash_file,) = (tmp_path / "sh").iterdir()
    assert bash_file.name.startswith("claude-jail-resolv.")
    assert got == bash_file.read_bytes()
    warned = [f"claude-sandbox: {w}\n" for w in staged.warnings]
    assert os.fsdecode(sh.stderr).replace(str(conf), "/etc/resolv.conf") == "".join(
        warned
    )
    assert got.startswith(b"nameserver 192.0.2.53\n")


def test_stage_dns_without_a_resolv_conf(tmp_path: Path) -> None:
    staged = stage_dns(resolv_conf=str(tmp_path / "none"), tmpdir=str(tmp_path))
    assert staged.path is not None
    assert Path(staged.path).read_text() == "nameserver 192.0.2.53\n"
    assert len(staged.warnings) == 1


def test_stage_dns_ignores_tmpdir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unlike the bash, which honours $TMPDIR: it may be in the workspace."""
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    staged = stage_dns()
    assert staged.path is not None
    os.remove(staged.path)
    assert os.path.dirname(staged.path) == "/tmp"


def test_stage_dns_when_mktemp_fails(tmp_path: Path) -> None:
    staged = stage_dns(tmpdir=str(tmp_path / "none"))
    assert staged == jail.StagedDns(
        None,
        (
            "egress jail — could not stage a DNS override (mktemp failed);"
            " name resolution may fail.",
        ),
    )


def test_stage_dns_when_the_write_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(fd: int, mode: str) -> NoReturn:
        os.close(fd)
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(jail.os, "fdopen", fail)
    with pytest.raises(SandboxError, match="No space left on device"):
        stage_dns(tmpdir=str(tmp_path))
    assert list(tmp_path.iterdir()) == []


# --- small pieces and the real Ops ------------------------------------------------


def test_route_field_and_status() -> None:
    route = "default via 10.0.2.2 dev eth0 proto dhcp"
    assert jail.route_field("via", route) == "10.0.2.2"
    assert jail.route_field("dev", route) == "eth0"
    assert jail.route_field("dhcp", route) == ""
    assert jail.status(3) == 3
    assert jail.status(-signal.SIGTERM) == 143


def test_real_ops(tmp_path: Path) -> None:
    ops = Ops()
    env = {"PATH": "/usr/bin:/bin"}
    assert ops.which("sh", env) is not None
    assert ops.run(["sh", "-c", "echo hi; echo err >&2"], env, quiet=True) == (
        0,
        "hi\n",
    )
    assert ops.run(["/nonexistent"], env) == (127, "")
    log = tmp_path / "log"
    assert ops.run(["sh", "-c", "echo e >&2; exit 3"], env, stderr_to=str(log)) == (
        3,
        "",
    )
    assert log.read_text() == "e\n"
    assert ops.read("/proc/self/ns/net").startswith("net:[")
    assert ops.read(str(log)) == "e\n"
    assert ops.read(str(tmp_path / "gone")) == ""
    assert ops.exists(str(log)) and ops.is_file(str(log))
    assert not ops.is_socket(str(log)) and not ops.is_socket(str(tmp_path / "x"))

    relay_dir = str(tmp_path / "relay")
    ops.mkdir(relay_dir)
    assert os.stat(relay_dir).st_mode & 0o777 == 0o700
    ops.touch(f"{relay_dir}/f")
    ops.remove(f"{relay_dir}/f")
    ops.remove(f"{relay_dir}/f")
    ops.rmtree(relay_dir)
    assert not os.path.exists(relay_dir)

    relay = ops.spawn(["sh", "-c", "sleep 30 & wait"], env, relay=True)
    assert os.getpgid(relay.pid) == relay.pid  # its own group
    jail.stop_relays([relay], ops)
    assert relay.wait() == -signal.SIGTERM
    ops.kill(relay.pid, signal.SIGTERM)  # already gone: ignored
    child = ops.spawn(["true"], env)
    assert os.getpgid(child.pid) in (os.getpgid(0), child.pid) and child.wait() == 0
    ops.sleep(0)
    assert ops.environ()["PATH"] == os.environ["PATH"]


def test_real_ops_dirs_signals_and_exec() -> None:
    ops = Ops()
    path = ops.mkdtemp()
    assert path.startswith("/tmp/claude-jail.")
    ops.rmtree(path)
    previous = signal.getsignal(signal.SIGHUP)
    ops.signal(signal.SIGHUP, signal.SIG_IGN)
    signal.signal(signal.SIGHUP, previous)
    # The real exec and stderr, in a child.
    code = (
        "from claude_sandbox.jail import OS\n"
        "OS.stderr('to stderr')\n"
        "OS.execvpe(['sh', '-c', 'exit 5'], {'PATH': '/usr/bin:/bin'})\n"
    )
    done = subprocess.run(
        [ops.executable, "-c", code], capture_output=True, text=True, check=False
    )
    assert (done.returncode, done.stderr) == (5, "to stderr\n")
