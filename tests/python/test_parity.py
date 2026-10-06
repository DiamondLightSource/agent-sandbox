"""The comparison harness (ADR 26, issue #72 phase 1).

Every scenario runs through the bash shadow (via argv_driver.sh) and the
Python port, against the same temporary directory tree and the same
environment, and the results must be identical. The scenarios cover every
case in tests/bwrap_argv.sh; ``maps`` names the ones each stands for.

Paths the bash hard-codes (/run/user, /etc/shadow, /dev/nvidia*, ...) are
read from the real host by both builders, so they agree on any host; the
branches only some hosts reach are pinned in test_units.py.
"""

import os
import shutil
import socket
import subprocess
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from claude_sandbox.bwrap import GITCONFIG_PATH, bwrap_argv
from claude_sandbox.config import (
    KNOBS,
    Config,
    callback_enabled,
    callback_ports,
    egress_jail_enabled,
    local_model_enabled,
    local_ports,
    parse_config,
    resolve_workspace_root,
    validate_callback_ports,
    validate_local_model_port,
)
from claude_sandbox.errors import SandboxError
from claude_sandbox.profiles import PROFILES, agent_profile

REPO = Path(__file__).resolve().parents[2]
SHADOW = REPO / ".devcontainer" / "claude-sandbox" / "claude-shadow"
DRIVER = Path(__file__).with_name("argv_driver.sh")
BASH = shutil.which("bash") or "/bin/bash"

# What every scenario starts from; a scenario's env overrides it, and None
# removes a variable.
BASE_ENV = {"HOME": "{root}/home", "PATH": "{path}"}
SYSTEM_PATH = "/usr/bin:/bin"

# A refusal compares as this marker followed by the message, so a mismatch
# reads as a plain list diff either way.
REFUSED = "<refused>"


def driver(
    root: Path, env: Mapping[str, str], *args: str
) -> subprocess.CompletedProcess[bytes]:
    """Run argv_driver.sh in ``root`` with exactly ``env``."""
    return subprocess.run(
        [BASH, str(DRIVER), str(SHADOW), *args],
        env=dict(env),
        cwd=root,
        capture_output=True,
        check=False,
    )


def nul_split(out: bytes) -> list[str]:
    return [os.fsdecode(item) for item in out.split(b"\0")[:-1]]


def build_tree(root: Path, entries: Iterable[str]) -> None:
    """Create ``entries`` under ``root``: ``dir/``, ``file``, ``fifo|fifo``,
    ``sock|sock`` or ``link -> target``."""
    for entry in entries:
        entry = entry.replace("{root}", str(root))
        name, _, target = entry.partition(" -> ")
        name, _, kind = name.partition("|")
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        if target:
            path.symlink_to(target)
        elif name.endswith("/"):
            path.mkdir(exist_ok=True)
        elif kind == "fifo":
            os.mkfifo(path)
        elif kind == "sock":
            with socket.socket(socket.AF_UNIX) as sock:
                sock.bind(str(path))
        else:
            path.touch()


def scenario_env(
    root: Path, overrides: Mapping[str, str | None], path: str
) -> dict[str, str]:
    merged: dict[str, str | None] = {**BASE_ENV, **overrides}
    return {
        k: v.replace("{root}", str(root)).replace("{path}", path)
        for k, v in merged.items()
        if v is not None
    }


@pytest.fixture(scope="session")
def bash_path(tmp_path_factory: pytest.TempPathFactory) -> str:
    """PATH for the bash side, with GNU coreutils' realpath first.

    allow-device runs `realpath -e`. GNU coreutils (CI runners, Debian and
    Ubuntu 24.04 guests) refuses /dev/zero/..; uutils, the default from
    Ubuntu 25.10, accepts it. The port follows GNU, so on a uutils host the
    bash is pointed at GNU's copy, which Ubuntu installs as gnurealpath.
    """
    realpath = shutil.which("realpath", path=SYSTEM_PATH)
    assert realpath is not None, f"no realpath on {SYSTEM_PATH}"
    version = subprocess.run(
        [realpath, "--version"], capture_output=True, text=True, check=False
    ).stdout
    if "uutils" not in version:
        return SYSTEM_PATH
    gnu = shutil.which("gnurealpath")
    if gnu is None:
        pytest.fail(
            "realpath is uutils and gnurealpath is missing: install gnu-coreutils"
        )
    shim = tmp_path_factory.mktemp("gnu-bin")
    (shim / "realpath").symlink_to(gnu)
    return f"{shim}:{SYSTEM_PATH}"


