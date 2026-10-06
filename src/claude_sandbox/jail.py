"""The egress jail (ADR 0015, Design D) and its loopback relays (ADRs 20, 21).

Ported from ``netns_launch``, ``netns_holder``, ``jail_stage_dns`` and the
relay helpers in ``.devcontainer/claude-sandbox/claude-shadow``. The launch
runs inside a user+net namespace that a *holder* process owns, bridged to the
internet by pasta, with a routing allowlist the agent cannot change:

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

Deliberate differences from the bash:

- Readiness: the bash attaches pasta once ``/proc/<holder>/ns/net`` exists,
  which is already true before ``unshare`` has made the namespace. Here
  launch waits until the holder's netns differs from its own and its uid
  and gid maps are written.
- Signal dispositions: the bash starts the holder as a background job of a
  non-interactive shell, so the holder and the agent inherit SIGINT and
  SIGQUIT ignored. Here they keep the defaults, as on the unjailed path, so
  ^C reaches the child's own terminal as it does there.
- Signal handlers only record the signal (see ``Signals``), so cleanup
  always runs whole; a second signal does not cut it short. So a second ^C
  during a cleanup that hangs is recorded, not acted on: only SIGKILL
  stops it (the bash exits on it).
- Relays get their own session from ``start_new_session``, not ``setsid``.
- The staged resolv.conf is removed after every failure, including a
  missing tool, and is always staged under /tmp (see ``stage_dns``).
- A holder that cannot bring up loopback or exec the command says so.
- Tools come from the fixed tool path, never PATH, and a missing ``ss`` or
  ``ip`` is refused up front (the bash finds them on PATH).

Every side effect goes through ``Ops`` so tests can replace it. Standard
library only: this module is on the launch path (ADR 26).
"""

import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from functools import partial
from types import FrameType
from typing import NoReturn, Protocol

from .config import (
    ALLOW_IP,
    Config,
    callback_ports,
    lines,
    local_ports,
    validate_callback_ports,
    validate_local_model_port,
)
from .errors import SandboxError
from .tools import find_tool

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

# What launch hands the holder through its environment, as the bash does.
JAIL_READY = "CLAUDE_JAIL_READY"
JAIL_RELAY_DIR = "CLAUDE_JAIL_RELAY_DIR"
JAIL_LOCAL_PORTS = "CLAUDE_JAIL_LOCAL_PORTS"
JAIL_CALLBACK_PORTS = "CLAUDE_JAIL_CALLBACK_PORTS"

# pasta's log. A fixed path, as in the bash and the container workflow's
# diagnostics.
PASTA_LOG = "/tmp/claude-pasta.log"

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
# cannot be pivoted to), then link-local marked unreachable.
BLACKHOLES = ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10")
_BLACKHOLE_NOTES = {"100.64.0.0/10": " CGNAT"}
LINK_LOCAL = "169.254.0.0/16"

# wait_for: up to ~10s, 200 polls 50ms apart.
WAIT_TRIES = 200
WAIT_STEP = 0.05

# The exit status after a signal, as the bash traps set it.
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


Handler = Callable[[int, FrameType | None], None]


class Ops:
    """Every side effect the jail has, so tests can replace each one."""

    executable = sys.executable

    def find_tool(self, name: str) -> str | None:
        """The tool's absolute path on the fixed tool path, never PATH."""
        return find_tool(name)

    def exists(self, path: str) -> bool:
        return os.path.exists(path)

    def is_file(self, path: str) -> bool:
        return os.path.isfile(path)

    def is_socket(self, path: str) -> bool:
        try:
            return (os.stat(path).st_mode & 0o170000) == 0o140000
        except OSError:
            return False

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
        try:
            if group:
                os.killpg(pid, sig)
            else:
                os.kill(pid, sig)
        except OSError:
            pass

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)

    def mkdtemp(self) -> str:
        # Always under /tmp, which bwrap masks. Never TMPDIR: it may sit in
        # the writable workspace, and the agent must not reach the relay
        # sockets or the readiness handshake.
        return tempfile.mkdtemp(prefix="claude-jail.", dir="/tmp")

    def mkdir(self, path: str) -> None:
        os.mkdir(path, 0o700)
        os.chmod(path, 0o700)

    def touch(self, path: str) -> None:
        with open(path, "a"):
            pass

    def rmtree(self, path: str) -> None:
        shutil.rmtree(path, ignore_errors=True)

    def remove(self, path: str) -> None:
        try:
            os.remove(path)
        except OSError:
            pass

    def signal(self, sig: int, handler: Handler | int) -> None:
        signal.signal(sig, handler)

    def ignored(self, sig: int) -> bool:
        return signal.getsignal(sig) == signal.SIG_IGN

    def environ(self) -> dict[str, str]:
        return dict(os.environ)

    def stderr(self, message: str) -> None:
        print(message, file=sys.stderr, flush=True)


