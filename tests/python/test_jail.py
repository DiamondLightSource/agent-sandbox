"""Unit tests for jail.py: its processes and /proc replaced by FakeOps, its
files in a temporary directory, its signal handlers real.

The real namespaces, pasta and relays are exercised in a container by
tests/jail_python.sh; these pin the argv, the order, the fail-closed paths,
the cleanup and the signal statuses.
"""

import dataclasses
import os
import re
import signal
import stat
import subprocess
import sys
from collections.abc import Callable, Iterator, Mapping, Sequence
from pathlib import Path
from typing import NoReturn

import pytest

from claude_sandbox import jail
from claude_sandbox.config import Config
from claude_sandbox.errors import SandboxError
from claude_sandbox.jail import Ops, stage_dns

PY = sys.executable
COMMAND = [
    "/usr/bin/script",
    "--return",
    "-q",
    "-E",
    "never",
    "-c",
    "agent",
    "/dev/null",
]
DEFAULT_ROUTE = "default via 10.0.2.2 dev eth0 proto static metric 100\n"
LINK_ROUTES = "10.0.2.0/24 proto kernel scope link src 10.0.2.15\n\n"
ADDRS = (
    "1: lo    inet 127.0.0.1/8 scope host lo\\       valid_lft forever\n"
    "2: eth0    inet 10.0.2.15/24 brd 10.0.2.255 scope global eth0\\ ...\n"
)
LOCAL_TABLE = (
    "local 10.0.2.15 dev eth0 table local proto kernel scope host src 10.0.2.15\n"
    "broadcast 10.0.2.255 dev eth0 table local proto kernel scope link\n"
    "local 127.0.0.0/8 dev lo table local proto kernel scope host src 127.0.0.1\n"
    "local 127.0.0.1 dev lo table local proto kernel scope host src 127.0.0.1\n"
    "broadcast 127.255.255.255 dev lo table local proto kernel scope link\n"
)
RULES = (
    "0:\tfrom all lookup local\n"
    "32766:\tfrom all lookup main\n"
    "32767:\tfrom all lookup default\n"
)
SIGNALS = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP, signal.SIGPIPE, signal.SIGXFSZ)


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


def fire(sig: int) -> None:
    """Deliver ``sig`` to the handler the jail installed."""
    handler = signal.getsignal(sig)
    assert callable(handler)
    handler(sig, None)


class FakeOps(Ops):
    def __init__(self, root: Path, capsys: pytest.CaptureFixture[str]) -> None:
        self.root = root
        self.capsys = capsys
        self.log: list[tuple[object, ...]] = []
        self.missing: set[str] = set()  # tools not installed
        self.no_socket: set[str] = set()  # relay sockets that never appear
        self.fail: set[str] = set()  # joined argv that exit 1
        self.listening: set[str] = set()  # ports ss reports
        self.procs: dict[int, FakeProc] = {}
        self.next_rc = 0  # the next spawned process's status
        self.next_polls = 1000
        self.spawn_error: set[str] = set()  # argv[0] that cannot start
        self.on_sleep: Callable[[int], None] = lambda n: None
        self.sleeps = 0
        self.env: dict[str, str] = {}  # the last spawn's environment
        self.netns_ready = True
        self.readied = False  # the holder's go-ahead was seen
        self.contents: dict[str, str] = {}  # what read() returns for a path
        self.captured: list[str] = []
        # The netns's routes as `ip -o route show table all` prints them:
        # the main table by destination (what pasta mirrored, then what the
        # holder set), the other tables, and the policy rules.
        self.main: dict[str, str] = {}
        for line in (DEFAULT_ROUTE + LINK_ROUTES).splitlines():
            if line.split():
                self.main[jail.route_prefix(line.split()[0])] = line
        self.other_tables = LOCAL_TABLE
        self.rules = RULES
        self.v6_addrs = ""
        self.v6_routes = ""
        self.egress_src = "10.0.2.15"
        self.main_ignores = ""  # an `ip route replace` that succeeds but never shows

    @property
    def ready(self) -> Path:
        """The holder's handshake file (see ``holder``)."""
        return self.root / "ready"

    @property
    def relay(self) -> str:
        """The relay directory the launch handed the holder."""
        return self.env["CLAUDE_JAIL_RELAY_DIR"]

    def observe(self) -> None:
        jails = self.root.glob("tmp/claude-jail.*/ready")
        self.readied = self.readied or any(True for _ in jails)

    def read(self, path: str) -> str:
        if path in self.contents:
            return self.contents[path]
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
        # Tools are absolute; match on their names.
        line = " ".join([os.path.basename(argv[0]), *argv[1:]])
        if line in self.fail:
            return 1, ""
        if argv[:2] == ["/usr/bin/ss", "-H"]:
            port = argv[-1].rpartition(":")[2]
            return 0, f"LISTEN 0 5 127.0.0.1:{port}\n" * (port in self.listening)
        if line == "ip route show default":
            return 0, DEFAULT_ROUTE
        if line == "ip -4 route flush table main":
            self.main.clear()
        elif line.startswith("ip route replace ") and " ".join(argv[3:]) != (
            self.main_ignores
        ):
            self.route(argv[3:])
        if line == "ip -4 -o route show table all":
            return 0, "".join(f"{r}\n" for r in self.main.values()) + self.other_tables
        if line == "ip -4 rule show":
            return 0, self.rules
        if line == "ip -4 -o addr show dev eth0 scope global":
            return 0, ADDRS.split("\n", 1)[1]
        if line == "ip -4 -o addr show":
            return 0, ADDRS
        if line == "ip route get 1.1.1.1":
            return 0, f"1.1.1.1 via 10.0.2.2 dev eth0 src {self.egress_src} uid 0\n"
        if line == "ip -6 -o addr show":
            return 0, self.v6_addrs
        if line == "ip -6 -o route show table all":
            return 0, self.v6_routes
        if line.startswith("ip -o route show dev"):
            return 0, LINK_ROUTES
        return 0, ""

    def route(self, args: Sequence[str]) -> None:
        """``ip route replace ARGS`` as the kernel would then print it."""
        if args[0] in ("blackhole", "unreachable"):
            self.main[args[1]] = f"{args[0]} {args[1]}"
            return
        dst = jail.route_prefix(args[0])
        shown = args[0].removesuffix("/32")
        if "via" in args:
            via = args[list(args).index("via") + 1]
            self.main[dst] = f"{shown} via {via} dev eth0"
        else:
            self.main[dst] = f"{shown} dev eth0 scope link"

    def spawn(
        self, argv: Sequence[str], env: Mapping[str, str], *, relay: bool = False
    ) -> FakeProc:
        if argv[0] in self.spawn_error:
            raise PermissionError(13, "Permission denied")
        pid = 100 + len(self.procs)
        self.log.append(("spawn", pid, relay, *argv))
        self.env = dict(env)
        # A relay that listens on a Unix socket makes it.
        for arg in argv:
            sock = arg.removeprefix("UNIX-LISTEN:").partition(",")[0]
            listens = arg.startswith("UNIX-LISTEN:")
            if listens and os.path.basename(sock) not in self.no_socket:
                os.mknod(sock, stat.S_IFSOCK | 0o600)
        proc = self.procs[pid] = FakeProc(pid, self.next_rc, self.next_polls)
        return proc

    def execve(self, argv: Sequence[str], env: Mapping[str, str]) -> NoReturn:
        self.log.append(("exec", *argv))
        if argv[0] in self.spawn_error:
            raise FileNotFoundError(2, "No such file or directory")
        raise Exec

    def kill(self, pid: int, sig: int, *, group: bool = False) -> None:
        self.observe()
        self.log.append(("kill", pid, sig, group))
        proc = self.procs[pid]
        proc.done, proc.rc = True, -sig

    def sleep(self, seconds: float) -> None:
        self.observe()
        self.sleeps += 1
        self.on_sleep(self.sleeps)

    def kinds(self, *kinds: str) -> list[tuple[object, ...]]:
        return [entry for entry in self.log if entry[0] in kinds]

    def errors(self) -> list[str]:
        """Each message printed to stderr so far."""
        err = self.capsys.readouterr().err
        self.captured += re.split(r"\n(?=claude-sandbox: )", err.rstrip("\n"))
        self.captured = [line for line in self.captured if line]
        return self.captured

    def jails(self) -> list[Path]:
        """The jail directories left behind."""
        return list(self.root.glob("tmp/claude-jail.*"))


