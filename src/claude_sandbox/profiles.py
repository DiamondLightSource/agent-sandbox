"""Agent profiles: everything that differs between the wrapped agents.

One shadow wraps several agents (ADR 18, Invariant 0) and picks the profile
from the name it was invoked as. All agents share the same filesystem,
network and terminal isolation; a profile selects only the binary, the
persistent state and the injected flags. Add an agent by adding a profile
here, never by touching the argv builder.

Standard library only: this module is on the launch path (ADR 26).
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from .errors import SandboxError

LIBEXEC = "/usr/libexec/claude-sandbox"

# Shipped skills: the repo's top-level skills/ tree, placed by the installer
# under /usr/libexec next to the runtime helpers (root-owned, ro in-session).
# The argv builder binds each one read-only onto the agent's own skills
# directory INSIDE the sandbox, so the user's host ~/.claude (or ~/.codex,
# ~/.pi) is never written to and the skill's scripts cannot be rewritten by
# a compromised session. A constant, not an env seam: tests pass the
# builder a fixture; nothing outside the jail's trust boundary can choose a
# bind source.
SHIPPED_SKILLS_DIR = f"{LIBEXEC}/skills"

# Shared user skills (issue #52, ADR 25): $HOME-relative, bound READ-WRITE
# into EVERY agent's session, unlike the per-agent config dirs. Codex and Pi
# discover skills here natively; Claude reaches one only through a symlink
# the user places in ~/.claude/skills. Only this one directory is shared —
# never the rest of ~/.agents, whose contents are whatever third-party tools
# decide to write there (Codex already reads ~/.agents/plugins).
SHARED_SKILLS_REL = ".agents/skills"

# The in-sandbox integrity battery, run by `claude-sandbox verify` through the
# same profile and isolation instead of the agent.
VERIFY_BATTERY = f"{LIBEXEC}/verify-sandbox-battery.sh"


@dataclass(frozen=True, slots=True)
class AgentProfile:
    """One agent's knobs. ``$HOME``-relative paths carry no leading slash."""

    name: str  # stamped into IS_SANDBOX_AGENT
    real: str  # real binary on the host, off the user's PATH
    inner_rel: str  # conventional path inside the sandbox (bind_back only)
    bind_back: bool  # bind ``real`` to ``$HOME/inner_rel`` and exec that
    home_dirs: tuple[str, ...]  # dirs bound rw (config + login state)
    home_files: tuple[str, ...]  # files bound rw
    home_tmpfs: tuple[str, ...]  # tmpfs masks over sub-paths of home_dirs
    skills_rel: str  # dir the agent discovers skills in
    inject: tuple[str, ...]  # args injected ahead of the user's
    filter_chrome: bool  # strip a user-supplied --chrome
    setenv: tuple[tuple[str, str], ...]  # env the sandbox sets for itself
    label: str  # human name for messages
    exec_via: str = ""  # launcher exec'd with ``real`` as its argument


PROFILES: Mapping[str, AgentProfile] = {
    "claude": AgentProfile(
        name="claude",
        real=f"{LIBEXEC}/claude",
        # Bound back to the conventional path inside the sandbox so Claude's
        # installMethod=native self-check sees what it expects (Invariant 1).
        # Read-only, so a session cannot rewrite the binary later sessions
        # run (the bash shadow bound it read-write).
        inner_rel=".local/bin/claude",
        bind_back=True,
        home_dirs=(".claude",),
        home_files=(".claude.json",),
        home_tmpfs=(),
        skills_rel=".claude/skills",
        inject=("--no-chrome",),
        filter_chrome=True,
        # Claude's updater-disable is delivered by managed-settings
        # (env.DISABLE_AUTOUPDATER), so there is nothing to add here.
        setenv=(),
        label="Claude",
    ),
    "codex": AgentProfile(
        name="codex",
        # Codex ships as a package — the binary needs its siblings (ripgrep
        # at codex-path/rg, its own bwrap/zsh under codex-resources/). So the
        # whole release directory is relocated and we exec IN PLACE from it:
        # /usr/libexec is already visible inside the sandbox via --ro-bind / /.
        # The binary we exec is READ-ONLY in the session, so an in-session
        # self-update cannot rewrite it.
        real=f"{LIBEXEC}/codex-dist/bin/codex",
        inner_rel="",
        bind_back=False,
        # CODEX_HOME defaults to ~/.codex: config.toml, auth.json, sessions/.
        # Bound rw for the same reason ~/.claude is (Invariant 2): it is ONE
        # OpenAI login, not a repo-scoped credential like a gh/glab PAT.
        home_dirs=(".codex",),
        home_files=(),
        # The vendor installer unpacks the codex binary itself under
        # $CODEX_HOME/packages/ — INSIDE the directory bound read-write. Left
        # visible, that is a writable copy of the agent's own binary in its
        # own session, which the next launch could re-execute. Mask it.
        home_tmpfs=(".codex/packages",),
        skills_rel=".codex/skills",
        # Codex has no browser-extension RPC channel to disable and no
        # --chrome flag; injecting Claude's would abort the launch.
        inject=(),
        filter_chrome=False,
        # The in-sandbox half of the updater-disable: stops `codex update`
        # fetching a release it cannot install and half-unpacking it into
        # CODEX_HOME. /etc/codex/managed_config.toml covers the startup check.
        setenv=(("CODEX_UPDATE_DISABLED", "1"),),
        label="Codex",
        exec_via=f"{LIBEXEC}/codex-launch",
    ),
    "pi": AgentProfile(
        name="pi",
        # A fixed launcher checks the sandbox before starting the standalone
        # release. Both live under the read-only /usr/libexec tree.
        real=f"{LIBEXEC}/pi-run",
        inner_rel="",
        bind_back=False,
        home_dirs=(".pi",),
        home_files=(),
        home_tmpfs=(),
        skills_rel=".pi/agent/skills",
        inject=(),
        filter_chrome=False,
        setenv=(("PI_SKIP_VERSION_CHECK", "1"),),
        label="Pi",
    ),
}


def detect_agent(argv0: str, override: str = "") -> str:
    """Name the agent this invocation wraps.

    ``override`` (CLAUDE_SANDBOX_AGENT) exists for tests and for reaching a
    shadow through a differently-named symlink. It is matched against a
    CLOSED set and can only ever select a hard-coded profile — it can never
    name an arbitrary binary for the sandbox to launch. An unrecognised
    ``argv0`` falls back to claude.
    """
    if override:
        if override in PROFILES:
            return override
        raise SandboxError(
            f"claude-sandbox: unknown CLAUDE_SANDBOX_AGENT '{override}'"
            " (expected: claude, codex, pi)."
        )
    base = argv0.rpartition("/")[2]
    return base if base in ("codex", "pi") else "claude"


def filter_chrome_args(args: Iterable[str]) -> list[str]:
    """``args`` with every ``--chrome`` removed.

    Stripping it stops a user-supplied flag from overriding the --no-chrome
    injection (the browser-extension native-messaging RPC channel is outside
    the threat model). Both the recursion guard and the argv builder strip
    through this one helper so the two paths can never diverge.
    """
    return [arg for arg in args if arg != "--chrome"]


def agent_exec_argv(
    profile: AgentProfile, home: str, *, verify: bool = False
) -> list[str]:
    """The command for fresh and nested launches, before the user's args."""
    if verify:
        return ["/bin/bash", VERIFY_BATTERY]
    if profile.exec_via:
        command = [profile.exec_via, profile.real]
    elif profile.bind_back:
        command = [f"{home}/{profile.inner_rel}"]
    else:
        command = [profile.real]
    return [*command, *profile.inject]
