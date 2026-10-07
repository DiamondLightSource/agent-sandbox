"""The egress jail (ADR 0015, Design D) and its loopback relays (ADRs 20, 21).

The launch runs inside a user+net namespace that a *holder* process owns,
bridged to the internet by pasta, with a routing allowlist the agent cannot
change:

1. ``stage_dns`` (called by the shadow BEFORE it builds the bwrap argv)
   writes a resolv.conf naming only the pasta forwarder. ``bwrap.py`` binds
   it over /etc/resolv.conf when ``CLAUDE_SANDBOX_JAIL_RESOLV`` names it.
2. ``launch`` checks its tools and the ports, starts the outer relay ends,
   then starts the holder: ``unshare -rn`` re-entering this package as
   ``python -I -m claude_sandbox _jail_holder -- COMMAND``.
3. Once the holder owns its namespaces, pasta attaches to it from outside and
   ``launch`` signals readiness through a file.
4. ``holder_main`` (inside the namespace) brings up loopback, waits for that
   file, locks the routes, starts the inner relay ends, and runs COMMAND.

The order is load-bearing: netns, then pasta, then routes locked, then the
agent. Every step that the security of the jail rests on fails CLOSED: the
process exits non-zero and the agent never starts. Only the DNS forwarder
route, ``allow-ip`` routes and callback relays fail soft, because losing one
loses reachability, never containment.

The holder owns the netns from an ANCESTOR user namespace, so the caps bwrap
leaves the agent in its own nested userns confer no authority over the routes.
Ancestor-userns ownership is the boundary, not caplessness.

No executable is found through PATH (ADR 26). ``unshare``, ``pasta``,
``socat``, ``ip`` and ``ss`` come from ``tools.TOOL_PATH`` as absolute paths,
and a missing one is a fail-closed refusal; the command to run must already
be absolute; the holder re-enters ``sys.executable``.

Readiness is the holder in a netns of its own with its uid and gid maps
written: ``/proc/<holder>/ns/net`` exists before ``unshare`` has made the
namespace. The holder and the agent keep the default signal dispositions,
as on the unjailed path, so ^C reaches the child's own terminal. Signal
handlers only record the signal (see ``Signals``), so cleanup always runs
whole; only SIGKILL cuts it short. The staged resolv.conf is removed after
every failure.

Processes and ``/proc`` go through ``Ops`` so tests can replace them.
Standard library only: this module is on the launch path (ADR 26).
"""

import ipaddress
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from functools import partial
from types import FrameType
from typing import NoReturn, Protocol

from .config import (
    ALLOW_IP,
    TUN,
    Config,
    callback_ports,
    lines,
    local_ports,
    validate_callback_ports,
    validate_local_model_port,
)
from .errors import SandboxError
from .tools import find_tool, shell_status

# In-netns DNS forwarder (issues #60, #11). ALL of the agent's DNS goes here.
# pasta's --dns-forward listens on it INSIDE the netns and relays to the
# host's resolvers from the HOST netns, so the jail needs no route to any
# real resolver. RFC5737 TEST-NET-1: never a real host, outside every
# blackholed range.
JAIL_DNS_FWD = "192.0.2.53"

# The argument ``__main__.py`` must dispatch to ``holder_main`` before it
# imports anything else, exactly as it dispatches ``_shadow``.
HOLDER_ENTRY = "_jail_holder"

RESOLV_CONF = "/etc/resolv.conf"
# The staged resolver, read by bwrap.py and removed by launch on exit.
JAIL_RESOLV = "CLAUDE_SANDBOX_JAIL_RESOLV"
_RESOLV_PREFIX = "claude-jail-resolv."

# What launch hands the holder through its environment.
JAIL_READY = "CLAUDE_JAIL_READY"
JAIL_RELAY_DIR = "CLAUDE_JAIL_RELAY_DIR"
JAIL_LOCAL_PORTS = "CLAUDE_JAIL_LOCAL_PORTS"
JAIL_CALLBACK_PORTS = "CLAUDE_JAIL_CALLBACK_PORTS"

# Where the jail's directory (the relay sockets and the readiness handshake)
# and the staged resolver go. Always /tmp, which bwrap masks. Never TMPDIR:
# it may sit in the writable workspace, and the agent must not reach them.
JAIL_TMP = "/tmp"

# pasta's log. A fixed path, as in the container workflow's diagnostics.
PASTA_LOG = "/tmp/claude-pasta.log"

# What an old pasta logs when it cannot attach from inside a container.
# Debian's passt before 0.0~git20230908 (Debian 12 has 0.0~git20230309)
# installs pasta as a symlink to passt, so a host enforcing AppArmor
# confines it under the profile for passt, which denies the holder's
# /proc/PID/ns/* and pasta's own log file: both opens fail with EACCES.
# Later packages make pasta a hard link with its own profile (issue #85;
# config.PASST_MIN, which the install and doctor check). The jail stays
# closed either way; this only says why.
PASTA_CANNOT_OPEN = "Couldn't open"
PASTA_TOO_OLD = (
    "\n  This passt is too old to attach to a namespace from inside a container"
    "\n  on an AppArmor host (Debian 12's is); use a base image with passt"
    "\n  0.0~git20230908 or later, such as Ubuntu 24.04 or Debian 13."
)