@pytest.fixture(autouse=True)
def restore_signals() -> Iterator[None]:
    saved = {sig: signal.getsignal(sig) for sig in SIGNALS}
    yield
    for sig, handler in saved.items():
        signal.signal(sig, handler)


NewOps = Callable[[], FakeOps]


@pytest.fixture
def new_ops(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> NewOps:
    """A FakeOps, with the jail's files under ``tmp_path``: its directory,
    /dev/net/tun, the IPv6 sysctls, and the holder's handshake file, which
    the holder's environment names."""
    (tmp_path / "tmp").mkdir()
    (tmp_path / "tun").touch()
    (tmp_path / "ipv6").mkdir()
    (tmp_path / "ready").touch()
    monkeypatch.setattr(jail, "JAIL_TMP", str(tmp_path / "tmp"))
    monkeypatch.setattr(jail, "TUN", str(tmp_path / "tun"))
    monkeypatch.setattr(jail, "IPV6_SYSCTLS", str(tmp_path / "ipv6"))
    off = tuple(str(tmp_path / "ipv6" / name) for name in ("all", "default"))
    monkeypatch.setattr(jail, "IPV6_OFF", off)
    for name in ("CLAUDE_JAIL_LOCAL_PORTS", "CLAUDE_JAIL_CALLBACK_PORTS"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("CLAUDE_SANDBOX_ALLOW_IP", raising=False)
    monkeypatch.setenv("CLAUDE_JAIL_READY", str(tmp_path / "ready"))
    current: list[FakeOps] = []

    def find_tool(name: str) -> str | None:
        return None if name in current[-1].missing else f"/usr/bin/{name}"

    monkeypatch.setattr(jail, "find_tool", find_tool)

    def make() -> FakeOps:
        current.append(FakeOps(tmp_path, capsys))
        return current[-1]

    return make


@pytest.fixture
def ops(new_ops: NewOps) -> FakeOps:
    return new_ops()


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


def test_launch_runs_the_holder_then_attaches_pasta(
    ops: FakeOps, tmp_path: Path
) -> None:
    ops.next_rc = 7
    env = {"PATH": "/usr/bin", "CLAUDE_JAIL_RELAY_DIR": "/forged"}
    resolv = tmp_path / "claude-jail-resolv.abc"
    resolv.touch()
    assert launch(ops, {**env, "CLAUDE_SANDBOX_JAIL_RESOLV": str(resolv)}) == 7
    holder = ["/usr/bin/unshare", "-rn", PY, "-I", "-m", "claude_sandbox"]
    assert ops.log[:2] == [
        ("spawn", 100, False, *holder, "_jail_holder", "--", *COMMAND),
        ("run", *PASTA),
    ]
    assert ops.readied
    # Its directory under /tmp, and the staged resolver, are gone.
    assert ops.jails() == [] and not resolv.exists()
    # The holder learns where the handshake file is, and nothing a caller
    # set for the relays survives.
    (jail_dir,) = {os.path.dirname(ops.env["CLAUDE_JAIL_READY"])}
    assert os.path.basename(jail_dir).startswith("claude-jail.")
    assert ops.env == {
        "PATH": "/usr/bin",
        "CLAUDE_SANDBOX_JAIL_RESOLV": str(resolv),
        "CLAUDE_JAIL_READY": f"{jail_dir}/ready",
    }
    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        assert getattr(signal.getsignal(sig), "__name__", "") == "_record"


def test_launch_starts_both_relay_directions(ops: FakeOps) -> None:
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
    relay = ops.relay
    spawns = ops.kinds("spawn")
    assert spawns[:2] == [
        (
            "spawn", 100, True, "/usr/bin/socat",
            f"UNIX-LISTEN:{relay}/1920.sock,mode=0600,fork", "TCP4:127.0.0.1:1920",
        ),
        (
            "spawn", 101, True, "/usr/bin/socat",
            f"UNIX-LISTEN:{relay}/8000.sock,mode=0600,fork", "TCP4:127.0.0.1:8000",
        ),
    ]  # fmt: skip
    assert spawns[2] == (
        "spawn", 102, True, "/usr/bin/socat",
        "TCP4-LISTEN:1455,bind=127.0.0.1,reuseaddr,fork",
        f"UNIX-CONNECT:{relay}/in-1455.sock",
    )  # fmt: skip
    assert ops.errors() == [
        "claude-sandbox: callback-port 53692 is already in use on this host;"
        " browser logins on that port will not reach this session."
    ]
    jail_dir = os.path.dirname(relay)
    assert ops.env == {
        "CLAUDE_JAIL_READY": f"{jail_dir}/ready",
        "CLAUDE_JAIL_RELAY_DIR": relay,
        "CLAUDE_JAIL_LOCAL_PORTS": "1920 8000 ",
        "CLAUDE_JAIL_CALLBACK_PORTS": "1455 ",
        "CLAUDE_SANDBOX_ALLOW_IP": "203.0.113.7",
    }
    # Each relay's whole process group is stopped, newest first.
    kills = [(pid, sig, group) for _, pid, sig, group in ops.kinds("kill")]
    assert kills == [(p, signal.SIGTERM, True) for p in (102, 101, 100)]
    assert ops.jails() == []


def test_the_relay_directory_is_private(ops: FakeOps) -> None:
    modes: list[int] = []
    ops.on_sleep = lambda n: modes.append(os.stat(ops.relay).st_mode)
    ops.next_polls = 2
    launch(ops, local_model_port="1920")
    assert modes and stat.S_IMODE(modes[0]) == 0o700


def test_callback_relay_that_never_listens_fails_soft(ops: FakeOps) -> None:
    assert launch(ops, callback_port_entries="1455") == 0
    assert ops.errors() == [
        "claude-sandbox: callback-port 1455 relay failed to start;"
        " browser logins on that port will not reach this session."
    ]
    assert ops.env["CLAUDE_JAIL_CALLBACK_PORTS"] == ""


def test_an_old_pasta_is_named_as_the_cause(ops: FakeOps) -> None:
    ops.fail.add(" ".join(["pasta", *PASTA[1:]]))
    ops.contents[jail.PASTA_LOG] = (
        "Couldn't open user namespace /proc/42/ns/user: Permission denied\n"
    )
    assert launch(ops, None) == 1
    (error,) = ops.errors()
    assert error.startswith("claude-sandbox: egress jail — pasta failed to attach")
    assert "too old to attach" in error and "Ubuntu 24.04" in error
    assert not ops.readied


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
            given(lambda o: os.remove(jail.TUN)),
            {},
            "needs /dev/net/tun — add --device=/dev/net/tun to the container",
        ),
        (
            given(lambda o: o.missing.add("socat")),
            {"local_model_port": "1920"},
            "needs socat for loopback relays",
        ),
        (
            given(lambda o: o.missing.add("ss")),
            {"callback_port_entries": "1455"},
            "needs ss (iproute2) for loopback relays",
        ),
        (
            given(lambda o: o.no_socket.add("1920.sock")),
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
            "— holder exited with status 0 before pasta could attach",
        ),
        (
            given(lambda o: setattr(o, "netns_ready", False)),
            {},
            "— holder netns never appeared",
        ),
        (
            given(lambda o: o.fail.add(" ".join(["pasta", *PASTA[1:]]))),
            {},
            "— pasta failed to attach to the netns (see /tmp/claude-pasta.log)",
        ),
    ],
)
def test_launch_fails_closed(
    ops: FakeOps, setup: Setup, conf: dict[str, str], message: str
) -> None:
    setup(ops)
    assert launch(ops, None, **conf) == 1
    # The tun device is the test's stand-in, named in the message.
    message = message.replace("/dev/net/tun", jail.TUN)
    assert ops.errors() == [f"claude-sandbox: egress jail {message}"]
    # The holder never got the go-ahead, and whatever started was stopped.
    assert not ops.readied
    assert all(proc.done for proc in ops.procs.values())
    assert ops.jails() == []


