"""The bwrap argv: a pure function of (profile, config, environment).

bwrap applies its operations in argv sequence, so the order of the
sections below is part of the security model (a mask must follow the bind it covers;
an allow-write bind must follow the masks it re-exposes a path through).

This is the ONLY place that contributes binds or environment to the argv
(ADR 26). It reads the filesystem only to test what exists, through an
injectable ``Probe``, and reads the environment only from the ``env`` mapping
it is given, never from ``os.environ``. Standard library only.
"""

import glob as _glob
import os
import re
import stat
from collections.abc import Iterable, Mapping, Sequence

from .config import Config, lines, words
from .errors import SandboxError
from .profiles import (
    LIBEXEC,
    SHARED_SKILLS_REL,
    SHIPPED_SKILLS_DIR,
    AgentProfile,
    agent_exec_argv,
    filter_chrome_args,
)

GITCONFIG_PATH = "/etc/claude-gitconfig"
# The jail's gh shim, first on its PATH unless no-forge (ADR 29): it gives gh
# the token cached for the repository in gh's own environment, so the
# session's environment never holds GH_TOKEN (battery check 04).
GH_SHIM_DIR = f"{LIBEXEC}/bin"

# Where the installer puts the shadow, and the names the sandbox owns there
# (Invariant 1: a plain `claude` must reach the shadow).
SHADOW_DIR = "/usr/local/bin"
ENTRY_POINTS = ("claude", "codex", "pi", "claude-sandbox")
# The directories the entry-point guard covered, for battery check 22.
ENTRY_GUARD_ENV = "CLAUDE_SANDBOX_ENTRY_GUARD"

# What the agent itself needs from the launching environment, forwarded by
# value when set and non-empty.
PASS_THROUGH = (
    "TERM",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "LC_MESSAGES",
    "LC_TIME",
    "LC_COLLATE",
    "LC_NUMERIC",
    "LC_MONETARY",
    "VIRTUAL_ENV",
    "UV_PROJECT_ENVIRONMENT",
    "UV_CACHE_DIR",
    "UV_PYTHON_CACHE_DIR",
    "UV_PYTHON_INSTALL_DIR",
    "UV_TOOL_DIR",
    "PRE_COMMIT_HOME",
    "CLAUDE_SANDBOX_WORKSPACE_ROOT",
)


# Names pass-env may never forward. Passing PATH would undo the shadow's PATH
# discipline (Invariant 1: plain `claude` must resolve to
# /usr/local/bin/claude); IS_SANDBOX would trip the recursion guard into
# skipping the jail. CODEX_HOME would move Codex's config + auth.json off the
# bound ~/.codex to an unbound tmpfs path, silently losing the login on exit.
# IS_SANDBOX_AGENT is emitted BEFORE the pass-env loop and a later --setenv
# of the same name wins, so forwarding it would let the conf forge the value
# battery check 03 uses to decide WHICH agent's config dir may legitimately
# appear under $HOME. An operator asking for these has misunderstood the
# flag, so the sandbox's own value wins.
PASS_ENV_DENY = frozenset(
    {
        "PATH",
        "HOME",
        "USER",
        "IS_SANDBOX",
        "GIT_CONFIG_GLOBAL",
        "GIT_CONFIG_SYSTEM",
        "CODEX_HOME",
        "CLAUDE_CONFIG_DIR",
        "CLAUDE_SANDBOX_AGENT",
        "IS_SANDBOX_AGENT",
        "BASH_ENV",
        "ENV",
        "SHELLOPTS",
        "BASHOPTS",
        "IFS",
    }
)
# ...and LD_PRELOAD and friends, which execute attacker-chosen code in every
# process the session spawns.
PASS_ENV_DENY_PREFIX = "LD_"