OS = Ops()


def status(returncode: int) -> int:
    """A child's exit status as bash's ``wait`` reports it: 128+N after
    signal N."""
    return 128 - returncode if returncode < 0 else returncode


class Signals:
    """INT exits 130, TERM and HUP exit 143, as the bash traps do.

    The handler only records the first signal; ``check`` raises it at the
    points where stopping is safe. Raising from the handler itself could
    land between a fork and the record of the child's pid, leaving a
    holder or relay nobody kills, or in the middle of cleanup.
    """

    def __init__(self, ops: Ops) -> None:
        self.status: int | None = None
        # A signal ignored on entry stays ignored, as bash cannot trap it.
        for sig in SIGNAL_STATUS:
            if not ops.ignored(sig):
                ops.signal(sig, self._record)

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
    return status(rc)


def stop_relays(relays: list[Proc], ops: Ops) -> None:
    """TERM each relay's process group, then reap it."""
    while relays:
        proc = relays.pop()
        ops.kill(proc.pid, signal.SIGTERM, group=True)
        proc.wait()


def stop_child(proc: Proc | None, ops: Ops) -> None:
    if proc is not None and proc.poll() is None:
        ops.kill(proc.pid, signal.SIGTERM)
        proc.wait()


def start_relay(
    argv: Sequence[str], env: Mapping[str, str], ops: Ops, relays: list[Proc]
) -> bool:
    """Start a socat relay and record it for ``stop_relays``."""
    try:
        relays.append(ops.spawn(argv, env, relay=True))
    except OSError:
        return False
    return True


def need(ops: Ops, name: str, why: str) -> str:
    """The absolute path of tool ``name``, or a fail-closed refusal."""
    path = ops.find_tool(name)
    if path is None:
        raise JailError(f"needs {name} {why}")
    return path


def _ss(ss: str, port: str, env: Mapping[str, str], ops: Ops) -> str:
    return ops.run([ss, "-H", "-ltn", f"sport = :{port}"], env)[1]


def port_in_use(ss: str, port: str, env: Mapping[str, str], ops: Ops) -> bool:
    """Something listens on TCP ``port`` (in this netns)."""
    return bool(_ss(ss, port, env, ops))


def loopback_listening(ss: str, port: str, env: Mapping[str, str], ops: Ops) -> bool:
    """A listener on 127.0.0.1:``port``. Reads the kernel's listener table:
    it never connects to the service, which need not be running."""
    return "127.0.0.1:" in _ss(ss, port, env, ops)


# --- DNS ---------------------------------------------------------------------


@dataclass(frozen=True)
class StagedDns:
    """What ``stage_dns`` did: the file to bind (None when it could not
    stage one) and the warnings for the shadow's ``launch_warn``."""

    path: str | None
    warnings: tuple[str, ...] = ()


_KEEP = re.compile(rb"[ \t\n\v\f\r]*(search|domain|options)[ \t\n\v\f\r]")
_NAMESERVER = re.compile(rb"[ \t\n\v\f\r]*nameserver")