def test_without_its_own_interpreter_the_launch_refuses(
    ops: FakeOps, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "executable", "python3")
    assert launch(ops) == 1
    assert ops.errors() == [
        "claude-sandbox: egress jail — cannot locate the sandbox's own Python"
        " interpreter"
    ]


def test_invalid_ports_are_refused_before_anything_starts(ops: FakeOps) -> None:
    rc = launch(
        ops, local_model_port="70000", callback_port_entries="x 1920"
    )  # 1920 is not outbound here: the model port is invalid, not 1920
    assert rc == 1
    assert ops.errors() == [
        "claude-sandbox: local-model-port must be 1–65535 (or 0 to disable).",
        "claude-sandbox: callback-port entries must be 1–65535, got 'x'.",
    ]
    assert ops.kinds("spawn", "run") == [] and ops.jails() == []


@pytest.mark.parametrize(
    ("sig", "expected"),
    [(signal.SIGINT, 130), (signal.SIGTERM, 143), (signal.SIGHUP, 143)],
)
def test_signal_while_the_agent_runs(ops: FakeOps, sig: int, expected: int) -> None:
    ops.on_sleep = lambda n: fire(sig) if n == 3 else None
    ops.next_polls = 1000
    assert launch(ops, local_model_port="1920") == expected
    # A second signal during cleanup changes nothing.
    fire(signal.SIGTERM)
    assert ops.kinds("kill") == [
        ("kill", 100, signal.SIGTERM, True),
        ("kill", 101, signal.SIGTERM, False),
    ]
    assert ops.jails() == []