def installed_locales() -> list[str]:
    out = subprocess.run(["locale", "-a"], capture_output=True, text=True)
    return out.stdout.split()


def snapshot(root: Path) -> list[str]:
    return sorted(str(p) for p in root.rglob("*"))


@dataclass(frozen=True)
class Scenario:
    name: str
    maps: str  # the tests/bwrap_argv.sh cases this one stands for
    env: Mapping[str, str | None] = field(default_factory=dict[str, str | None])
    tree: tuple[str, ...] = ("home/",)
    agent: str = "claude"
    workspace: str = "{root}/home"
    real: str = "/test/.local/bin/claude"
    args: tuple[str, ...] = ()
    conf: str | None = None
    verify: bool = False
    gitconfig: str = GITCONFIG_PATH
    locale: str = ""  # skip unless the host has this locale


REAL_ROOT = {"HOME": "/root"}  # bwrap_argv.sh's HOME=/root, read from the host
FULL_HOME = (
    "home/.claude/",
    "home/.claude.json",
    "home/.cache/",
    "home/.config/gh/",
    "home/.config/glab-cli/",
    "home/.config/Code/",
    "home/.local/share/helm/",
    "home/.local/bin/uv",
    "home/.local/bin/uvx",
    "home/.local/bin/claude",
)
SKILLS = (
    "shipped/alpha/SKILL.md",
    "shipped/beta/SKILL.md",
    "shipped/beta/scripts/",
    "shipped/.hidden/",
    "shipped/not-a-skill",
    "home/.claude/",
    "home/.codex/",
    "home/.pi/",
    "home/.claude.json",
)
SHARED = (
    "home/.agents/skills/",
    "home/.agents/plugins/",
    "home/.claude/",
    "home/.codex/",
    "home/.pi/",
    "home/.claude.json",
)
CODEX_REAL = PROFILES["codex"].real
PI_REAL = PROFILES["pi"].real

