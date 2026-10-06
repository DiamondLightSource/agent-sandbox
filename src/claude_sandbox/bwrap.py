"""The bwrap argv: a pure function of (profile, config, environment).

Ported line for line from ``bwrap_argv_build`` in
``.devcontainer/claude-sandbox/claude-shadow``, and kept in the same order:
bwrap applies its operations in argv sequence, so the order of the sections
below is part of the security model (a mask must follow the bind it covers;
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
from collections.abc import Mapping, Sequence
from typing import Protocol

from .config import Config, lines, words
from .errors import SandboxError
from .profiles import (
    SHARED_SKILLS_REL,
    SHIPPED_SKILLS_DIR,
    AgentProfile,
    agent_exec_argv,
    filter_chrome_args,
)

GITCONFIG_PATH = "/etc/claude-gitconfig"

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

# bash itself gives TERM the value `dumb` when the environment has none, and
# the bash shadow forwards that shell variable like any other. Kept so the
# argv matches; an empty TERM in the environment stays empty.
#
# TERM is the ONLY shell variable ported, deliberately. The bash's pass-env
# reads any shell variable by name, so it can also forward ones bash or the
# shadow invents (HOSTNAME, RANDOM, PPID, AGENT_REAL, ...). That is an
# accident of `${!name}`, not a feature: the port reads pass-env values from
# the environment only, which is correct. Don't "fix" it towards the bash.
SHELL_DEFAULTS = {"TERM": "dumb"}

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


class Probe(Protocol):
    """The filesystem facts the builder may ask for. Each follows symlinks,
    as the bash ``test`` operators do."""

    def is_dir(self, path: str) -> bool: ...  # [ -d ]
    def is_file(self, path: str) -> bool: ...  # [ -f ]
    def exists(self, path: str) -> bool: ...  # [ -e ]
    def readable(self, path: str) -> bool: ...  # [ -r ]
    def is_char_device(self, path: str) -> bool: ...  # [ -c ]
    def is_block_device(self, path: str) -> bool: ...  # [ -b ]
    def realpath(self, path: str) -> str: ...  # realpath -e; raises OSError
    def glob(self, pattern: str) -> list[str]: ...  # sorted pathname expansion


def _mode_is(path: str, test: int) -> bool:
    try:
        return stat.S_IFMT(os.stat(path).st_mode) == test
    except OSError:
        return False


class HostProbe:
    """The real filesystem."""

    def is_dir(self, path: str) -> bool:
        return os.path.isdir(path)

    def is_file(self, path: str) -> bool:
        return os.path.isfile(path)

    def exists(self, path: str) -> bool:
        return os.path.exists(path)

    def readable(self, path: str) -> bool:
        return os.access(path, os.R_OK)

    def is_char_device(self, path: str) -> bool:
        return _mode_is(path, stat.S_IFCHR)

    def is_block_device(self, path: str) -> bool:
        return _mode_is(path, stat.S_IFBLK)

    def realpath(self, path: str) -> str:
        # `realpath -e` as GNU coreutils does it, which is the kernel's own
        # path resolution: every component must exist, and a non-directory
        # may not be followed by anything, not even `.`, `..` or `/`. The
        # first stat raises exactly those errors on every Python version;
        # os.path.realpath(strict=True) does not (3.11 resolves
        # /dev/zero/.. to /dev, 3.13 refuses it). Once the path resolves,
        # os.path.realpath agrees across versions. The second stat refuses
        # what GNU's readlink walk cannot finish: a procfs magic link such as
        # /dev/stdin -> socket:[N]. uutils coreutils (Ubuntu 25.10+) is more
        # lenient and accepts /dev/zero/../null; this follows GNU.
        os.stat(path)
        resolved = os.path.realpath(path)
        os.stat(resolved)
        return resolved

    def glob(self, pattern: str) -> list[str]:
        # Code-point order on every host. The bash sorts by the launching
        # locale's collation (en_US puts alpha/ before Beta/, C the reverse);
        # only the order of binds onto distinct destinations differs.
        return sorted(_glob.glob(pattern))


HOST = HostProbe()


def _lookup(env: Mapping[str, str], name: str) -> str:
    """``${!name:-}`` in the bash shadow: the variable's value, or empty."""
    return env[name] if name in env else SHELL_DEFAULTS.get(name, "")