def test_signal_before_the_holder_is_ready(ops: FakeOps) -> None:
    ops.netns_ready = False
    ops.on_sleep = lambda n: fire(signal.SIGINT)
    assert launch(ops) == 130
    assert not ops.readied
    assert ops.kinds("kill") == [("kill", 100, signal.SIGTERM, False)]


def test_a_relative_command_is_refused(ops: FakeOps) -> None:
    with pytest.raises(SystemExit) as e:
        jail.launch(Config(), {}, ["script", "-c", "x"], ops=ops)
    assert e.value.code == 1
    assert ops.errors() == [
        "claude-sandbox: egress jail — the command to run must be an absolute path"
    ]
    assert ops.kinds("spawn") == [] and ops.jails() == []


def test_a_signal_ignored_on_entry_stays_ignored(new_ops: NewOps) -> None:
    signal.signal(signal.SIGHUP, signal.SIG_IGN)
    launch(new_ops())
    assert signal.getsignal(signal.SIGHUP) == signal.SIG_IGN
    assert callable(signal.getsignal(signal.SIGTERM))
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    with pytest.raises(Exec):
        jail.holder_main(["--", *COMMAND], ops=new_ops())
    assert signal.getsignal(signal.SIGINT) == signal.SIG_IGN


def test_holder_killed_by_a_signal_reports_128_plus_n(ops: FakeOps) -> None:
    ops.next_rc, ops.next_polls = -signal.SIGKILL, 2
    assert launch(ops) == 137


def test_only_a_staged_resolver_is_removed(ops: FakeOps, tmp_path: Path) -> None:
    named = tmp_path / "hosts"
    named.touch()
    launch(ops, {"CLAUDE_SANDBOX_JAIL_RESOLV": str(named)})
    assert named.exists()


# --- the holder ----------------------------------------------------------------

ROUTES = [
    "ip link set lo up",
    "ip route show default",
    "ip -4 -o addr show dev eth0 scope global",
    "ip -o route show dev eth0 scope link",
    "ip -4 route flush table main",
    "ip route replace blackhole 10.0.2.0/24",
    "ip route replace blackhole 10.0.0.0/8",
    "ip route replace blackhole 172.16.0.0/12",
    "ip route replace blackhole 192.168.0.0/16",
    "ip route replace blackhole 100.64.0.0/10",
    "ip route replace unreachable 169.254.0.0/16",
    "ip route replace blackhole 168.63.129.16/32",
    "ip route replace 10.0.2.2/32 dev eth0 src 10.0.2.15",
    "ip route replace default via 10.0.2.2 dev eth0 src 10.0.2.15",
    "ip route replace 192.0.2.53/32 via 10.0.2.2 dev eth0 src 10.0.2.15",
]
READ_BACK = [
    "ip -4 -o route show table all",
    "ip -4 rule show",
    "ip -4 -o addr show",
    "ip route get 1.1.1.1",
    "ip -6 -o addr show",
    "ip -6 -o route show table all",
]


def hold(ops: FakeOps, *argv: str) -> int:
    with pytest.raises(SystemExit) as e:
        jail.holder_main(argv, ops=ops)
    assert isinstance(e.value.code, int)
    return e.value.code


def runs(ops: FakeOps) -> list[str]:
    return [
        " ".join([os.path.basename(str(entry[1])), *map(str, entry[2:])])
        for entry in ops.kinds("run")
    ]


def test_holder_locks_routes_then_execs(
    ops: FakeOps, monkeypatch: pytest.MonkeyPatch
) -> None:
    allow = "203.0.113.7/24\n198.51.100.9/32"
    monkeypatch.setenv("CLAUDE_SANDBOX_ALLOW_IP", allow)
    with pytest.raises(Exec):
        jail.holder_main(["--", *COMMAND], ops=ops)
    punched = [
        f"ip route replace {host}/32 via 10.0.2.2 dev eth0 src 10.0.2.15"
        for host in ("203.0.113.7", "198.51.100.9")
    ]
    assert runs(ops) == [*ROUTES, *punched, *READ_BACK]
    # A prefix is narrowed to one address, and the launch says so.
    assert ops.errors() == [
        "claude-sandbox: egress jail — allow-ip 203.0.113.7/24: only 203.0.113.7"
        " is routed (allow-ip takes single addresses)"
    ]
    assert ops.log[-1] == ("exec", *COMMAND)
    # Started by Python, it resets what Python changed before exec.
    for sig in (signal.SIGINT, signal.SIGPIPE, signal.SIGXFSZ):
        assert signal.getsignal(sig) == signal.SIG_DFL