SCENARIOS = [
    Scenario(
        "vanilla",
        "1, 6, 7-default, 12-default-no-bind",
        REAL_ROOT,
        workspace="/workspaces/foo",
    ),
    Scenario("empty-workspace", "2", REAL_ROOT, workspace=""),
    Scenario(
        "missing-workspace", "3", REAL_ROOT, workspace="/srv/weird-workspace-path"
    ),
    Scenario("home-partial", "4a", tree=("home/.claude/", "home/.config/gh/")),
    Scenario("home-full", "4b", tree=FULL_HOME),
    Scenario(
        "home-files-as-dirs",
        "4b (a file bind needs a regular file: -f, not -e)",
        tree=("home/.claude/", "home/.claude.json/", "home/.local/bin/uv/"),
    ),
    Scenario(
        "workspace-is-file",
        "3 (a workspace bind needs a directory: -d, not -e)",
        tree=("home/", "ws-file"),
        workspace="{root}/ws-file",
    ),
    Scenario(
        "pass-through",
        "5",
        {**REAL_ROOT, "TERM": "xterm-256color", "LANG": "en_US.UTF-8"},
        workspace="/workspaces/foo",
    ),
    Scenario(
        "pass-through-all",
        "5 (every name in the list)",
        {
            "TERM": "dumb",
            **{v: "C.UTF-8" for v in ("LC_ALL", "LC_CTYPE", "LC_MESSAGES", "LC_TIME")},
            **{v: "C" for v in ("LC_COLLATE", "LC_NUMERIC", "LC_MONETARY")},
            "UV_PROJECT_ENVIRONMENT": "/p",
            "UV_CACHE_DIR": "/c",
            "UV_PYTHON_CACHE_DIR": "/pc",
            "PRE_COMMIT_HOME": "/pre",
            "CLAUDE_SANDBOX_WORKSPACE_ROOT": "/ws",
            "LANG": "",  # set but empty: not forwarded
        },
    ),
    Scenario(
        "chrome-strip",
        "7-strip",
        REAL_ROOT,
        workspace="/workspaces/foo",
        args=("--chrome", "--version"),
    ),
    Scenario("venv", "8b", {"VIRTUAL_ENV": "{root}/venv"}, tree=("home/", "venv/bin/")),
    Scenario(
        "venv-without-bin",
        "8b (no bin/: not on PATH)",
        {"VIRTUAL_ENV": "{root}/venv"},
        tree=("home/", "venv/"),
    ),
    Scenario(
        "uv-dirs",
        "8c",
        {"UV_PYTHON_INSTALL_DIR": "/opt/uv/python", "UV_TOOL_DIR": "/cache/uv-tools"},
    ),
    Scenario("no-forge", "9", {"CLAUDE_SANDBOX_NO_FORGE": "1"}, tree=FULL_HOME),
    Scenario(
        "allow-write-single",
        "10a",
        {"CLAUDE_SANDBOX_ALLOW_WRITE": "{root}/home/extra-rw"},
        tree=("home/extra-rw/",),
    ),
    Scenario(
        "allow-write-multi",
        "10b",
        {"CLAUDE_SANDBOX_ALLOW_WRITE": "{root}/home/extra-rw\n\n{root}/home/extra-rw2"},
        tree=("home/extra-rw/", "home/extra-rw2/"),
    ),
    Scenario(
        "allow-write-absent", "10c", {"CLAUDE_SANDBOX_ALLOW_WRITE": "/nonexistent/path"}
    ),
    Scenario(
        "allow-write-fifo",
        "10d, 10-mask-before-bind",
        {"CLAUDE_SANDBOX_ALLOW_WRITE": "{root}/home/extra.fifo"},
        tree=("home/extra.fifo|fifo",),
    ),
    Scenario(
        "allow-write-socket",
        "10e (socket)",
        {"CLAUDE_SANDBOX_ALLOW_WRITE": "{root}/home/podman.sock"},
        tree=("home/podman.sock|sock",),
    ),
    Scenario(
        "allow-write-relative-and-dangling",
        "10c (relative to the cwd; dangling symlink skipped)",
        {"CLAUDE_SANDBOX_ALLOW_WRITE": "rel\n{root}/dangling"},
        tree=("home/", "rel/", "dangling -> {root}/nowhere"),
    ),
    Scenario(
        "pass-env-single",
        "10p (plus duplicates, which are not merged)",
        {
            "CLAUDE_SANDBOX_PASS_ENV": "DOCKER_HOST,DOCKER_HOST TERM",
            "DOCKER_HOST": "unix:///run/user/1000/podman/podman.sock",
            "TERM": "xterm",
        },
    ),
    Scenario(
        "pass-env-separators",
        "10q",
        {
            "CLAUDE_SANDBOX_PASS_ENV": "FOO_A, FOO_B\nFOO_C,,\tFOO_D",
            "FOO_A": "a",
            "FOO_B": "b",
            "FOO_C": "c",
            "FOO_D": "d",
        },
    ),
    Scenario(
        "pass-env-unset", "10r", {"CLAUDE_SANDBOX_PASS_ENV": "DEFINITELY_UNSET_VAR"}
    ),
    Scenario(
        "pass-env-deny",
        "10s, 14d, 14e",
        {
            "CLAUDE_SANDBOX_PASS_ENV": (
                "PATH,HOME,USER,IS_SANDBOX,GIT_CONFIG_GLOBAL,GIT_CONFIG_SYSTEM,LD_PRELOAD,"
                "LD_LIBRARY_PATH,BASH_ENV,ENV,SHELLOPTS,BASHOPTS,IFS,CODEX_HOME,"
                "CLAUDE_CONFIG_DIR,IS_SANDBOX_AGENT"
            ),
            "PATH": "/evil/bin",
            "IS_SANDBOX": "0",
            "LD_PRELOAD": "/evil/x.so",
            "LD_LIBRARY_PATH": "/evil/lib",
            "BASH_ENV": "/evil/rc",
            "GIT_CONFIG_SYSTEM": "/evil/gitconfig",
            "CODEX_HOME": "/tmp/evil",
            "CLAUDE_CONFIG_DIR": "/tmp/evil",
            "IS_SANDBOX_AGENT": "codex",
            "USER": "evil",
        },
    ),
    Scenario(
        "pass-env-agent-override",
        "14d",
        {
            **REAL_ROOT,
            "CODEX_HOME": "/tmp/evil",
            "CLAUDE_SANDBOX_AGENT": "codex",
            "CLAUDE_SANDBOX_PASS_ENV": "CODEX_HOME,CLAUDE_SANDBOX_AGENT",
        },
        workspace="/workspaces/foo",
        real="/usr/libexec/claude-sandbox/codex",
    ),
    Scenario(
        "pass-env-junk-names",
        "10t",
        {
            "CLAUDE_SANDBOX_PASS_ENV": "9BAD,has-dash,has.dot,ok_name,_under",
            "ok_name": "fine",
            "_under": "u",
        },
    ),
    Scenario(
        "pass-env-glob",
        "none: a known divergence (KNOWN_DIVERGENCES)",
        {"CLAUDE_SANDBOX_PASS_ENV": "FOO_GLOB_*,NOMATCH_*", "FOO_GLOB_X": "leaked"},
        tree=("home/", "FOO_GLOB_X"),
    ),
    Scenario(
        "device",
        "11 device uses dev-bind",
        {"CLAUDE_SANDBOX_ALLOW_DEVICES": "/dev/zero\n\n/dev/null"},
    ),
    *(
        Scenario(
            f"device-rejected-{i}",
            "11 invalid sandbox device",
            {"CLAUDE_SANDBOX_ALLOW_DEVICES": dev},
        )
        for i, dev in enumerate(
            (
                "/dev",
                "/dev/pts",
                "/etc/passwd",
                "/dev/../etc/passwd",
                "/dev/no-such-claude-device",
                "/dev/null/",
                "/dev/zero/..",
                # Python 3.11's strict realpath resolved this to /dev/null.
                "/dev/zero/../null",
            )
        )
    ),
    Scenario(
        "device-symlink-outside-dev",
        "11 invalid sandbox device (a link from outside /dev)",
        {"CLAUDE_SANDBOX_ALLOW_DEVICES": "{root}/zlink"},
        tree=("home/", "zlink -> /dev/zero"),
    ),
    Scenario(
        "gpu",
        "11 GPU keeps private dev / available GPU device bound",
        {"CLAUDE_SANDBOX_GPU": "1"},
    ),
    Scenario(
        "resolv-override",
        "12-resolv-bind",
        {"CLAUDE_SANDBOX_JAIL_RESOLV": "{root}/resolv.conf"},
        tree=("home/", "resolv.conf"),
    ),
    Scenario(
        "resolv-missing",
        "12-missing-no-bind",
        {"CLAUDE_SANDBOX_JAIL_RESOLV": "/nonexistent/resolv.conf"},
    ),
    Scenario(
        "codex",
        "14b, 14c",
        tree=("home/.codex/", "home/.claude/", "home/.cache/", "home/.claude.json"),
        agent="codex",
        real=CODEX_REAL,
        args=("--chrome", "exec"),
    ),
    Scenario(
        "is-sandbox-agent",
        "14e",
        {
            **REAL_ROOT,
            "IS_SANDBOX_AGENT": "codex",
            "CLAUDE_SANDBOX_PASS_ENV": "IS_SANDBOX_AGENT",
        },
        workspace="/workspaces/foo",
    ),
    Scenario("skills-claude", "15 (claude), 15-ro, 15-tree, 15-pure", tree=SKILLS),
    Scenario("skills-codex", "15-codex", tree=SKILLS, agent="codex", real=CODEX_REAL),
    Scenario("skills-pi", "15-pi", tree=SKILLS, agent="pi", real=PI_REAL),
    Scenario("skills-none", "15-none", tree=("home/.claude/",)),
    Scenario(
        "skills-locale-order",
        "none: a known divergence (KNOWN_DIVERGENCES)",
        {"LC_ALL": "en_US.UTF-8"},
        tree=("home/.claude/", "shipped/alpha/SKILL.md", "shipped/Beta/SKILL.md"),
        locale="en_US.utf8",
    ),
    Scenario("shared-claude", "16 (claude)", tree=SHARED),
    Scenario("shared-codex", "16 (codex)", tree=SHARED, agent="codex", real=CODEX_REAL),
    Scenario(
        "shared-pi", "16 (pi), 16-isolation", tree=SHARED, agent="pi", real=PI_REAL
    ),
    Scenario("shared-absent", "16-absent, 16-pure"),
    Scenario(
        "verify",
        "none: --sandbox-verify runs the battery",
        verify=True,
        args=("--chrome",),
    ),
    Scenario(
        "local-model-port",
        "none: Pi's discovery port",
        {"CLAUDE_SANDBOX_LOCAL_MODEL_PORT": "1920"},
    ),
    Scenario(
        "gitconfig-path",
        "none: the path is a parameter; the environment cannot move it",
        {"CLAUDE_SANDBOX_GITCONFIG_PATH": "/evil/gitconfig"},
        gitconfig="/x/gitconfig",
    ),
    Scenario(
        "home-unset",
        "none: HOME falls back to /root",
        {"HOME": None},
        workspace="/workspaces/foo",
    ),
    Scenario(
        "awkward-args",
        "none: arguments survive as array elements",
        args=("", "a b", "line\nbreak", "--", "--chrome=x"),
    ),
    Scenario(
        "conf-end-to-end",
        "11 (parse_config feeding the builder)",
        {"CLAUDE_SANDBOX_GPU": "0", "DOCKER_HOST": "tcp://d"},
        tree=("home/", "extra/"),
        conf=(
            "workspace-root = /from/conf\nno-forge\nallow-write = {root}/extra\n"
            "pass-env = DOCKER_HOST\ngpu\nlocal-model-port\n"
        ),
    ),
]


