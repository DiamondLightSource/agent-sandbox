"""jail.py against real namespaces, pasta and socat.

Needs unprivileged user+net namespaces, /dev/net/tun, pasta, socat, ip and
ss: run inside this repository's image, as tests/jail_python.sh does. Skips
elsewhere, unless JAIL_NETNS_REQUIRE=1, which turns a skip into a failure.

The assertions are those of tests/egress_jail.sh and tests/local_model.sh,
ported to drive the Python jail directly, so each case can choose its own
command and configuration. Each test runs a small driver as
`python -I -m claude_sandbox._jail_driver COMMAND...` from a venv holding a
copy of the package: the holder re-enters that same interpreter as
`python -I -m claude_sandbox _jail_holder`, exactly as installed.
"""

import os
import pty
import re
import select
import shutil
import signal
import socket
import subprocess
import sys
import time
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
TOOLS = ("unshare", "pasta", "socat", "ip", "ss", "script", "getent")

# Stands in for the shadow: stage DNS (DRIVER_STAGE_DNS=1), wrap the command
# in script(1) (DRIVER_SCRIPT=1), then hand over to jail.launch.
DRIVER = """\
import os, shlex, sys
from claude_sandbox import jail
from claude_sandbox.config import Config
env = dict(os.environ)
if env.get("DRIVER_STAGE_DNS") == "1":
    staged = jail.stage_dns()
    env[jail.JAIL_RESOLV] = staged.path
command = sys.argv[1:]
if env.get("DRIVER_SCRIPT") == "1":
    env["SHELL"] = "/bin/bash"
    command = ["script", "--return", "-q", "-E", "never", "-c",
               shlex.join(command), "/dev/null"]
jail.launch(Config.from_env(env), env, command)
"""


def _usable() -> str:
    missing = [tool for tool in TOOLS if shutil.which(tool) is None]
    if missing:
        return f"missing {', '.join(missing)}"
    if not os.path.exists("/dev/net/tun"):
        return "no /dev/net/tun"
    if subprocess.run(["unshare", "-rn", "true"], check=False).returncode:
        return "cannot create a user+net namespace"
    return ""


_why = _usable()
if _why:
    if os.environ.get("JAIL_NETNS_REQUIRE") == "1":
        pytest.fail(f"JAIL_NETNS_REQUIRE=1 but {_why}", pytrace=False)
    pytest.skip(_why, allow_module_level=True)


@pytest.fixture(scope="module")
def python(tmp_path_factory: pytest.TempPathFactory) -> str:
    """A venv with a copy of the package, run the way the shim runs it."""
    venv = tmp_path_factory.mktemp("venv")
    subprocess.run([sys.executable, "-m", "venv", "--without-pip", venv], check=True)
    (site,) = venv.glob("lib/python*/site-packages")
    package = site / "claude_sandbox"
    shutil.copytree(REPO / "src" / "claude_sandbox", package)
    (package / "_jail_driver.py").write_text(DRIVER)
    return str(venv / "bin" / "python")


ENV = {"PATH": os.environ["PATH"], "CLAUDE_SANDBOX_LOCAL_MODEL_PORT": "0"}


def driver_argv(python: str, *command: str) -> list[str]:
    return [python, "-I", "-m", "claude_sandbox._jail_driver", *command]


def run(python: str, command: str, **env: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        driver_argv(python, "bash", "-c", command),
        env={**ENV, **env},
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )


def wait_until(ready: Callable[[], bool], timeout: float = 10) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if ready():
            return True
        time.sleep(0.05)
    return ready()


def listening(port: int) -> bool:
    out = subprocess.run(
        ["ss", "-H", "-ltn", f"sport = :{port}"], capture_output=True, text=True
    ).stdout
    return bool(out)


def leftovers() -> list[str]:
    """Anything a jail left behind: holders, pasta, relays, jail dirs."""
    found = [str(p) for p in Path("/tmp").glob("claude-jail.*")]
    found += [str(p) for p in Path("/tmp").glob("claude-jail-resolv.*")]
    for proc in Path("/proc").glob("[0-9]*"):
        try:
            cmdline = (proc / "cmdline").read_bytes().replace(b"\0", b" ")
            comm = (proc / "comm").read_text().strip()
        except OSError:
            continue
        if (
            b"_jail_holder" in cmdline
            or comm.startswith("pasta")
            or comm.startswith("passt")
            or (comm == "socat" and b"/tmp/claude-jail." in cmdline)
        ):
            found.append(f"{proc.name}: {cmdline.decode(errors='replace')}")
    return found