class Probe:
    """The filesystem facts the builder may ask for: the real filesystem,
    or a test's. Each follows symlinks, as the shell's ``test`` operators
    do. A test replaces the first four; the rest derive from ``mode``."""

    def mode(self, path: str) -> int:
        """``st_mode``, or 0 when ``path`` does not resolve."""
        try:
            return os.stat(path).st_mode
        except OSError:
            return 0

    def readable(self, path: str) -> bool:
        return os.access(path, os.R_OK)

    def realpath(self, path: str) -> str:
        # `realpath -e` as GNU coreutils does it, which is close to the
        # kernel's own path resolution: every component must exist, and a
        # non-directory may not be followed by anything, not even `.`, `..`
        # or `/`. The first stat raises exactly those errors on every Python
        # version; os.path.realpath(strict=True) does not (3.11 resolves
        # /dev/zero/.. to /dev, 3.13 refuses it). Once the path resolves,
        # os.path.realpath agrees across versions. The second stat refuses
        # what GNU's readlink walk cannot finish: a procfs magic link such as
        # /dev/stdin -> socket:[N].
        #
        # The kernel is stricter than GNU's walk in two corner cases, both
        # refusals here where GNU would resolve: a path needing more than 40
        # symlink hops, and `..` out of a directory without search
        # permission. uutils coreutils (Ubuntu 25.10+) is more lenient still
        # and accepts /dev/zero/../null; this follows GNU.
        os.stat(path)
        resolved = os.path.realpath(path)
        os.stat(resolved)
        return resolved

    def glob(self, pattern: str) -> list[str]:
        # Code-point order on every host, whatever the locale.
        return sorted(_glob.glob(pattern))

    def is_dir(self, path: str) -> bool:
        return stat.S_ISDIR(self.mode(path))

    def is_file(self, path: str) -> bool:
        return stat.S_ISREG(self.mode(path))

    def exists(self, path: str) -> bool:
        return self.mode(path) != 0


HOST = Probe()


def path_ahead_of_shadow(path: str, probe: Probe = HOST) -> list[str]:
    """The directories a ``PATH`` lookup searches before the shadow's.

    Each is resolved through symlinks, as the binds are, and listed once, in
    ``PATH`` order. Empty and relative entries are skipped, and so are
    entries that do not resolve to a directory. When ``PATH`` does not hold
    ``SHADOW_DIR`` at all, every entry counts as ahead of it.
    """
    try:
        shadow_dir = probe.realpath(SHADOW_DIR)
    except OSError:
        shadow_dir = SHADOW_DIR
    dirs: list[str] = []
    for entry in path.split(":"):
        if not entry.startswith("/"):
            continue
        if os.path.normpath(entry) == SHADOW_DIR:
            break
        try:
            resolved = probe.realpath(entry)
        except OSError:
            continue
        if resolved == shadow_dir:
            break
        if probe.is_dir(resolved) and resolved not in dirs:
            dirs.append(resolved)
    return dirs


def inside(path: str, roots: Iterable[str]) -> bool:
    """``path`` is one of ``roots`` or lies under one (all resolved)."""
    return any(os.path.commonpath([path, root]) == root for root in roots)


def _lookup(env: Mapping[str, str], name: str) -> str:
    """The variable's value, or empty. Values come from the environment
    only. With no TERM there, the agent gets TERM=dumb, as from a shell; an
    empty TERM stays empty."""
    return env.get(name, "dumb" if name == "TERM" else "")


