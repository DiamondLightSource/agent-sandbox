"""The shadow: what runs when a user types ``claude``, ``codex`` or ``pi``.

Ported from the launch body of ``.devcontainer/claude-sandbox/claude-shadow``
(everything after the function definitions, plus ``configure_launch``,
``sandbox_launch`` and their helpers). The three-line bash shim at
``/usr/local/bin/<agent>`` execs the root-owned interpreter with ``-I`` and
lands in ``main`` below (ADR 26). Read top to bottom: ``run`` is the order a
launch happens in, and each step is a function just below it.

This module adds no bind and no environment to the bwrap argv; ``bwrap.py``
builds all of it. The egress jail lives behind the seam in ``jail.py``.

Standard library only: this module is on the launch path (ADR 26).
"""

import glob
import os
import shlex
import signal
import subprocess
import sys
import tempfile
import termios
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from errno import ENOENT
from types import FrameType
from typing import NoReturn

from . import jail
from .bwrap import GITCONFIG_PATH, HOST, Probe, bwrap_argv
from .config import (
    CONFIG_PATH,
    Config,
    egress_jail_enabled,
    parse_config,
    resolve_workspace_root,
    valid_tcp_port,
)
from .errors import SandboxError
from .gitconfig import render_gitconfig
from .profiles import (
    PROFILES,
    SHARED_SKILLS_REL,
    SHIPPED_SKILLS_DIR,
    AgentProfile,
    agent_exec_argv,
    detect_agent,
    filter_chrome_args,
)
from .tools import TOOL_PATH, find_tool

# The shim, byte for byte as install.sh places it (ADR 26). The self-exec
# check compares the real binary against it; a test pins it to the file.
SHIM = (
    "#!/bin/bash\n"
    "# /usr/local/bin/claude (and codex, pi): hand off to the root-owned install.\n"
    "exec /usr/libexec/claude-sandbox/venv/bin/python -I -m claude_sandbox"
    ' _shadow "${0##*/}" -- "$@"\n'
)

ExecVE = Callable[[str, list[str], Mapping[str, str]], NoReturn]


def read_git_config(key: str, env: Mapping[str, str]) -> str:
    """``$(git config --get KEY 2>/dev/null || true)``: empty when unset.

    git comes from the fixed tool path (tools.py); without it the identity
    is empty.
    """
    git = find_tool("git")
    if git is None:
        return ""
    try:
        out = subprocess.run(
            [git, "config", "--get", key],
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
        ).stdout
    except OSError:
        return ""
    return os.fsdecode(out).rstrip("\n")


@dataclass(frozen=True)
class Host:
    """The paths the shadow reads and writes, and how it execs.

    The defaults are the installed locations. Tests pass fixtures; nothing
    in the environment can change these.
    """

    config_path: str = CONFIG_PATH
    gitconfig_path: str = GITCONFIG_PATH
    shipped_skills_dir: str = SHIPPED_SKILLS_DIR
    profiles: Mapping[str, AgentProfile] = field(default_factory=lambda: PROFILES)
    execve: ExecVE = os.execve
    probe: Probe = HOST
    git_config_get: Callable[[str, Mapping[str, str]], str] = read_git_config
    find_tool: Callable[[str], str | None] = find_tool
    mountinfo: str = "/proc/self/mountinfo"


INSTALLED = Host()


def main(argv0: str, args: list[str], host: Host = INSTALLED) -> NoReturn:
    """The shim's entry: launch, or exit with the bash shadow's status.

    INT, TERM and HUP unwind through ``finally`` (so nothing the shadow
    created is left behind), then the process dies by that signal, as the
    bash shadow's EXIT trap does. A signal ignored on entry stays ignored.
    """
    for signum in (signal.SIGTERM, signal.SIGHUP):
        if signal.getsignal(signum) == signal.SIG_DFL:
            signal.signal(signum, raise_signalled)
    try:
        run(argv0, args, original_environ(), host)
    except SandboxError as e:
        _refuse(str(e))
    except KeyboardInterrupt:
        die_by(signal.SIGINT)
    except Signalled as e:
        die_by(e.signum)