def _before(argv: list[str], first: str, then: str) -> bool:
    """The first element ending in ``first`` precedes the one ending ``then``."""
    ends = [next(i for i, a in enumerate(argv) if a.endswith(e)) for e in (first, then)]
    return ends[0] < ends[1]


@dataclass(frozen=True)
class Divergence:
    """A known, deliberate difference between the bash and the port.

    ``bash_shows`` and ``python_shows`` say what each side's outcome (an argv,
    or a config report) looks like. ``same_after`` removes exactly the
    difference, and the rest of the two outcomes must then be identical, so
    a regression elsewhere in a divergent scenario still fails. Unless
    ``racy``, the outcomes must also differ, so an entry fails once the bash
    is fixed and can then be deleted.
    """

    why: str
    bash_shows: Callable[[list[str]], bool]
    python_shows: Callable[[list[str]], bool]
    same_after: Callable[[list[str]], list[str]]
    racy: bool = False  # the bash outcome is not deterministic


def _drop_setenv(name: str) -> Callable[[list[str]], list[str]]:
    """Remove every ``--setenv NAME VALUE`` triple."""

    def drop(argv: list[str]) -> list[str]:
        out: list[str] = []
        i = 0
        while i < len(argv):
            if argv[i] == "--setenv" and argv[i + 1 : i + 2] == [name]:
                i += 3
                continue
            out.append(argv[i])
            i += 1
        return out

    return drop