# pasta flags. IPv4-only keeps all traffic within the routing policy (the
# netns has no IPv6 to blackhole). Port forwarding and gateway-to-loopback
# mapping are off (ADR 19): loopback crosses only through the relays below.
PASTA_FLAGS = (
    "--config-net",
    "--ipv4-only",
    "--no-map-gw",
    "-t", "none",
    "-u", "none",
    "-T", "none",
    "-U", "none",
    "--dns-forward", JAIL_DNS_FWD,
    "--quiet",
    "--log-file", PASTA_LOG,
)  # fmt: skip

# Internal ranges the holder blackholes (CGNAT so a Tailscale-addressed host
# cannot be pivoted to), then link-local, where the clouds' metadata
# services live, marked unreachable.
BLACKHOLES = ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10")
_BLACKHOLE_NOTES = {"100.64.0.0/10": " CGNAT"}
# Azure's WireServer: a public address only the VM it serves can reach, so
# through pasta the jail could. A metadata-class service, like link-local.
WIRESERVER = "168.63.129.16/32"
LINK_LOCAL = "169.254.0.0/16"

# wait_for: up to ~10s, 200 polls 50ms apart.
WAIT_TRIES = 200
WAIT_STEP = 0.05

# The exit status after a signal, as a shell reports it.
SIGNAL_STATUS = {signal.SIGINT: 130, signal.SIGTERM: 143, signal.SIGHUP: 143}


class JailError(Exception):
    """A fail-closed refusal. The message follows ``egress jail``."""


class Interrupted(Exception):
    """INT, TERM or HUP arrived; carries the exit status."""

    def __init__(self, status: int) -> None:
        super().__init__(status)
        self.status = status


class Proc(Protocol):
    """The part of ``subprocess.Popen`` the jail uses."""

    @property
    def pid(self) -> int: ...
    def poll(self) -> int | None: ...
    def wait(self) -> int: ...


class Ops:
    """The processes the jail starts and the ``/proc`` it reads, so tests
    can replace them."""

    def read(self, path: str) -> str:
        """A /proc file or link, or "" when it is gone."""
        try:
            if os.path.islink(path):
                return os.readlink(path)
            with open(path) as f:
                return f.read()
        except OSError:
            return ""

    def run(
        self,
        argv: Sequence[str],
        env: Mapping[str, str],
        *,
        quiet: bool = False,
        stderr_to: str | None = None,
    ) -> tuple[int, str]:
        """Run to completion: (status, stdout). 127 when it cannot start.

        Captures stdout only when ``quiet`` is false and the caller reads
        it; pasta forks a daemon, so its stdout is never a pipe.
        """
        err = None
        try:
            if stderr_to is not None:
                err = open(stderr_to, "ab")
            done = subprocess.run(
                argv,
                env=dict(env),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE if stderr_to is None else None,
                stderr=subprocess.DEVNULL if quiet else err,
                check=False,
            )
        except OSError:
            return 127, ""
        finally:
            if err is not None:
                err.close()
        return done.returncode, os.fsdecode(done.stdout or b"")

    def spawn(
        self, argv: Sequence[str], env: Mapping[str, str], *, relay: bool = False
    ) -> Proc:
        """Start ``argv``. A relay gets /dev/null for stdin and stderr and
        its own session, so killing its process group stops every forked
        connection too. Anything else keeps stdin on the terminal and stays
        in the terminal's foreground process group."""
        if relay:
            return subprocess.Popen(
                argv,
                env=dict(env),
                stdin=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        return subprocess.Popen(argv, env=dict(env))

    def execve(self, argv: Sequence[str], env: Mapping[str, str]) -> NoReturn:
        os.execve(argv[0], list(argv), dict(env))

    def kill(self, pid: int, sig: int, *, group: bool = False) -> None:
        with suppress(OSError):
            (os.killpg if group else os.kill)(pid, sig)

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)


OS = Ops()