def test_holder_keeps_a_gateway_that_is_a_connected_route(
    ops: FakeOps, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A mirrored scope-link route can be the gateway's own /32 (DHCP on
    Azure). It is not blackholed: it is the gateway, pinned on-link."""
    linked = LINK_ROUTES + "10.0.2.2 proto dhcp scope link src 10.0.2.15\n"
    monkeypatch.setitem(globals(), "LINK_ROUTES", linked)
    with pytest.raises(Exec):
        jail.holder_main(["--", *COMMAND], ops=ops)
    done = runs(ops)
    assert "ip route replace blackhole 10.0.2.2/32" not in done
    assert ops.main["10.0.2.2/32"] == "10.0.2.2 dev eth0 scope link"
    assert "blackhole 10.0.2.2/32" not in ops.main.values()


def test_mirrored_routes_are_flushed(
    ops: FakeOps, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An Azure-shaped table: pasta mirrors every host route, and a DHCP
    host route to the metadata service or WireServer, or a VPN's internal
    subnet, beats the blackholes. None survives: the main table is rebuilt
    as exactly the allowlist."""
    linked = LINK_ROUTES + "10.0.3.0/24 proto kernel scope link src 10.0.3.9\n"
    monkeypatch.setitem(globals(), "LINK_ROUTES", linked)
    for line in (
        "default via 10.0.2.2 dev eth0 proto dhcp metric 100",
        "10.0.2.2 dev eth0 proto dhcp scope link metric 100",
        "169.254.169.254 via 10.0.2.2 dev eth0 proto dhcp metric 100",
        "168.63.129.16 via 10.0.2.2 dev eth0 proto dhcp metric 100",
        "10.24.0.0/16 via 10.0.2.2 dev eth0",
    ):
        ops.main[line] = line
    with pytest.raises(Exec):
        jail.holder_main(["--", *COMMAND], ops=ops)
    assert sorted(ops.main.values()) == sorted(
        [
            "blackhole 10.0.2.0/24",
            "blackhole 10.0.3.0/24",
            *(f"blackhole {net}" for net in jail.BLACKHOLES),
            "unreachable 169.254.0.0/16",
            "blackhole 168.63.129.16/32",
            "10.0.2.2 dev eth0 scope link",
            "default via 10.0.2.2 dev eth0",
            "192.0.2.53 via 10.0.2.2 dev eth0",
        ]
    )


def test_ipv6_is_switched_off(ops: FakeOps) -> None:
    with pytest.raises(Exec):
        jail.holder_main(["--", *COMMAND], ops=ops)
    assert [Path(path).read_text() for path in jail.IPV6_OFF] == ["1\n", "1\n"]


def unwritable(monkeypatch: pytest.MonkeyPatch, index: int) -> None:
    """One of the IPv6 sysctls cannot be written."""
    off: list[str] = list(jail.IPV6_OFF)
    off[index] = f"{off[index]}/no/such/dir"
    monkeypatch.setattr(jail, "IPV6_OFF", tuple(off))


@pytest.mark.parametrize(
    ("addrs", "routes", "message"),
    [
        (
            "",
            "fe80::/64 dev eth0 proto kernel metric 256\n"
            "multicast ff00::/8 dev eth0 table local\n",
            None,
        ),
        ("2: eth0 inet6 2001:db8::5/64 scope global\n", "", "IPv6 address in the jail"),
        ("", "2001:db8::/64 dev eth0\n", "IPv6 route in the jail"),
        ("", "default via fe80::1 dev eth0\n", "IPv6 route in the jail"),
    ],
)
def test_ipv6_left_on_must_hold_nothing(
    ops: FakeOps,
    monkeypatch: pytest.MonkeyPatch,
    addrs: str,
    routes: str,
    message: str | None,
) -> None:
    """Where the sysctl cannot be written, IPv6 must hold only link-local,
    multicast and the kernel's own routes."""
    unwritable(monkeypatch, 0)
    ops.v6_addrs, ops.v6_routes = addrs, routes
    if message is None:
        with pytest.raises(Exec):
            jail.holder_main(["--", *COMMAND], ops=ops)
        return
    assert hold(ops, "--", "true") == 1
    (error,) = ops.errors()
    assert message in error and error.endswith("(fail-closed)")


def test_egress_must_leave_from_the_interface_address(ops: FakeOps) -> None:
    ops.egress_src = "10.0.3.9"
    assert hold(ops, "--", "true") == 1
    assert ops.errors() == [
        "claude-sandbox: egress jail — egress does not leave from 10.0.2.15"
        " (fail-closed)"
    ]


def test_no_address_on_the_egress_interface_refuses(ops: FakeOps) -> None:
    ops.fail.add("ip -4 -o addr show dev eth0 scope global")
    assert hold(ops, "--", "true") == 1
    assert ops.errors() == [
        "claude-sandbox: egress jail — no IPv4 address on eth0 after pasta attach"
    ]


@pytest.mark.parametrize(
    ("table", "rules", "message"),
    [
        (
            "10.24.0.0/16 via 10.0.2.2 dev eth0\n",
            RULES,
            "unexpected route in the jail: 10.24.0.0/16 via 10.0.2.2 dev eth0",
        ),
        (
            "default via 10.0.2.2 dev eth0 table 100\n",
            RULES,
            "unexpected route in the jail: default via 10.0.2.2 dev eth0 table 100",
        ),
        (
            "",
            RULES + "100:\tfrom all lookup 100\n",
            "unexpected routing rules in the jail: 100: from all lookup 100",
        ),
        ("", RULES + "5:\tfrom 10.0.2.15 lookup main\n", "unexpected routing rules"),
        ("", RULES.split("\n", 1)[1], "unexpected routing rules"),
        (
            "local 10.9.9.9 dev eth0 table local\n",
            RULES,
            "unexpected route in the jail: local 10.9.9.9",
        ),
        (
            "local not-an-address dev eth0 table local\n",
            RULES,
            "unexpected route in the jail: local not-an-address",
        ),
        (
            "unreachable 10.0.0.0/8 table local\n",
            RULES,
            "unexpected route in the jail: unreachable 10.0.0.0/8",
        ),
    ],
)
def test_anything_left_over_refuses_the_launch(
    ops: FakeOps, table: str, rules: str, message: str
) -> None:
    """A route the holder did not set, a route in another table, or a rule
    that picks another table: each could route around the allowlist."""
    ops.other_tables += table
    ops.rules = rules
    assert hold(ops, "--", "true") == 1
    (error,) = ops.errors()
    assert message in error
    assert ops.kinds("exec", "spawn") == []