def _drop_lines(*prefixes: str) -> Callable[[list[str]], list[str]]:
    """Remove the report lines starting with any of ``prefixes``."""
    return lambda report: [r for r in report if not r.startswith(prefixes)]


# Every known divergence, keyed by the scenario or conf case that shows it.
# Everywhere else the outcomes must be identical.
#
# Divergences no scenario can show on CI, recorded here so the list is whole:
# - uutils realpath (Ubuntu 25.10+) accepts /dev/zero/../null for
#   allow-device; GNU and the port refuse it (the harness runs GNU).
# - The bash's pass-env reads shell variables as well as the environment
#   (PPID, RANDOM, HOSTNAME, the shadow's own globals); the port reads only
#   the environment, apart from TERM=dumb (bwrap.SHELL_DEFAULTS).
KNOWN_DIVERGENCES: dict[str, Divergence] = {
    "pass-env-glob": Divergence(
        "The bash expands each pass-env word as a glob against the cwd, the "
        "jail-writable workspace; the port only splits (config.words).",
        bash_shows=lambda argv: "FOO_GLOB_X" in argv,
        python_shows=lambda argv: "FOO_GLOB_X" not in argv,
        same_after=_drop_setenv("FOO_GLOB_X"),
    ),
    "ports-glob": Divergence(
        "The bash expands each local-port and callback-port word as a glob "
        "against the cwd; the port only splits, so `1?` stays invalid.",
        bash_shows=lambda report: "local_ports=12" in report,
        python_shows=lambda report: "local_ports=1?" in report,
        same_after=_drop_lines(
            "local_ports=", "local_model_enabled=", "validate_local_model_port="
        ),
    ),
    "ports-overlap-race": Divergence(
        "The bash checks callback/local overlap with `local_ports | grep -qx` "
        "under pipefail; when grep matches early in a long list the writer "
        "dies of SIGPIPE and the overlap goes unreported. The port always "
        "reports it.",
        bash_shows=lambda report: True,
        python_shows=lambda report: any(
            "1455 is listed as both" in line for line in report
        ),
        same_after=_drop_lines("validate_callback_ports="),
        racy=True,
    ),
    "skills-locale-order": Divergence(
        "The bash orders glob results (shipped skills, GPU nodes) by the "
        "launching locale's collation; the port uses code-point order on "
        "every host. Only the order of binds to distinct paths differs.",
        bash_shows=lambda argv: _before(argv, "/alpha", "/Beta"),
        python_shows=lambda argv: _before(argv, "/Beta", "/alpha"),
        same_after=sorted,
    ),
}