def bwrap_argv(
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
    constants a caller overrides only in tests; the git config path is not
    read from the environment (in the bash, the shadow exports its constant
    before the builder runs). Raises SandboxError for an allow-device entry that
    is not a device node under /dev.
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
        if (
            not device.startswith("/dev/")
            or not resolved.startswith("/dev/")
            or (
                not probe.is_char_device(resolved)
                and not probe.is_block_device(resolved)
            )
        ):
            raise SandboxError(
                "claude-sandbox: allow-device needs a character or block device"
                f" under /dev: {device}"
            )
        argv += ["--dev-bind", resolved, resolved]
    if config.gpu:
        # The container runtime supplies driver libraries in the read-only
        # root and selects the available GPUs. Never bind all of /dev.
        # Glob order is code-point order, not the bash's locale collation:
        # the binds are to distinct paths, so only their order can differ.
        for pattern in ("/dev/nvidia*", "/dev/nvidia-caps/*", "/dev/dri/*"):
            for device in probe.glob(pattern):
                if probe.is_char_device(device):
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
    forge_rels = () if config.no_forge else (".config/gh", ".config/glab-cli")
    for rel in (*profile.home_dirs, SHARED_SKILLS_REL, ".cache", *forge_rels):
        if probe.is_dir(f"{home}/{rel}"):
            argv += ["--bind", f"{home}/{rel}", f"{home}/{rel}"]
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
    # stay exactly what install placed.
    # A `*/` pattern matches only directories and links to them, so unlike
    # the bash (whose `-d` test drops the unmatched pattern itself) there is
    # nothing to filter.
    # As for the GPU nodes, skills come in code-point order where the bash
    # used the locale's collation; each lands on its own destination.
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
    if profile.bind_back:
        argv += ["--bind", real_agent, f"{home}/{profile.inner_rel}"]

    if workspace and probe.is_dir(workspace):
        argv += ["--bind", workspace, workspace]
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
    for path in lines(config.allow_write):
        if probe.exists(path):
            argv += ["--bind", path, path]

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
    for name in PASS_THROUGH:
        if value := _lookup(env, name):
            argv += ["--setenv", name, value]

    # Operator-configured passthrough (pass-env in claude-sandbox.conf): what
    # the *project* needs — DOCKER_HOST for a container-based test suite, and
    # the like. Opt-in by name, so --clearenv stays the default. Names only:
    # values come from the launching environment, so pass-env can forward a
    # variable the operator's shell already has but cannot invent a value.
    # Split only: the bash also expands each word as a glob against the cwd,
    # the jail-writable workspace, which the port refuses (see config.words).
    for name in words(config.pass_env):
        # Not a shell identifier — skip rather than emit a --setenv bwrap
        # would choke on.
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
            continue
        if name in PASS_ENV_DENY or name.startswith(PASS_ENV_DENY_PREFIX):
            continue
        if value := _lookup(env, name):
            argv += ["--setenv", name, value]

    # Disable the Chrome browser-extension RPC channel: strip any
    # user-supplied --chrome so it can't override the --no-chrome injection.
    # The native-messaging-host bridge would let any installed Chrome
    # extension on the host invoke tools inside this in-sandbox Claude.
    user_args = filter_chrome_args(args) if profile.filter_chrome else list(args)

    # Exec via the in-sandbox conventional path so the agent's argv[0]
    # matches what its official installer would have placed.
    return [*argv, "--", *agent_exec_argv(profile, home, verify=verify), *user_args]