def _say(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


class Signals:
    """INT exits 130, TERM and HUP exit 143, as a shell's would.

    The handler only records the first signal; ``check`` raises it at the
    points where stopping is safe. Raising from the handler itself could
    land between a fork and the record of the child's pid, leaving a
    holder or relay nobody kills, or in the middle of cleanup. A signal
    ignored on entry stays ignored.
    """

    def __init__(self) -> None:
        self.status: int | None = None
        for sig in SIGNAL_STATUS:
            if signal.getsignal(sig) != signal.SIG_IGN:
                signal.signal(sig, self._record)

    def _record(self, signum: int, frame: FrameType | None) -> None:
        if self.status is None:
            self.status = SIGNAL_STATUS[signal.Signals(signum)]

    def check(self) -> None:
        if self.status is not None:
            raise Interrupted(self.status)


def wait_for(
    ready: Callable[[], bool], ops: Ops, signals: Signals | None = None
) -> bool:
    """Poll ``ready`` for up to ~10s; True the moment it holds."""
    for _ in range(WAIT_TRIES):
        if ready():
            return True
        if signals is not None:
            signals.check()
        ops.sleep(WAIT_STEP)
    return False


def wait_child(proc: Proc, ops: Ops, signals: Signals) -> int:
    """Wait for ``proc`` to exit, or for a signal; its status."""
    while (rc := proc.poll()) is None:
        signals.check()
        ops.sleep(WAIT_STEP)
    return shell_status(rc)


def stop_child(proc: Proc | None, ops: Ops) -> None:
    if proc is not None and proc.poll() is None:
        ops.kill(proc.pid, signal.SIGTERM)
        proc.wait()


def need(name: str, why: str) -> str:
    """The absolute path of tool ``name``, or a fail-closed refusal."""
    path = find_tool(name)
    if path is None:
        raise JailError(f"needs {name} {why}")
    return path


def is_socket(path: str) -> bool:
    try:
        return stat.S_ISSOCK(os.stat(path).st_mode)
    except OSError:
        return False


def unix_to_tcp(socat: str, sock: str, port: str) -> list[str]:
    """A relay that listens on the Unix socket ``sock`` and connects each
    connection to 127.0.0.1:``port``."""
    return [socat, f"UNIX-LISTEN:{sock},mode=0600,fork", f"TCP4:127.0.0.1:{port}"]


def tcp_to_unix(socat: str, port: str, sock: str) -> list[str]:
    """A relay that listens on 127.0.0.1:``port`` and connects each
    connection to the Unix socket ``sock``."""
    return [
        socat,
        f"TCP4-LISTEN:{port},bind=127.0.0.1,reuseaddr,fork",
        f"UNIX-CONNECT:{sock}",
    ]


class Relays:
    """The socat relays one side of the jail starts, stopped together."""

    def __init__(self, env: Mapping[str, str], ops: Ops, signals: Signals) -> None:
        self.env, self.ops, self.signals = env, ops, signals
        self.procs: list[Proc] = []

    def start(self, argv: Sequence[str], ready: Callable[[], bool]) -> bool:
        """Start a relay; True once ``ready`` holds."""
        try:
            self.procs.append(self.ops.spawn(argv, self.env, relay=True))
        except OSError:
            return False
        return wait_for(ready, self.ops, self.signals)

    def listening(self, ss: str, port: str, *, loopback: bool = False) -> bool:
        """Something listens on TCP ``port`` in this netns, on 127.0.0.1 with
        ``loopback``. Reads the kernel's listener table: it never connects
        to the service, which need not be running."""
        found = self.ops.run([ss, "-H", "-ltn", f"sport = :{port}"], self.env)[1]
        return "127.0.0.1:" in found if loopback else bool(found)

    def stop(self) -> None:
        """TERM each relay's process group, newest first, then reap it."""
        while self.procs:
            proc = self.procs.pop()
            self.ops.kill(proc.pid, signal.SIGTERM, group=True)
            proc.wait()


# --- DNS ---------------------------------------------------------------------


@dataclass(frozen=True)
class StagedDns:
    """What ``stage_dns`` did: the file to bind (None when it could not
    stage one) and the warnings for the shadow's ``launch_warn``."""

    path: str | None
    warnings: tuple[str, ...] = ()


_KEEP = re.compile(rb"[ \t\n\v\f\r]*(search|domain|options)[ \t\n\v\f\r]")
_NAMESERVER = re.compile(rb"[ \t\n\v\f\r]*nameserver")


def stage_dns(*, resolv_conf: str = RESOLV_CONF, tmpdir: str = JAIL_TMP) -> StagedDns:
    """Stage a resolv.conf that sends every query to the pasta forwarder.

    The host's ``search``, ``domain`` and ``options`` lines are kept:
    dropping them breaks short-name resolution at sites with a search list.
    Its real resolvers are dropped: a route to one would open that internal
    host on every port (issue #11). Written under /tmp, which bwrap masks,
    never ``$TMPDIR``, which may sit in the writable workspace. Raises
    SandboxError if the file cannot be written.
    """
    try:
        fd, path = tempfile.mkstemp(prefix=_RESOLV_PREFIX, dir=tmpdir)
    except OSError:
        return StagedDns(
            None,
            (
                "egress jail — could not stage a DNS override (mktemp failed);"
                " name resolution may fail.",
            ),
        )
    try:
        with open(resolv_conf, "rb") as f:
            host = f.read().split(b"\n")
    except OSError:
        host = []
    if host and host[-1] == b"":
        host.pop()
    kept = [line + b"\n" for line in host if _KEEP.match(line)]
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(f"nameserver {JAIL_DNS_FWD}\n".encode())
            f.writelines(kept)
    except OSError as e:
        os.remove(path)
        raise SandboxError(
            f"claude-sandbox: egress jail — could not write {path}: {e.strerror}"
        ) from None
    warnings: tuple[str, ...] = ()
    # pasta forwards to the host's own resolvers; with none there is nothing
    # to forward to. Warn rather than fail: pasta attach is the gate.
    if not any(_NAMESERVER.match(line) for line in host):
        warnings = (
            "egress jail — /etc/resolv.conf lists no resolvers; forwarding"
            " Claude DNS to the host's resolvers via pasta. If resolution still"
            " fails the host has none to forward to.",
        )
    return StagedDns(path, warnings)


# --- outside the jail ----------------------------------------------------------


@dataclass
class _Outer:
    """What ``launch`` must undo, in this order."""

    relays: Relays
    holder: Proc | None = None
    jail_dir: str | None = None


def launch(
    config: Config, env: Mapping[str, str], command: Sequence[str], *, ops: Ops = OS
) -> NoReturn:
    """Run ``command`` in the egress jail and exit with its status.

    ``env`` is the launch environment, ``CLAUDE_SANDBOX_JAIL_RESOLV``
    included when ``stage_dns`` staged a file; that file is removed on exit.
    Exits 1, without starting ``command``, on any setup failure; 130 on
    INT; 143 on TERM or HUP; otherwise the holder's status.
    """
    raise SystemExit(_launch(config, env, command, ops))


def _launch(
    config: Config, env: Mapping[str, str], command: Sequence[str], ops: Ops
) -> int:
    signals = Signals()
    state = _Outer(Relays(env, ops, signals))
    try:
        return _start(config, env, command, ops, signals, state)
    except JailError as e:
        _say(f"claude-sandbox: egress jail {e}")
        return 1
    except Interrupted as e:
        return e.status
    finally:
        # Signals only set a flag now, so nothing interrupts this.
        state.relays.stop()
        stop_child(state.holder, ops)
        if state.jail_dir is not None:
            shutil.rmtree(state.jail_dir, ignore_errors=True)
        resolv = env.get(JAIL_RESOLV, "")
        # Only a file stage_dns made: never delete what a caller named.
        if os.path.basename(resolv).startswith(_RESOLV_PREFIX):
            with suppress(OSError):
                os.remove(resolv)


def _start(
    config: Config,
    env: Mapping[str, str],
    command: Sequence[str],
    ops: Ops,
    signals: Signals,
    state: _Outer,
) -> int:
    # Fail-closed: no unjailed fallback. The messages deliberately do not
    # name the CLAUDE_SANDBOX_EGRESS_JAIL=0 escape hatch.
    unshare = need("unshare", "(util-linux)")
    pasta = need("pasta", "(apt-get install passt)")
    if not os.path.exists(TUN):
        raise JailError(f"needs {TUN} — add --device={TUN} to the container")
    # The holder must be this interpreter, by absolute path, never PATH's,
    # and the holder runs the command as given, never searching PATH.
    if not os.path.isabs(sys.executable):
        raise JailError("— cannot locate the sandbox's own Python interpreter")
    if not command or not os.path.isabs(command[0]):
        raise JailError("— the command to run must be an absolute path")

    # Every port is checked before anything starts.
    errors = validate_local_model_port(config) + validate_callback_ports(config)
    if errors:
        for error in errors:
            _say(error)
        return 1
    outbound = local_ports(config)
    inbound = callback_ports(config)

    try:
        state.jail_dir = tempfile.mkdtemp(prefix="claude-jail.", dir=JAIL_TMP)
    except OSError as e:
        raise JailError(f"— cannot create its directory under /tmp: {e}") from None
    holder_env = dict(env)
    for name in (JAIL_LOCAL_PORTS, JAIL_CALLBACK_PORTS, JAIL_RELAY_DIR, ALLOW_IP):
        holder_env.pop(name, None)
    holder_env[JAIL_READY] = f"{state.jail_dir}/ready"
    if config.allow_ip:
        holder_env[ALLOW_IP] = config.allow_ip

    relays = state.relays
    if outbound or inbound:
        socat = need("socat", "for loopback relays")
        relay_dir = f"{state.jail_dir}/relay"
        os.mkdir(relay_dir)
        os.chmod(relay_dir, 0o700)
        holder_env[JAIL_RELAY_DIR] = relay_dir
        # Outer ends of the local-port relays (ADR 20): one private Unix
        # socket per port, connected to the outer 127.0.0.1. No IP route, no
        # mapping of the host's whole loopback. Fatal if one does not start.
        for port in outbound:
            sock = f"{relay_dir}/{port}.sock"
            if not relays.start(
                unix_to_tcp(socat, sock, port), partial(is_socket, sock)
            ):
                raise JailError(f"— loopback relay for port {port} failed to start")
        if outbound:
            holder_env[JAIL_LOCAL_PORTS] = "".join(f"{p} " for p in outbound)
        # Outer ends of the callback relays (ADR 21), which LISTEN on the
        # outer 127.0.0.1 and so can collide with a second session or an
        # unwrapped agent. Fail soft: warn and carry on. The holder waits
        # only on the ports that started.
        if inbound:
            ss = need("ss", "(iproute2) for loopback relays")
            started: list[str] = []
            for port in inbound:
                in_use = partial(relays.listening, ss, port)
                if in_use():
                    _say(
                        f"claude-sandbox: callback-port {port} is already in use on"
                        " this host; browser logins on that port will not reach this"
                        " session."
                    )
                    continue
                argv = tcp_to_unix(socat, port, f"{relay_dir}/in-{port}.sock")
                if relays.start(argv, in_use):
                    started.append(port)
                else:
                    _say(
                        f"claude-sandbox: callback-port {port} relay failed to start;"
                        " browser logins on that port will not reach this session."
                    )
            holder_env[JAIL_CALLBACK_PORTS] = "".join(f"{p} " for p in started)

    # The holder inherits stdin and the process group: it, and the script(1)
    # and bwrap it runs, stay in the terminal's foreground group, so they
    # read the terminal without SIGTTIN or SIGTTOU.
    try:
        holder = state.holder = ops.spawn(
            [unshare, "-rn", sys.executable, "-I", "-m", "claude_sandbox",
             HOLDER_ENTRY, "--", *command],
            holder_env,
        )  # fmt: skip
    except OSError as e:
        raise JailError(f"— cannot start the holder: {e}") from None
    signals.check()

    # Ready once the holder is in a netns of its own with its uid and gid
    # maps written; unshare then execs the holder. Attaching any earlier
    # would put pasta in OUR namespaces.
    own_netns = ops.read("/proc/self/ns/net")

    def in_own_netns() -> bool:
        if (rc := holder.poll()) is not None:
            raise JailError(
                f"— holder exited with status {shell_status(rc)}"
                " before pasta could attach"
            )
        proc = f"/proc/{holder.pid}"
        netns = ops.read(f"{proc}/ns/net")
        return (
            netns not in ("", own_netns)
            and ops.read(f"{proc}/uid_map") != ""
            and ops.read(f"{proc}/gid_map") != ""
        )

    if not wait_for(in_own_netns, ops, signals):
        raise JailError("— holder netns never appeared")

    rc, _ = ops.run([pasta, *PASTA_FLAGS, str(holder.pid)], env, stderr_to=PASTA_LOG)
    if rc != 0:
        raise JailError(
            f"— pasta failed to attach to the netns (see {PASTA_LOG})"
            + (PASTA_TOO_OLD if PASTA_CANNOT_OPEN in ops.read(PASTA_LOG) else "")
        )
    signals.check()
    with open(holder_env[JAIL_READY], "a"):
        pass

    rc = wait_child(holder, ops, signals)
    state.holder = None
    return rc


# --- inside the jail: the holder -------------------------------------------------


def holder_main(argv: Sequence[str], *, ops: Ops = OS) -> NoReturn:
    """The holder: ``argv`` is ``-- COMMAND...``. Runs inside ``unshare -rn``,
    in the user+net namespace it owns."""
    raise SystemExit(_hold(argv, ops))


def _hold(argv: Sequence[str], ops: Ops) -> int:
    if len(argv) < 2 or argv[0] != "--":
        _say(f"claude-sandbox: usage: {HOLDER_ENTRY} -- COMMAND...")
        return 2
    command = argv[1:]
    env = dict(os.environ)
    # Undo what the interpreter changed at start-up: it ignores SIGPIPE and
    # SIGXFSZ and traps SIGINT (unless SIGINT was ignored on entry), and
    # exec keeps an ignored signal ignored.
    for sig in (signal.SIGPIPE, signal.SIGXFSZ):
        signal.signal(sig, signal.SIG_DFL)
    if signal.getsignal(signal.SIGINT) != signal.SIG_IGN:
        signal.signal(signal.SIGINT, signal.SIG_DFL)
    try:
        lock_routes(env, ops)
    except JailError as e:
        _say(f"claude-sandbox: egress jail {e}")
        return 1
    if not env.get(JAIL_LOCAL_PORTS, "") + env.get(JAIL_CALLBACK_PORTS, ""):
        try:
            ops.execve(command, env)
        except OSError as e:
            _say(f"claude-sandbox: {command[0]}: {e.strerror}")
            return 126 if isinstance(e, PermissionError) else 127
    return _hold_with_relays(command, env, ops)


def route_field(key: str, route: str) -> str:
    """The token after ``key`` in an ``ip route`` line (``via`` gives the
    gateway, ``dev`` the NIC), or ""."""
    tokens = route.split()
    for i, token in enumerate(tokens[:-1]):
        if token == key:
            return tokens[i + 1]
    return ""


# Route types `ip route` prints before the destination; anything else is a
# unicast route.
ROUTE_TYPES = frozenset(
    "unicast blackhole unreachable prohibit throw local broadcast multicast nat"
    " anycast".split()
)

# One route as the allowlist compares it: (type, destination, via, dev).
# proto, scope, src, metric and flags are not compared.
Route = tuple[str, str, str, str]

# The kernel's own policy rules, and nothing else may pick a table.
DEFAULT_RULES = (
    "0: from all lookup local",
    "32766: from all lookup main",
    "32767: from all lookup default",
)
IPV6_SYSCTLS = "/proc/sys/net/ipv6"
IPV6_OFF = (
    "/proc/sys/net/ipv6/conf/all/disable_ipv6",
    "/proc/sys/net/ipv6/conf/default/disable_ipv6",
)


def route_prefix(dst: str) -> str:
    """A destination as a prefix: ``default`` is 0.0.0.0/0, a bare
    address /32."""
    if dst == "default":
        return "0.0.0.0/0"
    return dst if "/" in dst else f"{dst}/32"


def as_route(line: str) -> Route:
    """A non-blank ``ip route`` line, or the arguments of ``ip route
    replace``, as the allowlist compares it."""
    tokens = line.split()
    kind = tokens.pop(0) if tokens[0] in ROUTE_TYPES else "unicast"
    dst = route_prefix(tokens[0]) if tokens else ""
    return (kind, dst, route_field("via", line), route_field("dev", line))


def parse_route(line: str) -> tuple[Route, str] | None:
    """An ``ip -o route show table all`` line as (route, table), or None for
    a blank one. The main table is not named in the output."""
    if not line.split():
        return None
    return as_route(line), route_field("table", line) or "main"


def own_addresses(addrs: str) -> set[str]:
    """The addresses, broadcast addresses and network addresses in
    ``ip -4 -o addr show``. Kernels up to 5.13 (RHEL 8, Ubuntu 20.04,
    Debian 11) also put a ``broadcast`` route for the network address of
    each /30 or shorter in the local table."""
    found: set[str] = set()
    for line in addrs.split("\n"):
        if inet := route_field("inet", line):
            found.add(inet.partition("/")[0])
            try:
                net = ipaddress.IPv4Interface(inet).network
            except ValueError:
                continue
            found.add(str(net.network_address))
        if brd := route_field("brd", line):
            found.add(brd)
    return found


def _kernel_local(route: Route, own: set[str]) -> bool:
    kind, dst, _, _ = route
    if kind not in ("local", "broadcast"):
        return False
    try:
        net = ipaddress.IPv4Network(dst, strict=False)
    except ValueError:
        return False
    return net.subnet_of(ipaddress.IPv4Network("127.0.0.0/8")) or (
        net.prefixlen == 32 and str(net.network_address) in own
    )


def check_routes(table: str, rules: str, addrs: str, allowed: set[Route]) -> None:
    """Refuse unless the main table holds exactly the routes the holder set
    (``allowed``), the local table only the kernel's routes for this
    namespace's own addresses and loopback, no other table holds any, and
    the policy rules are the kernel's three. A route left over, in any
    table, or a rule that picks another table, would route around the
    allowlist. A multipath route (``nexthop``) is refused outright: the
    allowlist compares one gateway and one device per route, and a second
    default or next hop must not hide behind the first."""
    own = own_addresses(addrs)
    seen: set[Route] = set()
    for line in table.split("\n"):
        words = line.split()
        if "nexthop" in words or words.count("via") > 1 or words.count("dev") > 1:
            raise JailError(
                f"— multipath route in the jail: {' '.join(words)} (fail-closed)"
            )
        parsed = parse_route(line)
        if parsed is None:
            continue
        route, name = parsed
        if name == "local" and _kernel_local(route, own):
            continue
        if name != "main" or route not in allowed or route in seen:
            # A second copy of an allowed route (a default with another
            # metric) is refused too: the holder sets each route once.
            raise JailError(
                f"— unexpected route in the jail: {line.strip()} (fail-closed)"
            )
        seen.add(route)
    if missing := sorted(allowed - seen):
        shown = " ".join(word for word in missing[0] if word)
        raise JailError(f"— route missing from the jail: {shown} (fail-closed)")
    got = [" ".join(line.split()) for line in rules.split("\n") if line.split()]
    if sorted(got) != sorted(DEFAULT_RULES):
        extra = next((r for r in got if r not in DEFAULT_RULES), "a default missing")
        raise JailError(
            f"— unexpected routing rules in the jail: {extra} (fail-closed)"
        )


def check_ipv6(addrs: str, routes: str) -> None:
    """With IPv6 still on (the sysctl could not be written): refuse a global
    address, or any route but link-local, multicast and the kernel's own."""
    for line in addrs.split("\n"):
        inet6 = route_field("inet6", line)
        if inet6 and not inet6.startswith(("fe80:", "::1/")):
            raise JailError(f"— IPv6 address in the jail: {inet6} (fail-closed)")
    for line in routes.split("\n"):
        tokens = line.split()
        if not tokens or route_field("table", line) == "local":
            continue
        dst = tokens[1] if tokens[0] in ROUTE_TYPES else tokens[0]
        if not dst.startswith(("fe80::/", "ff00::/", "multicast")):
            raise JailError(f"— IPv6 route in the jail: {line.strip()} (fail-closed)")


def _switch_off(path: str) -> bool:
    """Write 1 to the sysctl at ``path``; False when it cannot be written."""
    try:
        with open(path, "w") as f:
            f.write("1\n")
    except OSError:
        return False
    return True


def lock_routes(env: Mapping[str, str], ops: Ops) -> None:
    """Bring up loopback, wait for pasta, and lock the routing allowlist.

    pasta --config-net mirrors the host's L3 config into the netns, once, at
    attach: the address, the connected subnets, the gateway, and every
    other route the host has, a DHCP host route to a cloud's metadata
    service or a VPN's internal subnets included. Any of those, more
    specific than the blackholes, would beat them. So the main table is
    FLUSHED (only it: the local table holds loopback and the namespace's own
    addresses, which the relays and the DNS forwarder use) and rebuilt as
    exactly the allowlist: blackhole RFC1918, CGNAT and every connected
    subnet, mark link-local unreachable and blackhole Azure's WireServer,
    then punch back only the gateway, the DNS forwarder and the allow-ip
    devices, each with the interface's address as the source. Then every
    table and the policy rules are read back, and anything else refuses the
    launch. IPv6 is read back too: it may hold only link-local, multicast
    and the kernel's own routes, with no global address. That check is
    what holds in a container, where /proc/sys is read-only; writing the
    namespace's disable_ipv6 sysctls, where it can, is a bonus, and a
    kernel booted without IPv6 has nothing to check.

    The allowlist compares one gateway and one device per route, so a
    multipath route, or a second default, is refused rather than compared.

    The gateway's /32 is pinned on-link before the default route through it
    (which needs it): with the kernel's connected route flushed, that /32 is
    what reaches the gateway, and pasta answers its ARP.

    Raises JailError at any load-bearing failure, before the agent starts.
    """

    ip_tool = need("ip", "(iproute2)")
    allowed: set[Route] = set()

    def ip(*args: str, quiet: bool = False) -> tuple[int, str]:
        return ops.run([ip_tool, *args], env, quiet=quiet)

    def punch(*args: str, quiet: bool = False) -> bool:
        """``ip route replace ARGS``; once it has taken, the route is one
        the read-back expects."""
        if ip("route", "replace", *args, quiet=quiet)[0] != 0:
            return False
        allowed.add(as_route(" ".join(args)))
        return True

    def must(message: str, *args: str) -> None:
        if not punch(*args):
            raise JailError(f"— {message} (fail-closed)")

    if ip("link", "set", "lo", "up")[0] != 0:
        raise JailError("— could not bring up loopback")
    ready = env.get(JAIL_READY, "")
    if not wait_for(lambda: bool(ready) and os.path.isfile(ready), ops):
        raise JailError("— pasta never signalled ready")
    routes = ip("route", "show", "default")[1]
    if not routes.strip():
        raise JailError("— no default route after pasta attach")
    default = routes.split("\n")[0]
    gw, nic = route_field("via", default), route_field("dev", default)
    if not gw or not nic:
        raise JailError("— no default route via/dev after pasta attach")
    addr = ip("-4", "-o", "addr", "show", "dev", nic, "scope", "global", quiet=True)[1]
    src = route_field("inet", addr).partition("/")[0]
    if not src:
        raise JailError(f"— no IPv4 address on {nic} after pasta attach")
    # EVERY connected subnet on the egress NIC: blackholed, since the
    # gateway is all of it the jail may reach. Not the gateway's own /32 (a
    # DHCP route on Azure), which is pinned below.
    linked = ip("-o", "route", "show", "dev", nic, "scope", "link", quiet=True)[1]
    subnets = [route_prefix(ln.split()[0]) for ln in linked.split("\n") if ln.split()]
    subnets = [net for net in dict.fromkeys(subnets) if net != f"{gw}/32"]

    if ip("-4", "route", "flush", "table", "main")[0] != 0:
        raise JailError("— failed to flush the mirrored routes (fail-closed)")
    for subnet in subnets:
        must(f"failed to blackhole connected subnet {subnet}", "blackhole", subnet)
    for net in BLACKHOLES:
        must(
            f"failed to blackhole {net}{_BLACKHOLE_NOTES.get(net, '')}",
            "blackhole",
            net,
        )
    must(f"failed to mark {LINK_LOCAL} unreachable", "unreachable", LINK_LOCAL)
    must(
        f"failed to blackhole {WIRESERVER} (Azure WireServer)", "blackhole", WIRESERVER
    )
    must(f"failed to pin gateway {gw} on-link", f"{gw}/32", "dev", nic, "src", src)
    must(
        f"failed to restore default via {gw}",
        *("default", "via", gw, "dev", nic, "src", src),
    )

    # The forwarder and allow-ip /32s matter only for destinations inside
    # the blackholed ranges; anywhere else the default route already
    # reaches them. Losing one loses reachability, not containment.
    # DNS goes only to the pasta forwarder.
    via = ("via", gw, "dev", nic, "src", src)
    if not punch(f"{JAIL_DNS_FWD}/32", *via, quiet=True):
        _say(
            "claude-sandbox: egress jail — could not route DNS forwarder"
            f" {JAIL_DNS_FWD}"
        )
    # allow-ip devices (EPICS IOC, PMAC). Fail soft: the blackhole holds.
    # One address each: a prefix is narrowed to its first address, with a
    # warning, rather than refused, since existing confs carry them.
    for aip in lines(env.get(ALLOW_IP, "")):
        host, _, length = aip.partition("/")
        if length and length != "32":
            _say(
                f"claude-sandbox: egress jail — allow-ip {aip}: only {host} is"
                " routed (allow-ip takes single addresses)"
            )
        if not punch(f"{host}/32", *via, quiet=True):
            _say(f"claude-sandbox: egress jail — could not route allow-ip {aip}")

    # IPv6: pasta runs IPv4-only. Switch it off where /proc/sys can be
    # written (not in a container, usually); else the read-back below must
    # find nothing. No /proc/sys/net/ipv6 at all: a kernel booted with
    # ipv6.disable=1, where `ip -6` fails and there is nothing to check.
    v6_off = not os.path.exists(IPV6_SYSCTLS) or all(
        [_switch_off(path) for path in IPV6_OFF]
    )

    reads = [
        ip("-4", "-o", "route", "show", "table", "all", quiet=True),
        ip("-4", "rule", "show", quiet=True),
        ip("-4", "-o", "addr", "show", quiet=True),
        ip("route", "get", "1.1.1.1", quiet=True),
        ip("-6", "-o", "addr", "show", quiet=True),
        ip("-6", "-o", "route", "show", "table", "all", quiet=True),
    ]
    if any(rc != 0 for rc, _ in reads[:4]):
        raise JailError("— could not read back the routes (fail-closed)")
    (_, table), (_, rules), (_, addrs), (_, egress) = reads[:4]
    check_routes(table, rules, addrs, allowed)
    if route_field("src", egress) != src:
        raise JailError(f"— egress does not leave from {src} (fail-closed)")
    if not v6_off:
        if any(rc != 0 for rc, _ in reads[4:]):
            raise JailError("— could not read back the IPv6 state (fail-closed)")
        check_ipv6(reads[4][1], reads[5][1])


def _hold_with_relays(command: Sequence[str], env: Mapping[str, str], ops: Ops) -> int:
    """Inner relay ends, started in the holder's netns before bwrap masks /tmp.

    Outbound ports LISTEN on the agent's 127.0.0.1 and connect to the outer
    end's socket. Callback ports listen on their socket and connect to the
    agent's loopback per connection, so an agent not mid-login costs
    nothing and the browser is refused, not hung. Then COMMAND runs as a
    child, on the terminal, and its status is the holder's.
    """
    signals = Signals()
    relays = Relays(env, ops, signals)
    child: Proc | None = None
    relay_dir = env.get(JAIL_RELAY_DIR, "")
    try:
        socat = need("socat", "for loopback relays")
        ss = need("ss", "(iproute2) for loopback relays")
        for port in env.get(JAIL_LOCAL_PORTS, "").split():
            argv = tcp_to_unix(socat, port, f"{relay_dir}/{port}.sock")
            on_loopback = partial(relays.listening, ss, port, loopback=True)
            if not relays.start(argv, on_loopback):
                raise JailError(f"— loopback listener for port {port} failed to start")
        for port in env.get(JAIL_CALLBACK_PORTS, "").split():
            sock = f"{relay_dir}/in-{port}.sock"
            if not relays.start(
                unix_to_tcp(socat, sock, port), partial(is_socket, sock)
            ):
                raise JailError(f"— callback relay for port {port} failed to start")
        try:
            child = ops.spawn(command, env)
        except OSError as e:
            _say(f"claude-sandbox: {command[0]}: {e.strerror}")
            return 126 if isinstance(e, PermissionError) else 127
        return wait_child(child, ops, signals)
    except JailError as e:
        _say(f"claude-sandbox: egress jail {e}")
        return 1
    except Interrupted as e:
        return e.status
    finally:
        relays.stop()
        stop_child(child, ops)