def compare(name: str, py: list[str], sh: list[str]) -> None:
    """Assert ``py`` matches ``sh``, or shows the divergence registered."""
    known = KNOWN_DIVERGENCES.get(name)
    if known is None:
        assert py == sh
        return
    assert known.python_shows(py), py
    assert known.same_after(py) == known.same_after(sh)
    if not known.racy:
        assert known.bash_shows(sh), sh
        assert py != sh


def py_outcome(sc: Scenario, root: Path, env: dict[str, str]) -> list[str]:
    if sc.conf is not None:
        env = parse_config(str(root / "sandbox.conf"), env)
    try:
        return bwrap_argv(
            agent_profile(sc.agent),
            Config.from_env(env),
            env,
            sc.workspace.replace("{root}", str(root)),
            sc.real,
            sc.args,
            verify=sc.verify,
            shipped_skills_dir=str(root / "shipped"),
            gitconfig_path=sc.gitconfig,
        )
    except SandboxError as e:
        return [REFUSED, str(e)]


def sh_outcome(sc: Scenario, root: Path, env: dict[str, str]) -> list[str]:
    conf = str(root / "sandbox.conf") if sc.conf is not None else ""
    proc = driver(
        root,
        env,
        "argv",
        sc.agent,
        str(root / "shipped"),
        conf,
        "1" if sc.verify else "0",
        sc.gitconfig,
        sc.workspace.replace("{root}", str(root)),
        sc.real,
        *sc.args,
    )
    if proc.returncode:
        return [REFUSED, os.fsdecode(proc.stderr).rstrip("\n")]
    return nul_split(proc.stdout)


@pytest.mark.parametrize("sc", SCENARIOS, ids=[s.name for s in SCENARIOS])
def test_argv_matches_bash(
    sc: Scenario, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bash_path: str
) -> None:
    if sc.locale and sc.locale not in installed_locales():
        pytest.skip(f"needs the {sc.locale} locale")
    build_tree(tmp_path, sc.tree)
    if sc.conf is not None:
        (tmp_path / "sandbox.conf").write_text(sc.conf.replace("{root}", str(tmp_path)))
    env = scenario_env(tmp_path, sc.env, bash_path)
    monkeypatch.chdir(tmp_path)
    before = snapshot(tmp_path)

    expected = sh_outcome(sc, tmp_path, env)
    compare(sc.name, py_outcome(sc, tmp_path, env), expected)
    # Both builders are pure: neither created anything (15-pure, 16-pure).
    assert snapshot(tmp_path) == before