def test_a_route_that_did_not_take_refuses_the_launch(ops: FakeOps) -> None:
    ops.main_ignores = "blackhole 10.0.0.0/8"
    assert hold(ops, "--", "true") == 1
    (error,) = ops.errors()
    assert error == (
        "claude-sandbox: egress jail — route missing from the jail:"
        " blackhole 10.0.0.0/8 (fail-closed)"
    )


def test_routes_that_cannot_be_read_back_refuse_the_launch(ops: FakeOps) -> None:
    ops.fail.add("ip -4 rule show")
    assert hold(ops, "--", "true") == 1
    assert ops.errors() == [
        "claude-sandbox: egress jail — could not read back the routes (fail-closed)"
    ]


@pytest.mark.parametrize("index", range(4, 14))
def test_holder_fails_closed_on_each_load_bearing_route(
    ops: FakeOps, index: int
) -> None:
    ops.fail.add(ROUTES[index])
    assert hold(ops, "--", *COMMAND) == 1
    assert runs(ops) == ROUTES[: index + 1]
    (message,) = ops.errors()
    assert message.startswith("claude-sandbox: egress jail — failed to ")
    assert message.endswith(" (fail-closed)")
    assert ops.kinds("exec", "spawn") == []


def test_holder_route_messages(new_ops: NewOps) -> None:
    """The wording, for the ones whose wording differs."""
    got: list[str] = []
    for index in (10, 12, 13, 9):
        ops = new_ops()
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