def run(
    argv0: str, args: Sequence[str], env: Mapping[str, str], host: Host = INSTALLED
) -> NoReturn:
    """One launch, in the bash shadow's order. Ends in an exec or an exit."""
    env = dict(env)
    profile = host.profiles[detect_agent(argv0, env.get("CLAUDE_SANDBOX_AGENT", ""))]
    args, verify = _sandbox_verify(args)
    if env.get("IS_SANDBOX") == "1":
        _nested_launch(profile, env, args, verify, host)

    term = Terminal()
    invoked = argv0.rpartition("/")[2]
    if not env.get("CLAUDE_SANDBOX_AGENT") and invoked not in host.profiles:
        term.warn(
            f"invoked as '{invoked}', which names no known agent"
            f" — assuming {profile.label}. Set CLAUDE_SANDBOX_AGENT to choose"
            " explicitly."
        )
    _check_real_binary(profile)

    # configure_launch: the conf (env wins over it), then the git identity.
    try:
        env = parse_config(host.config_path, env)
    except OSError as e:
        _refuse(f"claude-sandbox: {host.config_path}: {e.strerror}")
    config = Config.from_env(env)
    _check_local_model_port(config)
    write_gitconfig(host, env, no_forge=config.no_forge)
    if not verify:
        check_config_persistence(profile, env, term, host.mountinfo)
    prepare_home(profile, env, config, term, host.shipped_skills_dir)

    jailed = egress_jail_enabled(config)
    if jailed:
        env = jail.stage_dns(env, term.warn)
    argv = build_argv(profile, env, args, verify, host)
    terminal, launch_env = terminal_command(argv, env, host)
    term.pause(verify)
    if jailed:
        jail.launch(config, launch_env, terminal)
    _exec(host, terminal[0], terminal, launch_env)


# --- the steps, in order ----------------------------------------------------


def original_environ(path: str = "/proc/self/environ") -> dict[str, str]:
    """The environment the shim exec'd the interpreter with.

    Python coerces a C or POSIX locale (PEP 538) by setting LC_CTYPE in its
    own environment, even under ``-I``; the bwrap argv forwards LC_CTYPE, so
    ``os.environ`` would put a value in the jail that the bash shadow never
    did. ``/proc/self/environ`` holds the block the kernel was given.
    """
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except OSError:
        return dict(os.environ)
    env: dict[str, str] = {}
    for entry in raw.split(b"\0"):
        name, eq, value = entry.partition(b"=")
        if eq and name:
            env.setdefault(os.fsdecode(name), os.fsdecode(value))
    return env


def _sandbox_verify(args: Sequence[str]) -> tuple[list[str], bool]:
    """``--sandbox-verify``: run the battery in place of the agent.

    The helper CLI's ``verify`` runs it through the same profile and
    isolation, without starting a model or asking for input.
    """
    if not args or args[0] != "--sandbox-verify":
        return list(args), False
    if len(args) > 1:
        _refuse("claude-sandbox: --sandbox-verify takes no arguments", 2)
    return [], True


def _nested_launch(
    profile: AgentProfile,
    env: Mapping[str, str],
    args: Sequence[str],
    verify: bool,
    host: Host,
) -> NoReturn:
    """The recursion guard: already inside the sandbox, so don't nest bwrap.

    A hook or skill spawned the agent. Claude's real binary is bound at its
    conventional path in here, so exec that; the --chrome strip applies
    here too, so a nested spawn can't re-enable the browser-extension
    channel.
    """
    user = filter_chrome_args(args) if profile.filter_chrome else list(args)
    command = agent_exec_argv(profile, env.get("HOME") or "/root", verify=verify)
    _exec(host, command[0], [*command, *user], env)


def _check_real_binary(profile: AgentProfile) -> None:
    """Refuse a missing real binary, or one that is a copy of the shim.

    The shim is installed for every agent whether or not that agent's
    binary was fetched (it must own the name before the vendor's installer
    can claim it), so this is the message a user sees running an agent that
    was never installed. A copy of the shim as the real binary would exec
    itself forever through the recursion guard: turn the hang into an error.
    """
    if not os.access(profile.real, os.X_OK):
        _refuse(
            f"claude-sandbox: real {profile.label} binary missing at {profile.real}.\n"
            "  Re-run `./install` from a fresh clone of claude-sandbox\n"
            "  (it fetches and relocates every supported agent it can reach)."
        )
    shim = SHIM.encode()
    try:
        with open(profile.real, "rb") as f:
            is_shim = f.read(len(shim) + 1) == shim
    except OSError:
        is_shim = False
    if is_shim:
        _refuse(
            f"claude-sandbox: {profile.real} is a copy of this shadow, not the real"
            f" {profile.label} binary.\n"
            "  Launching it would loop forever. Re-run `./install` to relocate a"
            " real one;\n"
            "  if the download is failing, the sandbox says so at the end of install."
        )