def assert_clean() -> None:
    # pasta notices its namespace has gone on its own; give it a moment.
    wait_until(lambda: not leftovers(), timeout=5)
    assert leftovers() == []


@pytest.fixture
def listeners() -> Iterator[Callable[[int, str], None]]:
    """Start socat listeners on the OUTER 127.0.0.1; stopped afterwards."""
    procs: list[subprocess.Popen[bytes]] = []

    def start(port: int, target: str) -> None:
        procs.append(
            subprocess.Popen(
                ["socat", f"TCP4-LISTEN:{port},bind=127.0.0.1,reuseaddr,fork", target],
                start_new_session=True,
            )
        )
        assert wait_until(lambda: listening(port))

    yield start
    for proc in procs:
        os.killpg(proc.pid, signal.SIGTERM)
        proc.wait()


@pytest.fixture(autouse=True)
def clean() -> Iterator[None]:
    assert leftovers() == []
    yield
    assert_clean()


# --- egress_jail.sh: the routing allowlist ------------------------------------

ROUTES_PROBE = r"""
set -u
ip -4 route show table all
echo "V6ROUTABLE:$(ip -6 addr show | awk '/inet6/{print $2}' \
    | grep -vE '^(::1/|fe80:)' | tr '\n' ' ')"
gw="$(ip route show default | awk '{print $3; exit}')"
echo "GW:$gw"
echo "GWROUTE:$(ip route get "$gw" | head -n1)"
for ip in 10.1.2.3 172.16.5.5 192.168.77.1 100.64.1.1 169.254.169.254; do
    if timeout 3 bash -c "exec 3<>/dev/tcp/$ip/80" 2>/dev/null; then
        echo "REACHED:$ip"
    fi
done
# By address: this probe has the container's resolvers, all blackholed.
echo "EGRESS:$(curl -ksS -o /dev/null -w '%{http_code}' --max-time 20 \
    https://1.1.1.1 2>&1)"
"""