def test_holder_soft_failures_still_launch(
    ops: FakeOps, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLAUDE_SANDBOX_ALLOW_IP", "\n203.0.113.7")
    allow = "ip route replace 203.0.113.7/32 via 10.0.2.2 dev eth0 src 10.0.2.15"
    ops.fail |= {ROUTES[14], allow}
    with pytest.raises(Exec):
        jail.holder_main(["--", *COMMAND], ops=ops)
    assert ops.errors() == [
        "claude-sandbox: egress jail — could not route DNS forwarder 192.0.2.53",
        "claude-sandbox: egress jail — could not route allow-ip 203.0.113.7",
    ]


@pytest.mark.parametrize(
    ("setup", "message"),
    [
        (given(lambda o: o.missing.add("ip")), "needs ip (iproute2)"),
        (
            given(lambda o: o.fail.add("ip link set lo up")),
            "— could not bring up loopback",
        ),
        (given(lambda o: o.ready.unlink()), "— pasta never signalled ready"),
        (
            given(lambda o: o.fail.add("ip route show default")),
            "— no default route after pasta attach",
        ),
    ],
)
def test_holder_refuses_before_routing(
    ops: FakeOps, setup: Setup, message: str
) -> None:
    setup(ops)
    assert hold(ops, "--", "true") == 1
    assert ops.errors() == [f"claude-sandbox: egress jail {message}"]


def test_holder_needs_a_gateway_and_a_device(
    ops: FakeOps, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("test_jail.DEFAULT_ROUTE", "default dev eth0\n")
    assert hold(ops, "--", "true") == 1
    assert ops.errors() == [
        "claude-sandbox: egress jail — no default route via/dev after pasta attach"
    ]


def test_holder_usage_and_exec_failure(new_ops: NewOps) -> None:
    assert hold(new_ops(), "true") == 2
    ops = new_ops()
    ops.spawn_error.add("/nonesuch")
    assert hold(ops, "--", "/nonesuch") == 127
    assert ops.errors()[-1] == "claude-sandbox: /nonesuch: No such file or directory"


@pytest.fixture
def relayed(ops: FakeOps, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> FakeOps:
    """A holder with relays to start, as launch hands them over."""
    (tmp_path / "relay").mkdir()
    monkeypatch.setenv("CLAUDE_JAIL_RELAY_DIR", str(tmp_path / "relay"))
    monkeypatch.setenv("CLAUDE_JAIL_LOCAL_PORTS", "1920 8000 ")
    monkeypatch.setenv("CLAUDE_JAIL_CALLBACK_PORTS", "1455 ")
    ops.listening = {"1920", "8000"}
    return ops


def test_holder_runs_inner_relays_and_the_agent(
    relayed: FakeOps, tmp_path: Path
) -> None:
    ops, relay = relayed, tmp_path / "relay"
    ops.next_rc = -signal.SIGINT  # each spawn exits this way when polled out
    ops.next_polls = 2
    assert hold(ops, "--", *COMMAND) == 130
    assert ops.kinds("spawn") == [
        (
            "spawn", 100, True, "/usr/bin/socat",
            "TCP4-LISTEN:1920,bind=127.0.0.1,reuseaddr,fork",
            f"UNIX-CONNECT:{relay}/1920.sock",
        ),
        (
            "spawn", 101, True, "/usr/bin/socat",
            "TCP4-LISTEN:8000,bind=127.0.0.1,reuseaddr,fork",
            f"UNIX-CONNECT:{relay}/8000.sock",
        ),
        (
            "spawn", 102, True, "/usr/bin/socat",
            f"UNIX-LISTEN:{relay}/in-1455.sock,mode=0600,fork", "TCP4:127.0.0.1:1455",
        ),
        ("spawn", 103, False, *COMMAND),
    ]  # fmt: skip
    assert [entry[1] for entry in ops.kinds("kill")] == [102, 101, 100]


def test_holder_interrupted_stops_the_agent(relayed: FakeOps) -> None:
    relayed.on_sleep = lambda n: fire(signal.SIGHUP)
    assert hold(relayed, "--", "true") == 143
    assert relayed.kinds("kill")[-1] == ("kill", 103, signal.SIGTERM, False)


@pytest.mark.parametrize(
    ("setup", "message"),
    [
        (
            given(lambda o: o.listening.discard("8000")),
            "— loopback listener for port 8000 failed to start",
        ),
        (
            given(lambda o: o.spawn_error.add("/usr/bin/socat")),
            "— loopback listener for port 1920 failed to start",
        ),
        (
            given(lambda o: o.no_socket.add("in-1455.sock")),
            "— callback relay for port 1455 failed to start",
        ),
        (
            given(lambda o: o.missing.add("ss")),
            "needs ss (iproute2) for loopback relays",
        ),
    ],
)
def test_holder_relay_failures_are_fatal(
    relayed: FakeOps, setup: Setup, message: str
) -> None:
    setup(relayed)
    assert hold(relayed, "--", "true") == 1
    assert relayed.errors() == [f"claude-sandbox: egress jail {message}"]
    assert not any(entry[3:] == ("true",) for entry in relayed.kinds("spawn"))


def test_holder_agent_that_cannot_start(relayed: FakeOps) -> None:
    relayed.spawn_error.add("/not/executable")
    assert hold(relayed, "--", "/not/executable") == 126


# --- DNS staging ---------------------------------------------------------------

FORWARDER = "nameserver 192.0.2.53\n"
NO_RESOLVERS = (
    "egress jail — /etc/resolv.conf lists no resolvers; forwarding Claude DNS to"
    " the host's resolvers via pasta. If resolution still fails the host has none"
    " to forward to."
)


@pytest.mark.parametrize(
    ("resolv", "staged_text", "warnings"),
    [
        # Every case is overridden: the jail has one DNS path, the pasta
        # forwarder, so the holder punches no route to a real resolver (#11).
        ("nameserver 8.8.8.8\n", FORWARDER, ()),  # routable
        ("nameserver 127.0.0.53\n", FORWARDER, ()),  # loopback stub
        ("nameserver 127.0.0.53\nnameserver 192.168.1.1\n", FORWARDER, ()),
        ("", FORWARDER, (NO_RESOLVERS,)),  # empty: forwarder, with a warning
        # search / domain / options survive; the real resolvers do not.
        (
            "search a.example b.example\nnameserver 8.8.8.8\noptions timeout:1\n",
            FORWARDER + "search a.example b.example\noptions timeout:1\n",
            (),
        ),
        # Indentation, a keyword with no space after it, a last line with no
        # newline, and CRLF.
        (
            " domain x\ndomain\n\tsearch y\r\n#options z\noptions ndots:2",
            FORWARDER + " domain x\n\tsearch y\noptions ndots:2\n",
            (NO_RESOLVERS,),
        ),
    ],
)
def test_stage_dns(
    tmp_path: Path, resolv: str, staged_text: str, warnings: tuple[str, ...]
) -> None:
    conf = tmp_path / "resolv.conf"
    conf.write_text(resolv)
    (tmp_path / "out").mkdir()
    staged = stage_dns(resolv_conf=str(conf), tmpdir=str(tmp_path / "out"))
    assert staged.path is not None
    assert os.path.basename(staged.path).startswith("claude-jail-resolv.")
    assert os.path.dirname(staged.path) == str(tmp_path / "out")
    assert Path(staged.path).read_text() == staged_text
    assert staged.warnings == warnings


def test_stage_dns_without_a_resolv_conf(tmp_path: Path) -> None:
    staged = stage_dns(resolv_conf=str(tmp_path / "none"), tmpdir=str(tmp_path))
    assert staged.path is not None
    assert Path(staged.path).read_text() == "nameserver 192.0.2.53\n"
    assert len(staged.warnings) == 1


def test_stage_dns_ignores_tmpdir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """$TMPDIR may be in the workspace."""
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
    assert not jail.is_socket(str(log)) and not jail.is_socket(str(tmp_path / "x"))

    relay = ops.spawn(["sh", "-c", "sleep 30 & wait"], env, relay=True)
    assert os.getpgid(relay.pid) == relay.pid  # its own group
    jail.stop_relays([relay], ops)
    assert relay.wait() == -signal.SIGTERM
    ops.kill(relay.pid, signal.SIGTERM)  # already gone: ignored
    child = ops.spawn(["true"], env)
    assert os.getpgid(child.pid) in (os.getpgid(0), child.pid) and child.wait() == 0
    ops.sleep(0)


def test_real_exec() -> None:
    code = (
        "from claude_sandbox.jail import OS\n"
        "OS.execve(['/bin/sh', '-c', 'exit 5'], {})\n"
    )
    done = subprocess.run([sys.executable, "-c", code], check=False)
    assert done.returncode == 5


def test_ipv6_state_that_cannot_be_read_refuses(
    ops: FakeOps, monkeypatch: pytest.MonkeyPatch
) -> None:
    unwritable(monkeypatch, 1)
    ops.fail.add("ip -6 -o addr show")
    assert hold(ops, "--", "true") == 1
    assert ops.errors() == [
        "claude-sandbox: egress jail — could not read back the IPv6 state (fail-closed)"
    ]


def test_an_old_kernels_network_broadcast_route_is_its_own() -> None:
    """Kernels up to 5.13 add a broadcast route for each subnet's network
    address to the local table; refusing it would refuse every launch."""
    addrs = "2: eth0    inet 192.168.1.10/24 brd 192.168.1.255 scope global eth0\n"
    local = (
        "broadcast 192.168.1.0 dev eth0 table local proto kernel scope link"
        " src 192.168.1.10\n"
        "local 192.168.1.10 dev eth0 table local proto kernel scope host"
        " src 192.168.1.10\n"
        "broadcast 192.168.1.255 dev eth0 table local proto kernel scope link"
        " src 192.168.1.10\n"
    )
    jail.check_routes(local, RULES, addrs, set())
    with pytest.raises(jail.JailError, match="unexpected route"):
        jail.check_routes(
            "broadcast 192.168.2.0 dev eth0 table local\n", RULES, addrs, set()
        )
    assert jail.own_addresses("2: eth0 inet not-an-address scope global\n") == {
        "not-an-address"
    }


@pytest.mark.parametrize(
    "line",
    [
        "default \tnexthop via 10.1.0.2 dev d0 weight 1"
        " \tnexthop via 10.2.0.2 dev d1 weight 1",
        "default via 10.0.2.2 dev eth0 via 10.0.3.1 dev eth1",
    ],
)
def test_a_multipath_route_is_refused(line: str) -> None:
    with pytest.raises(jail.JailError, match="multipath route"):
        jail.check_routes(line + "\n", RULES, ADDRS, set())


def test_a_kernel_without_ipv6_has_nothing_to_check(ops: FakeOps) -> None:
    """Booted with ipv6.disable=1: no /proc/sys/net/ipv6, and `ip -6` fails.
    That is IPv6 off, not a refusal."""
    os.rmdir(jail.IPV6_SYSCTLS)
    ops.fail |= {"ip -6 -o addr show", "ip -6 -o route show table all"}
    with pytest.raises(Exec):
        jail.holder_main(["--", *COMMAND], ops=ops)
    assert not any(os.path.exists(path) for path in jail.IPV6_OFF)


# Captured from a real jail (podman, kernel 7.0, iproute2 6.1), with
# allow-ip 203.0.113.7: what `ip -4 -o route show table all` and
# `ip -4 -o addr show` print once the holder has locked the routes.
# ip ends each line with a space; kept, as the parser must cope.
CAPTURED_TABLE = "".join(
    f"{line} \n"
    for line in (
        "default via 192.168.1.1 dev enp5s0 src 192.168.1.10",
        "blackhole 10.0.0.0/8",
        "blackhole 100.64.0.0/10",
        "blackhole 168.63.129.16",
        "unreachable 169.254.0.0/16",
        "blackhole 172.16.0.0/12",
        "192.0.2.53 via 192.168.1.1 dev enp5s0 src 192.168.1.10",
        "blackhole 192.168.0.0/16",
        "blackhole 192.168.1.0/24",
        "192.168.1.1 dev enp5s0 scope link src 192.168.1.10",
        "203.0.113.7 via 192.168.1.1 dev enp5s0 src 192.168.1.10",
        "local 127.0.0.0/8 dev lo table local proto kernel scope host src 127.0.0.1",
        "local 127.0.0.1 dev lo table local proto kernel scope host src 127.0.0.1",
        "broadcast 127.255.255.255 dev lo table local proto kernel scope link"
        " src 127.0.0.1",
        "local 192.168.1.10 dev enp5s0 table local proto kernel scope host"
        " src 192.168.1.10",
        "broadcast 192.168.1.255 dev enp5s0 table local proto kernel scope link"
        " src 192.168.1.10",
    )
)
CAPTURED_ADDRS = (
    "1: lo    inet 127.0.0.1/8 scope host lo\\"
    "       valid_lft forever preferred_lft forever\n"
    "2: enp5s0    inet 192.168.1.10/24 brd 192.168.1.255 scope global"
    " noprefixroute enp5s0\\       valid_lft forever preferred_lft forever\n"
)
CAPTURED_ALLOWED = {
    ("unicast", "0.0.0.0/0", "192.168.1.1", "enp5s0"),
    *(("blackhole", net, "", "") for net in jail.BLACKHOLES),
    ("blackhole", "168.63.129.16/32", "", ""),
    ("blackhole", "192.168.1.0/24", "", ""),
    ("unreachable", "169.254.0.0/16", "", ""),
    ("unicast", "192.0.2.53/32", "192.168.1.1", "enp5s0"),
    ("unicast", "192.168.1.1/32", "", "enp5s0"),
    ("unicast", "203.0.113.7/32", "192.168.1.1", "enp5s0"),
}


def test_a_captured_locked_table_passes() -> None:
    jail.check_routes(CAPTURED_TABLE, RULES, CAPTURED_ADDRS, CAPTURED_ALLOWED)


def test_the_allowlist_is_what_the_holder_set() -> None:
    """Each route the holder sets, read the way the read-back reads it."""
    assert {
        jail.as_route(args)
        for args in (
            "default via 192.168.1.1 dev enp5s0 src 192.168.1.10",
            *(f"blackhole {net}" for net in jail.BLACKHOLES),
            "blackhole 168.63.129.16/32",
            "blackhole 192.168.1.0/24",
            "unreachable 169.254.0.0/16",
            "192.0.2.53/32 via 192.168.1.1 dev enp5s0 src 192.168.1.10",
            "192.168.1.1/32 dev enp5s0 src 192.168.1.10",
            "203.0.113.7/32 via 192.168.1.1 dev enp5s0 src 192.168.1.10",
        )
    } == CAPTURED_ALLOWED


@pytest.mark.parametrize(
    "leftover",
    [
        # What pasta mirrored on the same host before the lock: a DHCP
        # default with a metric (a second default, refused even though it
        # matches ours), the connected subnet, a host route a VPN pushed, and
        # an injected metadata route.
        "default via 192.168.1.1 dev enp5s0 proto dhcp metric 100 ",
        "192.168.1.0/24 dev enp5s0 proto kernel scope link metric 100 ",
        "193.62.221.195 via 192.168.1.1 dev enp5s0 ",
        "169.254.169.254 via 192.168.1.1 dev enp5s0 ",
    ],
)
def test_a_captured_mirrored_route_is_refused(leftover: str) -> None:
    with pytest.raises(jail.JailError, match="unexpected route"):
        jail.check_routes(
            CAPTURED_TABLE + leftover + "\n", RULES, CAPTURED_ADDRS, CAPTURED_ALLOWED
        )