def write_gitconfig(host: Host, env: Mapping[str, str], *, no_forge: bool) -> None:
    """(Re)write the jail's git config from the host's current git identity.

    Called on every launch because VS Code's dev.containers.copyGitConfig
    fires AFTER postCreate, so an install-time render can have an empty
    user.name. Written to a temporary file and renamed, so a reader never
    sees half a file; the temporary file goes on any exit.
    """
    text = render_gitconfig(
        host.git_config_get("user.name", env),
        host.git_config_get("user.email", env),
        no_forge=no_forge,
    )
    path = host.gitconfig_path
    directory, _, name = path.rpartition("/")
    try:
        fd, tmp = tempfile.mkstemp(prefix=f"{name}.", dir=directory or "/")
    except OSError as e:
        _refuse(f"claude-sandbox: cannot write {path}: {e.strerror}")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", errors="surrogateescape") as f:
            f.write(text)
            os.fchmod(f.fileno(), 0o644)
        os.replace(tmp, path)
    except OSError as e:
        _refuse(f"claude-sandbox: cannot write {path}: {e.strerror}")
    finally:
        if os.path.lexists(tmp):
            os.unlink(tmp)


def is_mountpoint(path: str, mountinfo: str = "/proc/self/mountinfo") -> bool:
    """``mountpoint -q PATH``: PATH, resolved, is a mount target."""
    try:
        target = os.path.realpath(path, strict=True)
        with open(mountinfo, "rb") as f:
            table = f.read()
    except OSError:
        return False
    for line in table.splitlines():
        fields = line.split(b" ")
        # Field 5 is the mount point, with space, tab, newline and backslash
        # written as octal escapes.
        if len(fields) > 4 and _unescape(fields[4]) == os.fsencode(target):
            return True
    return False


def _unescape(field: bytes) -> bytes:
    for code in (b"\\040", b"\\011", b"\\012", b"\\134"):
        field = field.replace(code, bytes([int(code[1:], 8)]))
    return field


def check_config_persistence(
    profile: AgentProfile, env: Mapping[str, str], term: "Terminal", mountinfo: str
) -> None:
    """Warn when the agent's config dir will not survive a rebuild.

    A symlink (link_terminal_config wired it to a host-mounted path) or a
    direct bind mount is fine; a plain container directory means memory,
    settings and login state are lost on the next devcontainer rebuild.
    """
    rel = profile.home_dirs[0]
    cfg_dir = f"{env.get('HOME') or '/root'}/{rel}"
    if os.path.islink(cfg_dir) or is_mountpoint(cfg_dir, mountinfo):
        return
    term.warn_raw(
        f"\n\033[33mWARNING\033[0m: ~/{rel} is not host-mounted.\n"
        f"{profile.label} memory, settings, and login state will be lost on"
        " devcontainer rebuild.\n"
        "See https://diamondlightsource.github.io/claude-sandbox/how-to/"
        "use-the-container-image.html#authentication-and-persistence\n\n"
    )