def test_routes_are_locked_and_the_internet_is_reachable(python: str) -> None:
    done = run(python, ROUTES_PROBE, CLAUDE_SANDBOX_ALLOW_IP="203.0.113.7")
    assert done.returncode == 0, done.stderr
    out = done.stdout
    gw = re.search(r"^GW:(\S+)$", out, re.M)
    assert gw is not None, out
    gw_re = re.escape(gw.group(1))
    for line in (
        "blackhole 10.0.0.0/8",
        "blackhole 172.16.0.0/12",
        "blackhole 192.168.0.0/16",
        "blackhole 100.64.0.0/10",
        "unreachable 169.254.0.0/16",
    ):
        assert re.search(rf"^{re.escape(line)}( |$)", out, re.M), out
    nic = re.search(rf"^default via {gw_re} dev (\S+)", out, re.M)
    assert nic is not None, out
    # Every connected subnet pasta mirrored from this container is
    # blackholed: each is more specific than the RFC1918 blackholes.
    mirrored = subprocess.run(
        ["ip", "-o", "route", "show", "dev", nic.group(1), "scope", "link"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split("\n")
    for subnet in (line.split()[0] for line in mirrored if line.split()):
        assert re.search(rf"^blackhole {re.escape(subnet)}( |$)", out, re.M), out
    # The gateway stays routable, on-link, and only the forwarder and the
    # allow-ip device are punched back through it.
    assert re.search(rf"^{gw_re} dev {nic.group(1)}", out, re.M), out
    assert re.search(rf"^GWROUTE:{gw_re} dev ", out, re.M), out
    assert re.search(rf"^192\.0\.2\.53 via {gw_re} ", out, re.M), out
    assert re.search(rf"^203\.0\.113\.7 via {gw_re} ", out, re.M), out
    assert re.search(r"^V6ROUTABLE: *$", out, re.M), out
    assert "REACHED:" not in out
    assert re.search(r"^EGRESS:[23]\d\d$", out, re.M), out


def test_dns_goes_through_the_forwarder(python: str) -> None:
    probe = (
        'unshare -m bash -c \'mount --bind "$CLAUDE_SANDBOX_JAIL_RESOLV"'
        " /etc/resolv.conf && cat /etc/resolv.conf && getent hosts example.com'"
    )
    done = run(python, probe, DRIVER_STAGE_DNS="1")
    assert done.returncode == 0, done.stderr
    assert re.findall(r"^nameserver (\S+)", done.stdout, re.M) == ["192.0.2.53"]
    assert re.search(r"example\.com", done.stdout), done.stdout


# --- local_model.sh: the loopback relays ---------------------------------------

RELAY_PROBE = r"""
set -euo pipefail
echo "socket=$CLAUDE_JAIL_RELAY_DIR/31920.sock"
expected="$(head -c 262144 /dev/zero | sha256sum)"
actual="$(head -c 262144 /dev/zero | socat - TCP4:127.0.0.1:31920 | sha256sum)"
[ "$expected" = "$actual" ]
[ "$(socat - TCP4:127.0.0.1:31922 < /dev/null)" = second-port ]
if timeout 2 bash -c 'exec 3<>/dev/tcp/127.0.0.1/31921' 2>/dev/null; then
    echo 'Unexpected reach to a different host-loopback port' >&2; exit 1
fi
gw="$(ip route show default | awk '{print $3; exit}')"
if timeout 2 bash -c 'exec 3<>/dev/tcp/$1/31921' _ "$gw" 2>/dev/null; then
    echo 'Gateway unexpectedly maps to host loopback' >&2; exit 1
fi
ss -H -ltn 'sport = :31920' | grep -q '127.0.0.1:31920'
ss -H -ltn 'sport = :31922' | grep -q '127.0.0.1:31922'
[ "$(ss -H -ltn 'sport = :31922' | grep -vc '127.0.0.1:')" = 0 ]
[ -S "$CLAUDE_JAIL_RELAY_DIR/in-31955.sock" ]
[ -z "$(ss -H -ltn 'sport = :31955')" ]
echo RELAY_OK
exit 17
"""


def test_local_model_relay(python: str, listeners: Callable[[int, str], None]) -> None:
    listeners(31920, "EXEC:/bin/cat")
    listeners(31921, "EXEC:/bin/cat")
    listeners(31922, "EXEC:/bin/echo second-port")
    done = run(
        python,
        RELAY_PROBE,
        CLAUDE_SANDBOX_LOCAL_MODEL_PORT="31920",
        CLAUDE_SANDBOX_LOCAL_PORTS="31922",
        CLAUDE_SANDBOX_CALLBACK_PORTS="31955",
    )
    assert done.returncode == 17, done.stderr
    assert "RELAY_OK" in done.stdout


def test_missing_model_server_does_not_block_launch(python: str) -> None:
    done = run(
        python, "echo CLOUD_OK; exit 19", CLAUDE_SANDBOX_LOCAL_MODEL_PORT="31920"
    )
    assert (done.returncode, done.stdout) == (19, "CLOUD_OK\n")


CALLBACK_PROBE = r"""
set -euo pipefail
while [ ! -e "$1/go" ]; do sleep 0.05; done
socat TCP4-LISTEN:31955,bind=127.0.0.1,reuseaddr,fork "EXEC:/bin/echo callback-ok" &
listener=$!
trap 'kill "$listener" 2>/dev/null; exit 143' TERM INT
while ! ss -H -ltn 'sport = :31955' | grep -q '127.0.0.1:'; do sleep 0.05; done
echo LISTENING
sleep 60 & wait $!
"""


def connect(port: int) -> bytes:
    with socket.create_connection(("127.0.0.1", port), timeout=5) as conn:
        conn.shutdown(socket.SHUT_WR)
        out = b""
        while chunk := conn.recv(4096):
            out += chunk
        return out


def test_callback_relay(python: str, tmp_path: Path) -> None:
    out = tmp_path / "out"
    with out.open("w") as f:
        proc = subprocess.Popen(
            driver_argv(python, "bash", "-c", CALLBACK_PROBE, "_", str(tmp_path)),
            env={**ENV, "CLAUDE_SANDBOX_CALLBACK_PORTS": "31955"},
            stdout=f,
        )
    try:
        assert wait_until(lambda: listening(31955))
        # Nothing listening inside yet: refused fast, not hung.
        start = time.monotonic()
        assert connect(31955) == b""
        assert time.monotonic() - start < 4
        (tmp_path / "go").touch()
        assert wait_until(lambda: "LISTENING" in out.read_text())
        assert connect(31955) == b"callback-ok\n"
        assert not listening(31956)
        proc.send_signal(signal.SIGTERM)
        assert proc.wait(timeout=20) == 143
    finally:
        if proc.poll() is None:
            proc.kill()
    assert not listening(31955)


def test_busy_callback_port_fails_soft(
    python: str, listeners: Callable[[int, str], None]
) -> None:
    listeners(31955, "EXEC:/bin/cat")
    done = run(python, "echo BUSY_OK; exit 21", CLAUDE_SANDBOX_CALLBACK_PORTS="31955")
    assert (done.returncode, done.stdout) == (21, "BUSY_OK\n")
    assert "callback-port 31955 is already in use" in done.stderr


# --- cleanup and signals ---------------------------------------------------------


@pytest.mark.parametrize(
    ("sig", "expected"),
    [(None, 0), (signal.SIGINT, 130), (signal.SIGTERM, 143), (signal.SIGHUP, 143)],
)
@pytest.mark.parametrize("relays", [False, True], ids=["exec", "relays"])
def test_cleanup(
    python: str, tmp_path: Path, sig: signal.Signals | None, expected: int, relays: bool
) -> None:
    ready = tmp_path / "ready"
    command = f"touch {ready}; " + ("exit 0" if sig is None else "sleep 60")
    env: Mapping[str, str] = {
        **ENV,
        "DRIVER_STAGE_DNS": "1",
        "CLAUDE_SANDBOX_LOCAL_MODEL_PORT": "31920" if relays else "0",
        "CLAUDE_SANDBOX_CALLBACK_PORTS": "31955" if relays else "",
    }
    proc = subprocess.Popen(driver_argv(python, "bash", "-c", command), env=env)
    try:
        assert wait_until(ready.exists)
        if sig is not None:
            assert leftovers() != []  # the jail is really up
            proc.send_signal(sig)
        assert proc.wait(timeout=20) == expected
    finally:
        if proc.poll() is None:
            proc.kill()
    assert_clean()


# --- the terminal ------------------------------------------------------------------

TTY_CHILD = (
    'if [ -t 0 ]; then echo STDIN_IS_TTY; fi; read -r line; echo "GOT:$line"; sleep 60'
)


def read_until(fd: int, needle: bytes, timeout: float = 20) -> bytes:
    out = b""
    end = time.monotonic() + timeout
    while needle not in out:
        left = end - time.monotonic()
        if left <= 0 or not select.select([fd], [], [], left)[0]:
            raise AssertionError(f"no {needle!r} in {out!r}")
        try:
            out += os.read(fd, 4096)
        except OSError:  # EIO: the other side has gone
            raise AssertionError(f"no {needle!r} in {out!r}") from None
    return out


@pytest.mark.parametrize("variant", ["exec", "relays", "script", "script-relays"])
def test_ctrl_c_while_the_child_owns_the_terminal(python: str, variant: str) -> None:
    """^C typed at a real terminal. Without script(1) the terminal sends
    SIGINT to the whole foreground group; with it (the shadow's shape)
    script puts the terminal in raw mode and passes ^C to the child's own
    pty. Either way: 130, and nothing left behind."""
    env = {
        **ENV,
        "DRIVER_SCRIPT": "1" if variant.startswith("script") else "0",
        "CLAUDE_SANDBOX_LOCAL_MODEL_PORT": "31920" if "relays" in variant else "0",
    }
    pid, master = pty.fork()
    if pid == 0:  # the child: a session leader on the pty
        os.execve(python, driver_argv(python, "bash", "-c", TTY_CHILD), env)
    try:
        read_until(master, b"STDIN_IS_TTY")
        # A read that completes proves the child is in the terminal's
        # foreground process group: a background read would stop on SIGTTIN.
        os.write(master, b"hello\n")
        read_until(master, b"GOT:hello")
        os.write(master, b"\x03")
        _, wait_status = os.waitpid(pid, 0)
        assert os.waitstatus_to_exitcode(wait_status) == 130
    finally:
        os.close(master)
        try:
            os.kill(pid, signal.SIGKILL)
            os.waitpid(pid, 0)
        except (ProcessLookupError, ChildProcessError):
            pass