@dataclass(frozen=True)
class ConfCase:
    name: str
    maps: str
    conf: str | None  # None: no conf file at all
    env: Mapping[str, str] = field(default_factory=dict[str, str])
    tree: tuple[str, ...] = ()


CONF_CASES = [
    ConfCase(
        "workspace-root",
        "11-workspace-root, 8-override",
        "workspace-root = /custom/root\n",
    ),
    ConfCase("no-forge", "11-no-forge", "no-forge\n"),
    ConfCase(
        "no-forge-0", "none: any value means 1 (reported quirk)", "no-forge = 0\n"
    ),
    ConfCase(
        "devices",
        "11 devices-config",
        "gpu\nallow-device = /dev/null\nallow-device = /dev/zero\n",
        {"CLAUDE_SANDBOX_ALLOW_DEVICES": "/dev/full"},
    ),
    ConfCase("gpu-env-wins", "11 gpu-env-wins", "gpu\n", {"CLAUDE_SANDBOX_GPU": "0"}),
    ConfCase(
        "allow-write-single", "11-allow-write-single", "allow-write = /some/path\n"
    ),
    ConfCase(
        "allow-write-multi",
        "11-allow-write-multi",
        "allow-write = /path/one\nallow-write = /path/two\n",
    ),
    ConfCase("pass-env-single", "11-pass-env-single", "pass-env = DOCKER_HOST\n"),
    ConfCase(
        "pass-env-multi",
        "11-pass-env-multi",
        "pass-env = A_VAR, B_VAR\npass-env = C_VAR\n",
    ),
    ConfCase("egress-default-on", "11-egress-jail-default-on", "no-forge\n"),
    ConfCase("egress-bare", "11-egress-jail-bare", "egress-jail\n"),
    ConfCase("egress-off", "11-egress-jail-conf-off", "egress-jail = 0\n"),
    ConfCase(
        "egress-env-off-wins",
        "11-egress-jail-env-off-wins",
        "egress-jail\n",
        {"CLAUDE_SANDBOX_EGRESS_JAIL": "0"},
    ),
    ConfCase("egress-predicate-default", "11-egress-jail-predicate-default", None),
    ConfCase(
        "egress-other-values", "none: any value but 0 means on", "egress-jail = off\n"
    ),
    ConfCase("allow-ip-single", "11-allow-ip-single", "allow-ip = 172.23.1.2\n"),
    ConfCase(
        "allow-ip-multi",
        "11-allow-ip-multi",
        "allow-ip = 172.23.1.2\nallow-ip = 10.0.5.6\n",
    ),
    ConfCase("allow-ip-empty", "11-allow-ip-empty", "allow-ip =\n"),
    ConfCase(
        "comments",
        "11-comments",
        "# comment\n\nworkspace-root = /from/conf\n# another\n",
    ),
    ConfCase(
        "env-wins",
        "11-env-wins",
        "workspace-root = /from/config\n",
        {"CLAUDE_SANDBOX_WORKSPACE_ROOT": "/from/env"},
    ),
    ConfCase("absent", "11-absent, 8-pwd", None),
    ConfCase(
        "lexing",
        "none: whitespace, CRLF, comments, empty keys, unknown keys, no final newline",
        "\t workspace-root\t=  /a b # trailing comment\r\n=orphan\nnot-a-key = 1\n"
        "allow-write=/x=y\nallow-write = #only a comment\nallow-ip = 1.2.3.4\x00\n"
        "callback-port = 1455",
    ),
    ConfCase(
        "empty-env-replaced",
        "none: an empty variable counts as unset",
        "workspace-root = /from/conf\nlocal-model-port = 0\n",
        {"CLAUDE_SANDBOX_WORKSPACE_ROOT": "", "CLAUDE_SANDBOX_LOCAL_MODEL_PORT": ""},
    ),
    ConfCase(
        "workspace-root-empty",
        "8-empty-override (falls back to $PWD); an empty value sets nothing",
        "workspace-root =\n",
        {"CLAUDE_SANDBOX_WORKSPACE_ROOT": ""},
    ),
    ConfCase(
        "ports",
        "none: the relay sets, deduplicated, env and conf merged",
        "local-model-port\nlocal-port = 8080\nlocal-port = 1920, 9000\n"
        "callback-port = 1455\n",
        {"CLAUDE_SANDBOX_LOCAL_PORTS": "7000,8080"},
    ),
    ConfCase(
        "ports-invalid",
        "none: every bad entry named by its key",
        "local-model-port = 01\nlocal-port = 0\nlocal-port = 65536 abc 65535 +1\n"
        "callback-port = 99999\ncallback-port = 1455\n",
    ),
    ConfCase(
        "ports-overlap",
        "none: a port relayed both ways",
        # The overlapping port is the LAST local port: an earlier one races
        # SIGPIPE in the bash's `local_ports | grep -q` (reported).
        "local-model-port = 0\nlocal-port = 8080\nlocal-port = 1455\n"
        "callback-port = 1455\ncallback-port = 1455\n",
    ),
    ConfCase(
        "ports-overlap-race",
        "none: a known divergence (KNOWN_DIVERGENCES)",
        None,
        {
            "CLAUDE_SANDBOX_LOCAL_MODEL_PORT": "1455",
            "CLAUDE_SANDBOX_LOCAL_PORTS": ",".join(map(str, range(2000, 2400))),
            "CLAUDE_SANDBOX_CALLBACK_PORTS": "1455",
        },
    ),
    ConfCase(
        "ports-model-off",
        "none: local-model-port 0 drops the relay",
        "local-model-port = 0\n",
    ),
    ConfCase(
        "ports-glob",
        "none: a known divergence (KNOWN_DIVERGENCES)",
        "local-port = 1?\ncallback-port = [x]\n",
        tree=("12",),
    ),
    ConfCase(
        "ports-dedup-substring",
        "none: dedup is substring containment (reported quirk)",
        None,
        {"CLAUDE_SANDBOX_LOCAL_MODEL_PORT": "1 2", "CLAUDE_SANDBOX_LOCAL_PORTS": "1 2"},
    ),
]