def prepare_home(
    profile: AgentProfile,
    env: Mapping[str, str],
    config: Config,
    term: "Terminal",
    shipped_skills_dir: str,
) -> None:
    """Create what the argv builder binds, so its --bind finds a source.

    The forge credential dirs (unless no-forge), the agent's config dirs and
    files (else the agent's OAuth token writes into the in-sandbox tmpfs and
    vanishes on exit), the agent's skills dir, and the shared skills dir.
    """
    home = env.get("HOME") or "/root"
    try:
        if not config.no_forge:
            os.makedirs(f"{home}/.config/gh", exist_ok=True)
            os.makedirs(f"{home}/.config/glab-cli", exist_ok=True)
        for rel in profile.home_dirs:
            os.makedirs(f"{home}/{rel}", exist_ok=True)
        for rel in profile.home_files:
            _touch(f"{home}/{rel}")
        prepare_shipped_skills(profile, home, term, shipped_skills_dir)
    except OSError as e:
        _refuse(f"claude-sandbox: cannot create {e.filename}: {e.strerror}")
    # A dangling symlink (a shared store that has gone away) makes this
    # fail; launch without the share rather than abort.
    try:
        os.makedirs(f"{home}/{SHARED_SKILLS_REL}", exist_ok=True)
    except OSError:
        term.warn(
            f"cannot create ~/{SHARED_SKILLS_REL}; shared skills are unavailable"
            " this session."
        )


def _touch(path: str) -> None:
    """``touch PATH``: create it, or update its times."""
    try:
        os.utime(path)
    except FileNotFoundError:
        os.close(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_NOCTTY, 0o666))


def prepare_shipped_skills(
    profile: AgentProfile, home: str, term: "Terminal", shipped_skills_dir: str
) -> None:
    """Create the agent's skills dir when anything ships; warn per mask.

    The shipped-skill binds land inside that dir, one per skill. Creating
    it here, the one deliberate host write, stops bwrap from making it on
    the host bind. bwrap still leaves an empty mount point per shipped skill
    on the host, and an empty dir masks nothing, so only a non-empty one, or
    a non-directory, warns.
    """
    skills_dir = f"{home}/{profile.skills_rel}"
    # `*/` matches directories and links to them, never dot-directories.
    for skill in sorted(glob.glob(glob.escape(shipped_skills_dir) + "/*/")):
        name = skill.removesuffix("/").rpartition("/")[2]
        os.makedirs(skills_dir, exist_ok=True)
        host_skill = f"{skills_dir}/{name}"
        if (
            os.path.islink(host_skill)
            or (os.path.exists(host_skill) and not os.path.isdir(host_skill))
            or _has_entries(host_skill)
        ):
            term.warn(
                f"~/{profile.skills_rel}/{name} is shadowed by the shipped skill"
                " for this session."
            )


def _has_entries(path: str) -> bool:
    try:
        with os.scandir(path) as it:
            return any(True for _ in it)
    except OSError:
        return False


def _check_local_model_port(config: Config) -> None:
    """Refuse a bad local-model-port before it reaches the argv.

    The argv forwards it into the jail with --setenv. The bash validates it
    only on the jailed path (netns_launch), so a jail-off launch forwarded
    any value; here it is checked on every launch (a known divergence).
    """
    port = config.local_model_port
    if port != "0" and not valid_tcp_port(port):
        _refuse("claude-sandbox: local-model-port must be 1–65535 (or 0 to disable).")


def build_argv(
    profile: AgentProfile,
    env: Mapping[str, str],
    args: Sequence[str],
    verify: bool,
    host: Host,
) -> list[str]:
    """The bwrap argv, with its Config read from the same ``env``."""
    pwd = working_directory(env)
    config = Config.from_env(env)
    return bwrap_argv(
        profile,
        config,
        env,
        resolve_workspace_root(config, pwd),
        profile.real,
        args,
        verify=verify,
        shipped_skills_dir=host.shipped_skills_dir,
        gitconfig_path=host.gitconfig_path,
        probe=host.probe,
    )


def working_directory(env: Mapping[str, str]) -> str:
    """bash's ``$PWD``: the inherited PWD when it names the cwd, else getcwd.

    The shim's bash has already canonicalised an inherited PWD, so a
    workspace reached through a symlink keeps the path the user typed.
    """
    pwd = env.get("PWD", "")
    try:
        if pwd.startswith("/") and os.path.samefile(pwd, "."):
            return pwd
    except OSError:
        pass
    return os.getcwd()