def bwrap_build(
    profile: AgentProfile,
    config: Config,
    env: Mapping[str, str],
    workspace: str,
    real_agent: str,
    args: Sequence[str],
    *,
    verify: bool = False,
    shipped_skills_dir: str = SHIPPED_SKILLS_DIR,
    gitconfig_path: str = GITCONFIG_PATH,
    probe: Probe = HOST,
) -> list[str]:
    """The full ``bwrap ... -- agent args`` command.

    ``env`` is the launch environment with the conf applied (what
    ``config.parse_config`` returns) and ``config`` is
    ``Config.from_env(env)``. ``real_agent`` is the host binary bound back
    for a ``bind_back`` profile. ``verify`` runs the integrity battery in
    place of the agent. ``shipped_skills_dir`` and ``gitconfig_path`` are
    constants a caller overrides only in tests, never read from the
    environment. ``env["PATH"]`` is the launching PATH, read
    only for the entry-point guard. Raises SandboxError for an allow-device
    entry that is not a device node under /dev, and for an allow-write entry
    that is not an absolute path.
    """
    home = env.get("HOME") or "/root"

    argv = [
        "bwrap",
        "--ro-bind", "/", "/",
        # Fresh /dev (not --dev-bind) hides the host's /dev/pts so a TIOCSTI
        # inside the sandbox can only inject into the script(1)-allocated pty
        # the shadow wraps us in.
        "--dev", "/dev",
        # Unconditional ro-bind of host /proc — host PIDs visible
        # (info-disclosure, accepted) but kernel pidns isolation intact.
        "--ro-bind", "/proc", "/proc",
        "--tmpfs", "/tmp",
    ]  # fmt: skip

    # Keep the private /dev and its isolated terminal namespace. Only the
    # explicitly selected device nodes are restored, with --dev-bind so
    # bubblewrap does not apply the nodev flag used for ordinary binds.
    for device in lines(config.allow_devices):
        try:
            resolved = probe.realpath(device)
        except OSError as e:
            raise SandboxError(f"realpath: {device}: {e.strerror}") from None
        mode = probe.mode(resolved)
        if (
            not device.startswith("/dev/")
            or not resolved.startswith("/dev/")
            or not (stat.S_ISCHR(mode) or stat.S_ISBLK(mode))
        ):
            raise SandboxError(
                "claude-sandbox: allow-device needs a character or block device"
                f" under /dev: {device}"
            )
        argv += ["--dev-bind", resolved, resolved]
    if config.gpu:
        # The container runtime supplies driver libraries in the read-only
        # root and selects the available GPUs. Never bind all of /dev.
        for pattern in ("/dev/nvidia*", "/dev/nvidia-caps/*", "/dev/dri/*"):
            for device in probe.glob(pattern):
                if stat.S_ISCHR(probe.mode(device)):
                    argv += ["--dev-bind", device, device]

    # /run/{user,secrets} masks are emitted only when the host has the source
    # dir. Bwrap can't mkdir into a read-only /run when the parent has no
    # such subdir (typical of GHA's ubuntu-24.04 runner).
    for run_dir in ("/run/user", "/run/secrets"):
        if probe.is_dir(run_dir):
            argv += ["--tmpfs", run_dir]

    # Strict-under-/root by inversion: wipe $HOME, then bind back only what
    # the agent legitimately needs. Anything we forgot to enumerate stays
    # masked — the whole point of inverting.
    argv += ["--tmpfs", home]

    # Single bind-back list. --bind on a missing source would abort bwrap, so
    # each entry is gated on existence.
    #
    # Split-by-XDG-category: $HOME/.config stays strict-allowlist
    # (credentials live here — gh/glab tokens, gcloud OAuth, etc.) so
    # forward-compat masking of new credentialed tools still applies.
    # $HOME/.local/share (XDG data) and $HOME/.cache are bulk-bound. Bets on
    # XDG discipline — a tool that drops creds under ~/.local/share/<tool>/
    # instead of ~/.config/<tool>/ would leak. gh/glab token dirs are skipped
    # when CLAUDE_SANDBOX_NO_FORGE=1: the operator has declared this session
    # should not push to any forge. SHARED_SKILLS_REL joins every agent's
    # list: it holds skills, not credentials, so it is the one home path
    # agents deliberately share.
    #
    # Every directory bound read-write is also listed in `writable`, for the
    # entry-point guard at the end of the mounts.
    writable: list[str] = []
    forge_rels = () if config.no_forge else (".config/gh", ".config/glab-cli")
    for rel in (*profile.home_dirs, SHARED_SKILLS_REL, ".cache", *forge_rels):
        if probe.is_dir(f"{home}/{rel}"):
            argv += ["--bind", f"{home}/{rel}", f"{home}/{rel}"]
            writable.append(f"{home}/{rel}")
    # Per-agent tmpfs masks, emitted AFTER the binds above so they cover a
    # sub-path of a directory just bound rw. Unconditional, like the
    # .local/share masks: the mask must exist whether or not the host
    # currently has the directory, or a first in-session write would land on
    # the host.
    for rel in profile.home_tmpfs:
        argv += ["--tmpfs", f"{home}/{rel}"]
    # Shipped skills, one --ro-bind PER SKILL onto the agent's skills dir.
    # Per skill, not per tree: every agent discovers skills exactly one level
    # deep (<skills>/<name>/SKILL.md), so binding the whole tree would bury
    # them a level down; and a per-skill bind leaves the user's own skills in
    # the same directory visible alongside. Emitted after the rw binds so it
    # sits inside the bound config dir, and read-only so the bundled scripts
    # stay exactly what install placed. A `*/` pattern matches only
    # directories and links to them.
    for skill_dir in probe.glob(_glob.escape(shipped_skills_dir) + "/*/"):
        skill_dir = skill_dir.removesuffix("/")
        name = skill_dir.rpartition("/")[2]
        argv += ["--ro-bind", skill_dir, f"{home}/{profile.skills_rel}/{name}"]
    # $HOME/.local/share bulk-bound for host XDG data dirs (helm plugins,
    # krew, uv Python, etc.). Two sub-dirs stay ephemeral via tmpfs:
    #   applications/  Claude Code writes a .desktop URL handler here;
    #                  binding the host's dir would register the in-sandbox
    #                  claude as a host URL handler.
    #   claude/        Claude Code's versioned binary cache, ephemeral by
    #                  design and would collide with the host's install.
    if probe.is_dir(f"{home}/.local/share"):
        argv += ["--bind", f"{home}/.local/share", f"{home}/.local/share"]
        writable.append(f"{home}/.local/share")
        argv += ["--tmpfs", f"{home}/.local/share/applications"]
        argv += ["--tmpfs", f"{home}/.local/share/claude"]
    for rel in (*profile.home_files, ".local/bin/uv", ".local/bin/uvx"):
        if probe.is_file(f"{home}/{rel}"):
            argv += ["--bind", f"{home}/{rel}", f"{home}/{rel}"]

    # The real binary lives off-PATH on the host; expose it at the agent's
    # conventional ~/.local/bin/<agent> so its self-inspection (Claude's
    # installMethod=native check) and agent-spawns-agent lookups see the path
    # they expect. Unconditional — the shadow's loud-fail upstream catches a
    # missing real binary.
    #
    # Read-only. The bash bound it read-write, which let a session rewrite
    # the binary every later session (and every other agent's session) runs:
    # persistence across sessions. Nothing the agent does needs to write it;
    # its updater is off by managed settings.
    if profile.bind_back:
        argv += ["--ro-bind", real_agent, f"{home}/{profile.inner_rel}"]

    if workspace and probe.is_dir(workspace):
        argv += ["--bind", workspace, workspace]
        writable.append(workspace)
    # The agent's temp root (profile.tmpdir). Skipped when prepare_home could
    # not create it; the agent then falls back to the private /tmp.
    tmpdir = profile.tmpdir if profile.tmpdir and probe.is_dir(profile.tmpdir) else ""
    if tmpdir:
        argv += ["--bind", tmpdir, tmpdir]
        writable.append(tmpdir)
    # -e, not -d/-f: a unix socket is neither, so a -f test would silently
    # drop it, and rootless podman/docker expose their engine as a socket
    # under $XDG_RUNTIME_DIR. Dangling symlinks stay skipped: bwrap aborts on
    # a --bind whose source does not resolve.
    #
    # Emitted after the /run/{user,secrets} masks above, and the order is
    # load-bearing: a bind listed here re-exposes a single path *through* a
    # mask without lifting it (`allow-write = /run/user/1000/podman/
    # podman.sock` reaches the engine while ssh-agent, gpg-agent, dbus and
    # keyring sockets stay masked). Hoisting it above the masks would let a
    # mask clobber the operator's bind silently.
    #
    # A relative entry is refused rather than skipped: bwrap would resolve it
    # against the launch directory, the workspace, which a session can write,
    # so a conf line that meant one path could bind another.
    for path in lines(config.allow_write):
        if not path.startswith("/"):
            raise SandboxError(
                f"claude-sandbox: allow-write needs an absolute path: {path}"
            )
        if probe.exists(path):
            argv += ["--bind", path, path]
            writable.append(path)

    # Defence-in-depth file masks. Strict-under-/root already hides the $HOME
    # dotfiles, but masking them with /dev/null is free and survives if the
    # strict-root bind ever regresses. /etc masks are gated on readability so
    # non-root hosts don't trip EROFS.
    for name in (".netrc", ".Xauthority", ".ICEauthority"):
        argv += ["--bind-try", "/dev/null", f"{home}/{name}"]
    for path in ("/etc/shadow", "/etc/gshadow", "/etc/sudoers"):
        if probe.readable(path):
            argv += ["--bind", "/dev/null", path]

    # Use the staged pasta-forwarder resolver inside the network jail.
    resolv = env.get("CLAUDE_SANDBOX_JAIL_RESOLV", "")
    if resolv and probe.readable(resolv):
        argv += ["--ro-bind", resolv, "/etc/resolv.conf"]

    # Entry-point guard. Protect the sandbox's entry-point names (Invariant
    # 1): a session cannot create a command named claude, codex, pi or
    # claude-sandbox in a writable directory that precedes the shadow on
    # PATH. Each such name in each such directory gets a read-only bind of
    # /dev/null: inside the jail it is a mount point that cannot be written,
    # replaced, renamed or removed, and with bwrap's nodev on ordinary binds
    # it cannot even be opened. Where the name did not exist, bwrap creates
    # the mount point on the host as an empty file without execute bits,
    # which a PATH lookup passes over. The shadow refuses to launch when one
    # of these names there is anything else (shadow.check_entry_points), so
    # the bind only ever lands on that empty file or nothing. A directory
    # that does not exist at launch is not covered: binding inside it would
    # create it. The shadow's check runs again at the next launch.
    #
    # Last of the mounts: bwrap applies argv in order, so these must follow
    # every read-write bind they sit inside.
    roots: list[str] = []
    for root in writable:
        try:
            roots.append(probe.realpath(root))
        except OSError:
            continue
    guarded = [
        directory
        for directory in path_ahead_of_shadow(env.get("PATH", ""), probe)
        if inside(directory, roots)
    ]
    for directory in guarded:
        for name in ENTRY_POINTS:
            argv += ["--ro-bind", "/dev/null", f"{directory}/{name}"]

    argv += [
        "--cap-drop", "ALL",
        # --unshare-user-try is required when bwrap runs as root inside a
        # nested container that lacks CAP_SYS_ADMIN. When bwrap runs as
        # non-root it implicitly unshares user anyway.
        "--unshare-user-try",
        "--unshare-pid",
        "--unshare-ipc",
        "--unshare-uts",
        "--unshare-cgroup-try",
        # No --new-session: setsid() severs SIGWINCH delivery. The TIOCSTI
        # defence is delegated to the script(1) wrap around bwrap.
        "--die-with-parent",
    ]  # fmt: skip

    # Scrub the env by default, then re-export only what the agent needs.
    # $HOME/.local/bin is APPENDED so system tools take precedence.
    argv += ["--clearenv"]
    path = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
    if not config.no_forge:
        path = f"{GH_SHIM_DIR}:{path}"
    path += f":{home}/.local/bin"
    # Outer devcontainers point $VIRTUAL_ENV at a /cache-backed venv, visible
    # via --ro-bind / /. APPENDED so the /usr/local/bin/claude shadow still
    # wins resolution (Invariant 1).
    venv = env.get("VIRTUAL_ENV", "")
    if venv and probe.is_dir(f"{venv}/bin"):
        path += f":{venv}/bin"
    argv += ["--setenv", "PATH", path]
    argv += ["--setenv", "HOME", home]
    argv += ["--setenv", "USER", "root"]
    argv += ["--setenv", "IS_SANDBOX", "1"]
    # Which agent's session this is, for the in-sandbox verifier: check 03
    # asserts the EXACT contents of $HOME. Deliberately NOT named
    # CLAUDE_SANDBOX_AGENT: that is detect_agent's override, and a nested
    # `claude` inside a codex session would then re-dispatch to codex.
    argv += ["--setenv", "IS_SANDBOX_AGENT", profile.name]
    # Pi's discovery runs inside the jail, through the relayed model port.
    if config.local_model_port != "0":
        argv += ["--setenv", "CLAUDE_SANDBOX_LOCAL_MODEL_PORT", config.local_model_port]
    argv += ["--setenv", "GIT_CONFIG_GLOBAL", gitconfig_path]
    argv += ["--setenv", "GIT_CONFIG_SYSTEM", "/dev/null"]
    # Per-agent env the sandbox sets for itself. Empty for claude.
    for name, value in profile.setenv:
        argv += ["--setenv", name, value]
    # Only with the bind: pointed at the read-only /var/tmp, Claude could not
    # create its temp dir at all.
    if tmpdir:
        argv += ["--setenv", "CLAUDE_CODE_TMPDIR", tmpdir]
    for name in PASS_THROUGH:
        if value := _lookup(env, name):
            argv += ["--setenv", name, value]

    # Operator-configured passthrough (pass-env in claude-sandbox.conf): what
    # the *project* needs — DOCKER_HOST for a container-based test suite, and
    # the like. Opt-in by name, so --clearenv stays the default. Names only:
    # values come from the launching environment, so pass-env can forward a
    # variable the operator's shell already has but cannot invent a value.
    # Split only, never globbed against the jail-writable cwd (see
    # config.words).
    for name in words(config.pass_env):
        # Not a shell identifier — skip rather than emit a --setenv bwrap
        # would choke on.
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
            continue
        if name in PASS_ENV_DENY or name.startswith(PASS_ENV_DENY_PREFIX):
            continue
        if value := _lookup(env, name):
            argv += ["--setenv", name, value]
    # What the entry-point guard covered, for battery check 22. After
    # pass-env, so a forwarded variable of the same name cannot replace it.
    if guarded:
        argv += ["--setenv", ENTRY_GUARD_ENV, ":".join(guarded)]

    # Disable the Chrome browser-extension RPC channel: strip any
    # user-supplied --chrome so it can't override the --no-chrome injection.
    # The native-messaging-host bridge would let any installed Chrome
    # extension on the host invoke tools inside this in-sandbox Claude.
    user_args = filter_chrome_args(args) if profile.filter_chrome else list(args)

    # Exec via the in-sandbox conventional path so the agent's argv[0]
    # matches what its official installer would have placed.
    command = agent_exec_argv(profile, home, verify=verify)
    return [*argv, "--", *command, *user_args]