def py_conf_report(path: str, env: Mapping[str, str], pwd: str) -> list[str]:
    merged = parse_config(path, env)
    cfg = Config.from_env(merged)
    report = [f"env {k}={merged[k]}" for k in KNOBS if k in merged]
    local_errors = validate_local_model_port(cfg)
    callback_errors = validate_callback_ports(cfg)
    return [
        *report,
        f"workspace_root={resolve_workspace_root(cfg, pwd)}",
        f"egress_jail_enabled={int(egress_jail_enabled(cfg))}",
        "local_ports=" + "\n".join(local_ports(cfg)),
        f"local_model_enabled={int(local_model_enabled(cfg))}",
        "callback_ports=" + "\n".join(callback_ports(cfg)),
        f"callback_enabled={int(callback_enabled(cfg))}",
        f"validate_local_model_port={int(bool(local_errors))} "
        + "\n".join(local_errors),
        f"validate_callback_ports={int(bool(callback_errors))} "
        + "\n".join(callback_errors),
    ]


@pytest.mark.parametrize("case", CONF_CASES, ids=[c.name for c in CONF_CASES])
def test_config_matches_bash(
    case: ConfCase, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    build_tree(tmp_path, case.tree)
    conf = tmp_path / "sandbox.conf"
    if case.conf is not None:
        conf.write_bytes(case.conf.encode())
    env = {"PATH": SYSTEM_PATH, **case.env}
    monkeypatch.chdir(tmp_path)

    proc = driver(tmp_path, env, "config", str(conf), "/work/pwd")
    assert proc.returncode == 0, proc.stderr
    py = py_conf_report(str(conf), env, "/work/pwd")
    compare(case.name, py, nul_split(proc.stdout))


# A divergence entry that names no scenario would silently check nothing.
_unmatched = (
    set(KNOWN_DIVERGENCES) - {s.name for s in SCENARIOS} - {c.name for c in CONF_CASES}
)
if _unmatched:
    raise AssertionError(f"KNOWN_DIVERGENCES names no scenario: {_unmatched}")