def stage_dns(*, resolv_conf: str = RESOLV_CONF, tmpdir: str = "/tmp") -> StagedDns:
    """Stage a resolv.conf that sends every query to the pasta forwarder.

    The host's ``search``, ``domain`` and ``options`` lines are kept:
    dropping them breaks short-name resolution at sites with a search list.
    Its real resolvers are dropped: a route to one would open that internal
    host on every port (issue #11). Written under /tmp, which bwrap masks,
    never ``$TMPDIR``, which may sit in the writable workspace (the bash
    honours it). Raises SandboxError if the file cannot be written.
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
    """What ``launch`` must undo, in the order the bash undoes it."""

    relays: list[Proc] = field(default_factory=list[Proc])
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
    signals = Signals(ops)
    state = _Outer()
    try:
        return _start(config, env, command, ops, signals, state)
    except JailError as e:
        ops.stderr(f"claude-sandbox: egress jail {e}")
        return 1
    except Interrupted as e:
        return e.status
    finally:
        # Signals only set a flag now, so nothing interrupts this.
        stop_relays(state.relays, ops)
        stop_child(state.holder, ops)
        if state.jail_dir is not None:
            ops.rmtree(state.jail_dir)
        resolv = env.get(JAIL_RESOLV, "")
        # Only a file stage_dns made: never delete what a caller named.
        if os.path.basename(resolv).startswith(_RESOLV_PREFIX):
            ops.remove(resolv)


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
    unshare = need(ops, "unshare", "(util-linux)")
    pasta = need(ops, "pasta", "(apt-get install passt)")
    if not ops.exists("/dev/net/tun"):
        raise JailError(
            "needs /dev/net/tun — add --device=/dev/net/tun to the container"
        )
    # The holder must be this interpreter, by absolute path, never PATH's,
    # and the holder runs the command as given, never searching PATH.
    if not os.path.isabs(ops.executable):
        raise JailError("— cannot locate the sandbox's own Python interpreter")
    if not command or not os.path.isabs(command[0]):
        raise JailError("— the command to run must be an absolute path")

    # Every port is checked before anything starts.
    errors = validate_local_model_port(config) + validate_callback_ports(config)
    if errors:
        for error in errors:
            ops.stderr(error)
        return 1
    outbound = local_ports(config)
    inbound = callback_ports(config)

    try:
        state.jail_dir = ops.mkdtemp()
    except OSError as e:
        raise JailError(f"— cannot create its directory under /tmp: {e}") from None
    holder_env = dict(env)
    for name in (JAIL_LOCAL_PORTS, JAIL_CALLBACK_PORTS, JAIL_RELAY_DIR, ALLOW_IP):
        holder_env.pop(name, None)
    holder_env[JAIL_READY] = f"{state.jail_dir}/ready"
    if config.allow_ip:
        holder_env[ALLOW_IP] = config.allow_ip

    if outbound or inbound:
        socat = need(ops, "socat", "for loopback relays")
        relay_dir = f"{state.jail_dir}/relay"
        ops.mkdir(relay_dir)
        holder_env[JAIL_RELAY_DIR] = relay_dir
        if outbound:
            _outbound_relays(socat, relay_dir, outbound, env, ops, signals, state)
            holder_env[JAIL_LOCAL_PORTS] = "".join(f"{p} " for p in outbound)
        if inbound:
            ss = need(ops, "ss", "(iproute2) for loopback relays")
            started = _callback_relays(
                socat, ss, relay_dir, inbound, env, ops, signals, state
            )
            holder_env[JAIL_CALLBACK_PORTS] = "".join(f"{p} " for p in started)

    # The holder inherits stdin and the process group: it, and the script(1)
    # and bwrap it runs, stay in the terminal's foreground group, so they
    # read the terminal without SIGTTIN or SIGTTOU.
    try:
        holder = state.holder = ops.spawn(
            [unshare, "-rn", ops.executable, "-I", "-m", "claude_sandbox",
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
                f"— holder exited with status {status(rc)} before pasta could attach"
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
        raise JailError(f"— pasta failed to attach to the netns (see {PASTA_LOG})")
    signals.check()
    ops.touch(holder_env[JAIL_READY])

    rc = wait_child(holder, ops, signals)
    state.holder = None
    return rc


def _outbound_relays(
    socat: str,
    relay_dir: str,
    ports: list[str],
    env: Mapping[str, str],
    ops: Ops,
    signals: Signals,
    state: _Outer,
) -> None:
    """Outer ends of the local-port relays (ADR 20): one private Unix socket
    per port, connected to the outer 127.0.0.1. No IP route, no mapping of
    the host's whole loopback. Fatal if one does not start."""
    for port in ports:
        sock = f"{relay_dir}/{port}.sock"
        argv = [socat, f"UNIX-LISTEN:{sock},mode=0600,fork", f"TCP4:127.0.0.1:{port}"]
        if not start_relay(argv, env, ops, state.relays) or not wait_for(
            partial(ops.is_socket, sock), ops, signals
        ):
            raise JailError(f"— loopback relay for port {port} failed to start")