def terminal_command(
    argv: Sequence[str], env: Mapping[str, str], host: Host = INSTALLED
) -> tuple[list[str], dict[str, str]]:
    """Wrap the bwrap argv in script(1): (command, environment).

    The sandbox runs inside a freshly allocated pseudo-terminal. SIGWINCH
    propagates, job control works, and TIOCSTI is defanged: an ioctl inside
    the sandbox lands in script's pty, which script reads and writes back as
    bytes, not keystrokes, to the host terminal. --return keeps the agent's
    exit status. script runs ``$SHELL -c COMMAND``, so SHELL is bash and the
    argv is quoted for it (shlex quoting, which bash reads back to the same
    words as the bash shadow's ``printf %q``).

    Both script and bwrap come from the fixed tool path as absolute paths,
    so neither this process nor the inner shell looks anything up in PATH
    (ADR 26). The bash shadow uses PATH for both.
    """
    script, bwrap = _tool(host, "script"), _tool(host, "bwrap")
    command = shlex.join([bwrap, *argv[1:]])
    return (
        [script, "--return", "-q", "-E", "never", "-c", command, "/dev/null"],
        {**env, "SHELL": "/bin/bash"},
    )


def _tool(host: Host, name: str) -> str:
    path = host.find_tool(name)
    if path is None:
        _refuse(
            f"claude-sandbox: {name} not found in {':'.join(TOOL_PATH)};"
            " the sandbox needs it installed there.",
            127,
        )
    return path


def _exec(host: Host, path: str, argv: list[str], env: Mapping[str, str]) -> NoReturn:
    """Replace this process. Python ignores SIGPIPE and SIGXFSZ at start-up,
    and an ignored signal stays ignored across exec, so put them back."""
    sys.stdout.flush()
    sys.stderr.flush()
    for signum in (signal.SIGPIPE, signal.SIGXFSZ):
        signal.signal(signum, signal.SIG_DFL)
    try:
        host.execve(path, argv, env)
    except OSError as e:
        # bash's statuses for a command it cannot run.
        _refuse(
            f"claude-sandbox: {path}: {e.strerror}", 127 if e.errno == ENOENT else 126
        )


# --- the terminal -------------------------------------------------------------


class Terminal:
    """Warnings before launch, and the pause that keeps them on screen."""

    def __init__(self) -> None:
        self.warned = False

    def warn(self, message: str) -> None:
        """A warning that does not stop the launch (bash: launch_warn)."""
        self.warn_raw(f"claude-sandbox: {message}\n")

    def warn_raw(self, text: str) -> None:
        sys.stderr.write(text)
        sys.stderr.flush()
        self.warned = True

    def pause(self, verify: bool) -> None:
        """Hold any warning on screen until a key press.

        An agent redraws the whole terminal as it starts, so a warning
        printed before launch would vanish at once. Skipped when no person
        is at the terminal (stdin or stderr is not a tty) and for
        --sandbox-verify, which does not redraw. Ctrl-C cancels the launch.
        """
        if not self.warned or verify or not (os.isatty(0) and os.isatty(2)):
            return
        sys.stderr.write("Press any key to continue, Ctrl-C to cancel.")
        sys.stderr.flush()
        saved = termios.tcgetattr(0)
        quiet = termios.tcgetattr(0)
        quiet[3] &= ~(termios.ECHO | termios.ICANON)  # bash: read -s -n 1
        quiet[6][termios.VMIN] = 1
        quiet[6][termios.VTIME] = 0
        try:
            termios.tcsetattr(0, termios.TCSANOW, quiet)
            key = os.read(0, 1)
        finally:
            termios.tcsetattr(0, termios.TCSANOW, saved)
        if not key:  # end of input: bash's `read` fails and set -e exits
            sys.exit(1)
        sys.stderr.write("\n")


# --- exits --------------------------------------------------------------------


class Signalled(BaseException):
    """TERM or HUP arrived: unwind, then die by it."""

    def __init__(self, signum: int) -> None:
        super().__init__(signum)
        self.signum = signum


def raise_signalled(signum: int, frame: FrameType | None) -> None:
    raise Signalled(signum)


def die_by(signum: int) -> NoReturn:
    """Exit the way a process killed by ``signum`` does, so the caller's
    shell sees the signal, not a plain status."""
    sys.stderr.flush()
    signal.signal(signum, signal.SIG_DFL)
    os.kill(os.getpid(), signum)
    sys.exit(128 + signum)  # not reached unless the signal is blocked


def _refuse(message: str, status: int = 1) -> NoReturn:
    sys.stderr.write(f"{message}\n")
    sys.exit(status)