def _callback_relays(
    socat: str,
    ss: str,
    relay_dir: str,
    ports: list[str],
    env: Mapping[str, str],
    ops: Ops,
    signals: Signals,
    state: _Outer,
) -> list[str]:
    """Outer ends of the callback relays (ADR 21), which LISTEN on the outer
    127.0.0.1 and so can collide with a second session or an unwrapped
    agent. Fail soft: warn and carry on. Returns the ports that started, the
    only ones the holder should wait on."""
    started: list[str] = []
    for port in ports:
        if port_in_use(ss, port, env, ops):
            ops.stderr(
                f"claude-sandbox: callback-port {port} is already in use on this"
                " host; browser logins on that port will not reach this session."
            )
            continue
        argv = [
            socat,
            f"TCP4-LISTEN:{port},bind=127.0.0.1,reuseaddr,fork",
            f"UNIX-CONNECT:{relay_dir}/in-{port}.sock",
        ]
        if start_relay(argv, env, ops, state.relays) and wait_for(
            partial(port_in_use, ss, port, env, ops), ops, signals
        ):
            started.append(port)
        else:
            ops.stderr(
                f"claude-sandbox: callback-port {port} relay failed to start;"
                " browser logins on that port will not reach this session."
            )
    return started


# --- inside the jail: the holder -------------------------------------------------


def holder_main(argv: Sequence[str], *, ops: Ops = OS) -> NoReturn:
    """The holder: ``argv`` is ``-- COMMAND...``. Runs inside ``unshare -rn``,
    in the user+net namespace it owns."""
    raise SystemExit(_hold(argv, ops))


def _hold(argv: Sequence[str], ops: Ops) -> int:
    if len(argv) < 2 or argv[0] != "--":
        ops.stderr(f"claude-sandbox: usage: {HOLDER_ENTRY} -- COMMAND...")
        return 2
    command = argv[1:]
    env = ops.environ()
    # Undo what the interpreter changed at start-up: it ignores SIGPIPE and
    # SIGXFSZ and traps SIGINT (unless SIGINT was ignored on entry), and
    # exec keeps an ignored signal ignored.
    for sig in (signal.SIGPIPE, signal.SIGXFSZ):
        ops.signal(sig, signal.SIG_DFL)
    if not ops.ignored(signal.SIGINT):
        ops.signal(signal.SIGINT, signal.SIG_DFL)
    try:
        lock_routes(env, ops)
    except JailError as e:
        ops.stderr(f"claude-sandbox: egress jail {e}")
        return 1
    if not env.get(JAIL_LOCAL_PORTS, "") + env.get(JAIL_CALLBACK_PORTS, ""):
        try:
            ops.execve(command, env)
        except OSError as e:
            ops.stderr(f"claude-sandbox: {command[0]}: {e.strerror}")
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


def lock_routes(env: Mapping[str, str], ops: Ops) -> None:
    """Bring up loopback, wait for pasta, and lock the routing allowlist.

    pasta --config-net mirrors the host's L3 config into the netns (address,
    connected subnets, gateway). The allowlist is SURGICAL: blackhole RFC1918,
    CGNAT and every connected subnet, mark link-local unreachable, then punch
    back only the gateway, the DNS forwarder and the allow-ip devices. A
    blanket blackhole would cut the gateway on an all-RFC1918 site, and a
    connected-subnet route left alone (more specific than the blackholes)
    would leave the whole local subnet reachable.

    Raises JailError at any load-bearing failure, before the agent starts.
    """

    ip_tool = need(ops, "ip", "(iproute2)")

    def ip(*args: str, quiet: bool = False) -> tuple[int, str]:
        return ops.run([ip_tool, *args], env, quiet=quiet)

    def must(message: str, *args: str) -> None:
        if ip("route", "replace", *args)[0] != 0:
            raise JailError(f"— {message} (fail-closed)")

    if ip("link", "set", "lo", "up")[0] != 0:
        raise JailError("— could not bring up loopback")
    ready = env.get(JAIL_READY, "")
    if not wait_for(lambda: bool(ready) and ops.is_file(ready), ops):
        raise JailError("— pasta never signalled ready")
    routes = ip("route", "show", "default")[1]
    if not routes.strip():
        raise JailError("— no default route after pasta attach")
    default = routes.split("\n")[0]
    gw, nic = route_field("via", default), route_field("dev", default)
    if not gw or not nic:
        raise JailError("— no default route via/dev after pasta attach")
    # EVERY connected subnet on the egress NIC: each is more specific than
    # the blackholes below, so one left alone stays reachable.
    linked = ip("-o", "route", "show", "dev", nic, "scope", "link", quiet=True)[1]
    subnets = [line.split()[0] for line in linked.split("\n") if line.split()]

    # Blackhole first, then punch back. A mirrored scope-link route can be the
    # gateway's own /32 (DHCP on Azure); pinning the gateway before this would
    # let the subnet blackhole overwrite it.
    for subnet in subnets:
        must(f"failed to blackhole connected subnet {subnet}", "blackhole", subnet)
    for net in BLACKHOLES:
        note = _BLACKHOLE_NOTES.get(net, "")
        must(f"failed to blackhole {net}{note}", "blackhole", net)
    must(f"failed to mark {LINK_LOCAL} unreachable", "unreachable", LINK_LOCAL)
    must(f"failed to pin gateway {gw} on-link", f"{gw}/32", "dev", nic)
    must(f"failed to restore default via {gw}", "default", "via", gw, "dev", nic)

    # DNS goes only to the pasta forwarder. Losing this loses DNS, not
    # containment.
    if ip("route", "replace", f"{JAIL_DNS_FWD}/32", "via", gw, quiet=True)[0] != 0:
        ops.stderr(
            "claude-sandbox: egress jail — could not route DNS forwarder"
            f" {JAIL_DNS_FWD}"
        )
    # allow-ip devices (EPICS IOC, PMAC). Fail soft: the blackhole holds.
    for aip in lines(env.get(ALLOW_IP, "")):
        host = aip.rpartition("/")[0] if "/" in aip else aip
        if ip("route", "replace", f"{host}/32", "via", gw, quiet=True)[0] != 0:
            ops.stderr(f"claude-sandbox: egress jail — could not route allow-ip {aip}")


def _hold_with_relays(command: Sequence[str], env: Mapping[str, str], ops: Ops) -> int:
    """Inner relay ends, started in the holder's netns before bwrap masks /tmp.

    Outbound ports LISTEN on the agent's 127.0.0.1 and connect to the outer
    end's socket. Callback ports listen on their socket and connect to the
    agent's loopback per connection, so an agent not mid-login costs
    nothing and the browser is refused, not hung. Then COMMAND runs as a
    child, on the terminal, and its status is the holder's.
    """
    signals = Signals(ops)
    relays: list[Proc] = []
    child: Proc | None = None
    relay_dir = env.get(JAIL_RELAY_DIR, "")
    try:
        socat = need(ops, "socat", "for loopback relays")
        ss = need(ops, "ss", "(iproute2) for loopback relays")
        for port in env.get(JAIL_LOCAL_PORTS, "").split():
            argv = [
                socat,
                f"TCP4-LISTEN:{port},bind=127.0.0.1,reuseaddr,fork",
                f"UNIX-CONNECT:{relay_dir}/{port}.sock",
            ]
            if not start_relay(argv, env, ops, relays) or not wait_for(
                partial(loopback_listening, ss, port, env, ops), ops, signals
            ):
                raise JailError(f"— loopback listener for port {port} failed to start")
        for port in env.get(JAIL_CALLBACK_PORTS, "").split():
            sock = f"{relay_dir}/in-{port}.sock"
            argv = [
                socat,
                f"UNIX-LISTEN:{sock},mode=0600,fork",
                f"TCP4:127.0.0.1:{port}",
            ]
            if not start_relay(argv, env, ops, relays) or not wait_for(
                partial(ops.is_socket, sock), ops, signals
            ):
                raise JailError(f"— callback relay for port {port} failed to start")
        try:
            child = ops.spawn(command, env)
        except OSError as e:
            ops.stderr(f"claude-sandbox: {command[0]}: {e.strerror}")
            return 126 if isinstance(e, PermissionError) else 127
        return wait_child(child, ops, signals)
    except JailError as e:
        ops.stderr(f"claude-sandbox: egress jail {e}")
        return 1
    except Interrupted as e:
        return e.status
    finally:
        stop_relays(relays, ops)
        stop_child(child, ops)
