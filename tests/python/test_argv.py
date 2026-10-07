"""The bwrap argv as a contract with bwrap (bwrap.py).

The cases of the bash argv suite this replaced, by scenario number, and of
the comparison harness that checked the Python builder against the bash one.
Assertions are on what bwrap is told (an element, an adjacent pair, an
order), never on a whole argv: what the host has under /run, /etc and /dev
differs between machines, and test_units.py pins those branches with a fake
host.
"""

import os
import socket
from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest

from claude_sandbox.bwrap import (
    ENTRY_GUARD_ENV,
    ENTRY_POINTS,
    GITCONFIG_PATH,
    Built,
    bwrap_build,
)
from claude_sandbox.config import Config, parse_config
from claude_sandbox.errors import SandboxError
from claude_sandbox.profiles import LIBEXEC, PROFILES, VERIFY_BATTERY

REAL = "/test/.local/bin/claude"
SYSTEM_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
CODEX_REAL = PROFILES["codex"].real
PI_REAL = PROFILES["pi"].real


def build(
    env: Mapping[str, str],
    workspace: str = "/workspaces/foo",
    real: str = REAL,
    args: Sequence[str] = (),
    *,
    agent: str = "claude",
    skills: str = "/nonexistent/skills",
    verify: bool = False,
    gitconfig: str = GITCONFIG_PATH,
) -> list[str]:
    return bwrap_build(
        PROFILES[agent],
        Config.from_env(env),
        env,
        workspace,
        real,
        args,
        verify=verify,
        shipped_skills_dir=skills,
        gitconfig_path=gitconfig,
        state_dir="/nonexistent/state",
    ).argv


def pair(argv: list[str], first: str, then: str) -> bool:
    """``first`` immediately followed by ``then`` somewhere in ``argv``."""
    return any(argv[i : i + 2] == [first, then] for i in range(len(argv) - 1))


def setenv(argv: list[str], name: str) -> list[str]:
    """The values of every ``--setenv NAME VALUE``."""
    return [
        argv[i + 2]
        for i in range(len(argv) - 2)
        if argv[i : i + 2] == ["--setenv", name]
    ]


def binds(argv: list[str], flag: str = "--bind") -> list[str]:
    """The sources of every ``FLAG SRC DST``."""
    return [argv[i + 1] for i in range(len(argv) - 2) if argv[i] == flag]


def tree(root: Path, *entries: str) -> Path:
    """Create ``dir/`` or ``file`` entries under ``root``."""
    for entry in entries:
        path = root / entry
        path.parent.mkdir(parents=True, exist_ok=True)
        if entry.endswith("/"):
            path.mkdir(exist_ok=True)
        else:
            path.touch()
    return root


ROOT = {"HOME": "/root"}


# --- 1, 6, 7, 12: the vanilla launch ------------------------------------------


def test_vanilla() -> None:
    argv = build(ROOT)
    assert argv[0] == "bwrap"
    assert argv[1:4] == ["--ro-bind", "/", "/"]
    assert pair(argv, "--dev", "/dev")
    assert pair(argv, "--ro-bind", "/proc")  # unconditional
    assert pair(argv, "--cap-drop", "ALL")
    for flag in (
        "--unshare-user-try",
        "--unshare-pid",
        "--unshare-ipc",
        "--unshare-uts",
        "--unshare-cgroup-try",
        "--die-with-parent",
        "--clearenv",
    ):
        assert flag in argv
    # No --new-session (script(1) wraps the session instead); no --proc.
    assert "--new-session" not in argv and "--proc" not in argv
    assert setenv(argv, "PATH") == [f"{SYSTEM_PATH}:/root/.local/bin"]
    assert setenv(argv, "HOME") == ["/root"]
    assert setenv(argv, "USER") == ["root"]
    assert setenv(argv, "IS_SANDBOX") == ["1"]
    assert setenv(argv, "IS_SANDBOX_AGENT") == ["claude"]
    assert setenv(argv, "GIT_CONFIG_GLOBAL") == [GITCONFIG_PATH]
    assert setenv(argv, "GIT_CONFIG_SYSTEM") == ["/dev/null"]
    # The real binary, off PATH on the host, at its conventional path inside;
    # read-only, so a session cannot rewrite what later sessions run.
    i = argv.index("/root/.local/bin/claude")
    assert argv[i - 2 : i] == ["--ro-bind", REAL]
    # 7: --no-chrome straight after the command.
    assert argv[-3:] == ["--", "/root/.local/bin/claude", "--no-chrome"]
    # 6: defence-in-depth masks.
    for name in (".netrc", ".Xauthority", ".ICEauthority"):
        assert pair(argv, "/dev/null", f"/root/{name}")
    # 12: no resolver override unless one was staged.
    assert "/etc/resolv.conf" not in argv
    # The vendor's standalone package is never mounted into a session.
    assert not any("packages/standalone" in a for a in argv)


@pytest.mark.parametrize("workspace", ["", "/srv/weird-workspace-path"])
def test_no_workspace_bind(workspace: str) -> None:
    """2, 3: an empty or missing workspace binds nothing, and the rest
    stands."""
    argv = build(ROOT, workspace)
    assert "--clearenv" in argv
    assert binds(argv) == []


def test_a_file_is_not_a_workspace(tmp_path: Path) -> None:
    tree(tmp_path, "home/", "ws-file")
    argv = build({"HOME": str(tmp_path / "home")}, str(tmp_path / "ws-file"))
    assert str(tmp_path / "ws-file") not in argv


# --- 4: the bind-back over $HOME ----------------------------------------------


def test_home_partial(tmp_path: Path) -> None:
    home = tree(tmp_path, ".claude/", ".config/gh/")
    argv = build({"HOME": str(home)}, str(home))
    assert binds(argv) == [f"{home}/.claude", f"{home}/.config/gh", str(home)]
    for absent in (
        ".cache",
        ".config/glab-cli",
        ".local/share",
        ".local/share/applications",
        ".local/share/claude",
        ".claude.json",
        ".local/bin/uv",
        ".local/bin/uvx",
    ):
        assert f"{home}/{absent}" not in argv
    # Unconditional: the destination is on the in-sandbox tmpfs $HOME.
    assert f"{home}/.local/bin/claude" in argv


def test_home_full(tmp_path: Path) -> None:
    home = tree(
        tmp_path,
        ".claude/",
        ".claude.json",
        ".cache/",
        ".config/gh/",
        ".config/glab-cli/",
        ".config/Code/",
        ".local/share/helm/",
        ".local/bin/uv",
        ".local/bin/uvx",
        ".local/bin/claude",
    )
    argv = build({"HOME": str(home)}, str(home))
    rw = [
        f"{home}/{r}" for r in (".claude", ".cache", ".config/gh", ".config/glab-cli")
    ]
    rw += [f"{home}/.local/share"]
    rw += [f"{home}/{r}" for r in (".claude.json", ".local/bin/uv", ".local/bin/uvx")]
    assert binds(argv) == [*rw, str(home)]
    # .local/share is bulk-bound, with two sub-dirs masked after it.
    share = argv.index(f"{home}/.local/share")
    for sub in ("applications", "claude"):
        i = argv.index(f"{home}/.local/share/{sub}")
        assert argv[i - 1] == "--tmpfs" and i > share
    # Only the explicit .config allowlist is exposed.
    assert f"{home}/.config/Code" not in argv


def test_file_binds_need_regular_files(tmp_path: Path) -> None:
    home = tree(tmp_path, ".claude/", ".claude.json/", ".local/bin/uv/")
    argv = build({"HOME": str(home)}, "")
    assert f"{home}/.claude.json" not in argv
    assert f"{home}/.local/bin/uv" not in argv


# --- 5, 8b, 8c: the environment the agent gets --------------------------------


def test_pass_through() -> None:
    argv = build({**ROOT, "TERM": "xterm-256color", "LANG": "en_US.UTF-8"})
    assert setenv(argv, "TERM") == ["xterm-256color"]
    assert setenv(argv, "LANG") == ["en_US.UTF-8"]


def test_pass_through_every_name() -> None:
    env = {
        "HOME": "/h",
        "TERM": "dumb",
        "LC_ALL": "C.UTF-8",
        "LC_CTYPE": "C.UTF-8",
        "LC_MESSAGES": "C.UTF-8",
        "LC_TIME": "C.UTF-8",
        "LC_COLLATE": "C",
        "LC_NUMERIC": "C",
        "LC_MONETARY": "C",
        "UV_PROJECT_ENVIRONMENT": "/p",
        "UV_CACHE_DIR": "/c",
        "UV_PYTHON_CACHE_DIR": "/pc",
        "PRE_COMMIT_HOME": "/pre",
        "CLAUDE_SANDBOX_WORKSPACE_ROOT": "/ws",
        "LANG": "",  # set but empty: not forwarded
    }
    argv = build(env)
    for name, value in env.items():
        if name != "HOME":
            assert setenv(argv, name) == ([value] if value else []), name
    # bash's own default when TERM is unset, kept from the bash shadow.
    assert setenv(build({"HOME": "/h"}), "TERM") == ["dumb"]


def test_venv_bin_is_appended_to_path(tmp_path: Path) -> None:
    """8b: a venv binary cannot shadow `claude` or a system tool."""
    tree(tmp_path, "home/", "venv/bin/", "bare/")
    env = {"HOME": str(tmp_path / "home"), "VIRTUAL_ENV": str(tmp_path / "venv")}
    argv = build(env)
    home_bin = f"{tmp_path}/home/.local/bin"
    assert setenv(argv, "PATH") == [f"{SYSTEM_PATH}:{home_bin}:{tmp_path}/venv/bin"]
    assert setenv(argv, "VIRTUAL_ENV") == [str(tmp_path / "venv")]
    # No bin/: nothing to append, but the variable still goes through.
    argv = build({**env, "VIRTUAL_ENV": str(tmp_path / "bare")})
    assert setenv(argv, "PATH") == [f"{SYSTEM_PATH}:{home_bin}"]
    assert setenv(argv, "VIRTUAL_ENV") == [str(tmp_path / "bare")]


def test_uv_dirs_reach_the_jail() -> None:
    """8c: the image's baked interpreter and tool dir, so uv neither
    re-downloads nor re-installs into the ephemeral home every session."""
    store = "/usr/libexec/claude-sandbox/python"
    env = {**ROOT, "UV_PYTHON_INSTALL_DIR": store}
    argv = build({**env, "UV_TOOL_DIR": "/cache/uv-tools"})
    assert setenv(argv, "UV_PYTHON_INSTALL_DIR") == [store]
    assert setenv(argv, "UV_TOOL_DIR") == ["/cache/uv-tools"]


def test_home_unset_falls_back_to_root() -> None:
    assert setenv(build({}), "HOME") == ["/root"]


def test_the_git_config_path_is_a_parameter() -> None:
    """The environment cannot move the git config the jail reads."""
    argv = build(
        {**ROOT, "CLAUDE_SANDBOX_GITCONFIG_PATH": "/evil/gitconfig"},
        gitconfig="/x/gitconfig",
    )
    assert setenv(argv, "GIT_CONFIG_GLOBAL") == ["/x/gitconfig"]
    assert "/evil/gitconfig" not in argv


def test_local_model_port_reaches_pi_discovery() -> None:
    argv = build({**ROOT, "CLAUDE_SANDBOX_LOCAL_MODEL_PORT": "1920"})
    assert setenv(argv, "CLAUDE_SANDBOX_LOCAL_MODEL_PORT") == ["1920"]
    assert setenv(build(ROOT), "CLAUDE_SANDBOX_LOCAL_MODEL_PORT") == []


# --- 7: arguments -------------------------------------------------------------


def test_chrome_is_stripped_and_other_args_kept() -> None:
    argv = build(ROOT, args=["--chrome", "--version"])
    assert argv[-3:] == ["/root/.local/bin/claude", "--no-chrome", "--version"]
    assert "--chrome" not in argv


def test_awkward_args_survive_as_elements() -> None:
    args = ["", "a b", "line\nbreak", "--", "--chrome=x"]
    assert build(ROOT, args=args)[-5:] == args


def test_verify_runs_the_battery() -> None:
    argv = build(ROOT, args=["--chrome", "x"], verify=True)
    assert argv[argv.index("--") :] == ["--", "/bin/bash", VERIFY_BATTERY, "x"]


# --- 9: no-forge --------------------------------------------------------------


def test_no_forge_omits_the_forge_token_dirs(tmp_path: Path) -> None:
    home = tree(tmp_path, ".claude/", ".cache/", ".config/gh/", ".config/glab-cli/")
    argv = build({"HOME": str(home), "CLAUDE_SANDBOX_NO_FORGE": "1"}, str(home))
    assert binds(argv) == [f"{home}/.claude", f"{home}/.cache", str(home)]


# --- 10: allow-write ----------------------------------------------------------


def allow_write(tmp_path: Path, value: str) -> list[str]:
    home = tree(tmp_path, "home/")
    return build({"HOME": str(home), "CLAUDE_SANDBOX_ALLOW_WRITE": value}, "")


def test_allow_write_binds_each_existing_path(tmp_path: Path) -> None:
    """One path per line, as the host launcher hands them over: a space is
    part of a path, a blank line is nothing, a missing path is skipped."""
    tree(tmp_path, "a/", "b c/")
    value = f"{tmp_path}/a\n\n{tmp_path}/b c\n/nonexistent/path"
    assert binds(allow_write(tmp_path, value)) == [f"{tmp_path}/a", f"{tmp_path}/b c"]


def test_allow_write_skips_a_dangling_link(tmp_path: Path) -> None:
    (tmp_path / "dangling").symlink_to(tmp_path / "nowhere")
    assert binds(allow_write(tmp_path, f"{tmp_path}/dangling")) == []


@pytest.mark.parametrize("kind", ["fifo", "socket"])
def test_allow_write_binds_non_regular_files(tmp_path: Path, kind: str) -> None:
    """A unix socket is neither -d nor -f, and rootless podman's engine is
    one; and the bind comes after every mask, so it can re-expose a single
    path through one (a podman socket under a masked /run/user)."""
    path = tmp_path / "podman.sock"
    if kind == "fifo":
        os.mkfifo(path)
    else:
        with socket.socket(socket.AF_UNIX) as sock:
            sock.bind(str(path))
    argv = allow_write(tmp_path, str(path))
    assert binds(argv) == [str(path)]
    last_tmpfs = max(i for i, a in enumerate(argv) if a == "--tmpfs")
    assert argv.index(str(path)) > last_tmpfs


# --- 10p-10t, 14d, 14e: pass-env ----------------------------------------------


def test_pass_env_forwards_named_values() -> None:
    sock = "unix:///run/user/1000/podman/podman.sock"
    argv = build(
        {
            **ROOT,
            "CLAUDE_SANDBOX_PASS_ENV": "DOCKER_HOST,DOCKER_HOST TERM",
            "DOCKER_HOST": sock,
            "TERM": "xterm",
        }
    )
    # Duplicates are not merged; TERM also passes through on its own.
    assert setenv(argv, "DOCKER_HOST") == [sock, sock]
    assert setenv(argv, "TERM") == ["xterm", "xterm"]


def test_pass_env_separators_and_names() -> None:
    env = {
        **ROOT,
        "CLAUDE_SANDBOX_PASS_ENV": (
            "FOO_A, FOO_B\nFOO_C,,\tFOO_D,9BAD,has-dash,has.dot,_under,UNSET_VAR"
        ),
        **{f"FOO_{c}": c for c in "ABCD"},
        "_under": "u",
    }
    argv = build(env)
    for c in "ABCD":
        assert setenv(argv, f"FOO_{c}") == [c]
    assert setenv(argv, "_under") == ["u"]
    # Junk names are skipped, not emitted as a broken --setenv; an unset
    # one emits nothing.
    for name in ("9BAD", "has-dash", "has.dot", "UNSET_VAR"):
        assert name not in argv


def test_pass_env_is_never_globbed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The cwd is the jail-writable workspace: a planted file must not
    steer the next launch's names."""
    tree(tmp_path, "FOO_GLOB_X")
    monkeypatch.chdir(tmp_path)
    env = {**ROOT, "CLAUDE_SANDBOX_PASS_ENV": "FOO_GLOB_*", "FOO_GLOB_X": "leaked"}
    assert "FOO_GLOB_X" not in build(env)


def test_pass_env_cannot_override_the_sandbox() -> None:
    denied = (
        "PATH,HOME,USER,IS_SANDBOX,GIT_CONFIG_GLOBAL,GIT_CONFIG_SYSTEM,LD_PRELOAD,"
        "LD_LIBRARY_PATH,BASH_ENV,ENV,SHELLOPTS,BASHOPTS,IFS,CODEX_HOME,"
        "CLAUDE_CONFIG_DIR,IS_SANDBOX_AGENT,CLAUDE_SANDBOX_AGENT"
    )
    argv = build(
        {
            **ROOT,
            "CLAUDE_SANDBOX_PASS_ENV": denied,
            "PATH": "/evil/bin",
            "IS_SANDBOX": "0",
            "LD_PRELOAD": "/evil/x.so",
            "LD_LIBRARY_PATH": "/evil/lib",
            "BASH_ENV": "/evil/rc",
            "GIT_CONFIG_SYSTEM": "/evil/gitconfig",
            "CODEX_HOME": "/tmp/evil",
            "CLAUDE_CONFIG_DIR": "/tmp/evil",
            "IS_SANDBOX_AGENT": "codex",
            "CLAUDE_SANDBOX_AGENT": "codex",
            "USER": "evil",
        }
    )
    for name in denied.split(","):
        assert len(setenv(argv, name)) <= 1, name
    assert not any(a.startswith("/evil") or a == "/tmp/evil" for a in argv)
    for name in ("LD_PRELOAD", "LD_LIBRARY_PATH", "BASH_ENV", "SHELLOPTS"):
        assert name not in argv
    # The sandbox's own values survive the attempted override.
    assert setenv(argv, "PATH") == [f"{SYSTEM_PATH}:/root/.local/bin"]
    assert setenv(argv, "IS_SANDBOX") == ["1"]
    assert setenv(argv, "USER") == ["root"]
    assert setenv(argv, "IS_SANDBOX_AGENT") == ["claude"]
    assert setenv(argv, "GIT_CONFIG_SYSTEM") == ["/dev/null"]
    for name in ("CODEX_HOME", "CLAUDE_CONFIG_DIR", "CLAUDE_SANDBOX_AGENT"):
        assert name not in argv


# --- 11: devices and the conf feeding the builder ------------------------------


def test_devices_use_dev_bind_after_the_private_dev() -> None:
    argv = build({**ROOT, "CLAUDE_SANDBOX_ALLOW_DEVICES": "/dev/zero\n\n/dev/null"})
    assert binds(argv, "--dev-bind") == ["/dev/zero", "/dev/null"]
    assert argv.index("--dev") < argv.index("--dev-bind")


NOT_A_DEVICE = "claude-sandbox: allow-device needs a character or block device"


@pytest.mark.parametrize(
    ("device", "message"),
    [
        *(
            (d, f"{NOT_A_DEVICE} under /dev: {d}")
            for d in ("/dev", "/dev/pts", "/etc/passwd", "/dev/../etc/passwd")
        ),
        (
            "/dev/no-such-claude-device",
            "realpath: /dev/no-such-claude-device: No such file or directory",
        ),
        ("/dev/null/", "realpath: /dev/null/: Not a directory"),
        # GNU realpath refuses these; uutils (Ubuntu 25.10+) accepts them.
        ("/dev/zero/..", "realpath: /dev/zero/..: Not a directory"),
        # Python 3.11's strict realpath once resolved this to /dev/null.
        ("/dev/zero/../null", "realpath: /dev/zero/../null: Not a directory"),
    ],
)
def test_invalid_devices_are_refused(device: str, message: str) -> None:
    with pytest.raises(SandboxError) as e:
        build({**ROOT, "CLAUDE_SANDBOX_ALLOW_DEVICES": device})
    assert str(e.value) == message


def test_a_link_from_outside_dev_is_refused(tmp_path: Path) -> None:
    (tmp_path / "zlink").symlink_to("/dev/zero")
    with pytest.raises(SandboxError, match="under /dev: "):
        build({**ROOT, "CLAUDE_SANDBOX_ALLOW_DEVICES": str(tmp_path / "zlink")})


def test_gpu_keeps_the_private_dev() -> None:
    argv = build({**ROOT, "CLAUDE_SANDBOX_GPU": "1"})
    assert pair(argv, "--dev", "/dev")
    assert "/dev/net/tun" not in argv
    # Never the whole of /dev; only nodes under the GPU globs.
    for device in binds(argv, "--dev-bind"):
        assert device.startswith(("/dev/nvidia", "/dev/dri/")), device


def test_the_conf_feeds_the_builder(tmp_path: Path) -> None:
    tree(tmp_path, "home/.config/gh/", "extra/")
    conf = tmp_path / "sandbox.conf"
    conf.write_text(
        "workspace-root = /from/conf\nno-forge\n"
        f"allow-write = {tmp_path}/extra\npass-env = DOCKER_HOST\ngpu\n"
        "local-model-port\n"
    )
    launch = {
        "HOME": str(tmp_path / "home"),
        "CLAUDE_SANDBOX_GPU": "0",
        "DOCKER_HOST": "tcp://d",
    }
    argv = build(parse_config(str(conf), launch), "")
    assert binds(argv) == [f"{tmp_path}/extra"]  # no-forge: no gh
    assert setenv(argv, "DOCKER_HOST") == ["tcp://d"]
    assert setenv(argv, "CLAUDE_SANDBOX_WORKSPACE_ROOT") == ["/from/conf"]
    assert setenv(argv, "CLAUDE_SANDBOX_LOCAL_MODEL_PORT") == ["1920"]
    assert "--dev-bind" not in argv  # the environment's GPU=0 won


# --- 12: the egress jail's resolver --------------------------------------------


def test_a_staged_resolver_is_bound_over_resolv_conf(tmp_path: Path) -> None:
    resolv = tree(tmp_path, "resolv.conf") / "resolv.conf"
    argv = build({**ROOT, "CLAUDE_SANDBOX_JAIL_RESOLV": str(resolv)})
    i = argv.index(str(resolv))
    assert argv[i - 1 : i + 2] == ["--ro-bind", str(resolv), "/etc/resolv.conf"]
    argv = build({**ROOT, "CLAUDE_SANDBOX_JAIL_RESOLV": "/nonexistent/resolv.conf"})
    assert "/etc/resolv.conf" not in argv


# --- 14: one builder, three agents ---------------------------------------------


def test_codex_profile(tmp_path: Path) -> None:
    home = tree(tmp_path, ".codex/", ".claude/", ".cache/", ".claude.json")
    argv = build(
        {"HOME": str(home)}, str(home), CODEX_REAL, ["--chrome", "exec"], agent="codex"
    )
    # Exec'd in place from its root-owned package: read-only, no bind-back.
    launch = f"{LIBEXEC}/codex-launch"
    assert argv[argv.index("--") :] == ["--", launch, CODEX_REAL, "--chrome", "exec"]
    assert f"{home}/.local/bin/codex" not in argv
    # Its own login only: none of Claude's state, no --no-chrome.
    assert binds(argv) == [f"{home}/.codex", f"{home}/.cache", str(home)]
    for path in (".claude", ".claude.json", ".local/bin/claude"):
        assert f"{home}/{path}" not in argv
    assert "--no-chrome" not in argv
    # The vendor unpacks its binary inside ~/.codex: masked, after the bind.
    i = argv.index(f"{home}/.codex/packages")
    assert argv[i - 1] == "--tmpfs" and i > argv.index(f"{home}/.codex")
    # Every isolation primitive is the claude path's.
    for flag in (
        "--ro-bind",
        "--dev",
        "--tmpfs",
        "--unshare-user-try",
        "--unshare-pid",
        "--unshare-ipc",
        "--unshare-uts",
        "--unshare-cgroup-try",
        "--die-with-parent",
        "--clearenv",
    ):
        assert flag in argv
    assert pair(argv, "--cap-drop", "ALL")
    assert setenv(argv, "IS_SANDBOX") == ["1"]
    assert setenv(argv, "GIT_CONFIG_GLOBAL") == [GITCONFIG_PATH]
    # Whose session it is, for check 03; never the CLAUDE_SANDBOX_AGENT
    # override, or a nested `claude` would re-dispatch to codex.
    assert setenv(argv, "IS_SANDBOX_AGENT") == ["codex"]
    assert "CLAUDE_SANDBOX_AGENT" not in argv
    assert "--new-session" not in argv


# --- 15, 16: skills ------------------------------------------------------------

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


def snapshot(root: Path) -> list[Path]:
    return sorted(root.rglob("*"))


@pytest.mark.parametrize(
    ("agent", "real", "skills_rel"),
    [
        ("claude", REAL, ".claude/skills"),
        ("codex", CODEX_REAL, ".codex/skills"),
        ("pi", PI_REAL, ".pi/agent/skills"),
    ],
)
def test_shipped_skills_are_bound_read_only_per_skill(
    tmp_path: Path, agent: str, real: str, skills_rel: str
) -> None:
    tree(tmp_path, *SKILLS)
    before = snapshot(tmp_path)
    home, shipped = tmp_path / "home", tmp_path / "shipped"
    argv = build({"HOME": str(home)}, str(home), real, agent=agent, skills=str(shipped))
    # One read-only bind per skill directory, onto the agent's own dir.
    for name in ("alpha", "beta"):
        i = argv.index(f"{shipped}/{name}")
        dst = f"{home}/{skills_rel}/{name}"
        assert argv[i - 1 : i + 2] == ["--ro-bind", f"{shipped}/{name}", dst]
    assert f"{shipped}/alpha" not in binds(argv)  # never read-write
    assert f"{home}/{skills_rel}" not in argv  # per skill, not per tree
    assert not any(".hidden" in a or "not-a-skill" in a for a in argv)
    for other in {".claude/skills", ".codex/skills", ".pi/agent/skills"} - {skills_rel}:
        assert f"{home}/{other}/alpha" not in argv
    # The builder is pure: it created nothing on the host.
    assert snapshot(tmp_path) == before


def test_skills_come_in_code_point_order(tmp_path: Path) -> None:
    """Not the launching locale's collation: the binds are to distinct
    paths, so only their order could differ between hosts."""
    tree(tmp_path, "home/.claude/", "shipped/alpha/SKILL.md", "shipped/Beta/SKILL.md")
    env = {"HOME": str(tmp_path / "home"), "LC_ALL": "en_US.UTF-8"}
    argv = build(env, "", skills=str(tmp_path / "shipped"))
    assert argv.index(f"{tmp_path}/shipped/Beta") < argv.index(
        f"{tmp_path}/shipped/alpha"
    )


def test_no_shipped_skills(tmp_path: Path) -> None:
    home = tree(tmp_path, ".claude/")
    argv = build({"HOME": str(home)}, "", skills=str(tmp_path / "nonexistent"))
    assert not any(a.startswith(f"{home}/.claude/skills") for a in argv)


@pytest.mark.parametrize(
    ("agent", "real"), [("claude", REAL), ("codex", CODEX_REAL), ("pi", PI_REAL)]
)
def test_shared_agent_skills(tmp_path: Path, agent: str, real: str) -> None:
    """16 (ADR 25): ~/.agents/skills is read-write in every agent, and is
    the only part of ~/.agents bound."""
    home = tree(
        tmp_path,
        ".agents/skills/",
        ".agents/plugins/",
        ".claude/",
        ".codex/",
        ".pi/",
        ".claude.json",
    )
    argv = build({"HOME": str(home)}, "", real, agent=agent)
    shared = f"{home}/.agents/skills"
    i = argv.index(shared)
    assert argv[i - 1 : i + 2] == ["--bind", shared, shared]
    assert f"{home}/.agents" not in argv
    assert f"{home}/.agents/plugins" not in argv
    if agent == "pi":  # sharing skills does not share credentials
        assert f"{home}/.claude" not in argv and f"{home}/.codex" not in argv


def test_absent_shared_skills(tmp_path: Path) -> None:
    home = tree(tmp_path, ".claude/")
    argv = build({"HOME": str(home)}, "")
    assert f"{home}/.agents/skills" not in argv
    assert not (home / ".agents").exists()  # the builder made nothing


# --- the entry-point guard, on a real filesystem --------------------------------


@pytest.mark.parametrize(
    ("path", "guarded"),
    [
        ("{root}/data/venv/bin:/usr/local/bin:/usr/bin", True),
        ("{root}/opt/venv/bin:/usr/local/bin:/usr/bin", True),  # through a link
        ("{root}/data/venv/bin:/usr/bin", True),  # the shadow's dir not on PATH
        ("/usr/local/bin:{root}/data/venv/bin:/usr/bin", False),  # behind it
        ("{root}/ro:/usr/local/bin:/usr/bin", False),  # not writable
    ],
)
def test_entry_guard(tmp_path: Path, path: str, guarded: bool) -> None:
    tree(tmp_path, "home/", "data/venv/bin/", "ro/")
    (tmp_path / "opt").mkdir()
    (tmp_path / "opt/venv").symlink_to(tmp_path / "data/venv")
    env = {
        "HOME": str(tmp_path / "home"),
        "PATH": path.replace("{root}", str(tmp_path)),
        "CLAUDE_SANDBOX_ALLOW_WRITE": str(tmp_path / "data"),
    }
    argv = build(env, "")
    venv_bin = f"{tmp_path}/data/venv/bin"
    expected = [f"{venv_bin}/{n}" for n in ENTRY_POINTS] if guarded else []
    assert [
        argv[i + 2]
        for i in range(len(argv) - 2)
        if argv[i : i + 2] == ["--ro-bind", "/dev/null"]
        and argv[i + 2].rpartition("/")[2] in ENTRY_POINTS
    ] == expected
    assert setenv(argv, ENTRY_GUARD_ENV) == ([venv_bin] if guarded else [])


# --- uv's Python store, read-only (ADR 27) --------------------------------------


def store_binds(tmp_path: Path, env: Mapping[str, str]) -> tuple[list[str], Built]:
    """The read-only binds of a store, and the build, for a layout with
    ``home`` and an ``allow-write`` of ``data``."""
    env = {
        "HOME": f"{tmp_path}/home",
        "CLAUDE_SANDBOX_ALLOW_WRITE": f"{tmp_path}/data",
        **env,
    }
    built = bwrap_build(
        PROFILES["claude"], Config.from_env(env), env, "", REAL, [],
        shipped_skills_dir="/nonexistent", state_dir="/nonexistent",
    )  # fmt: skip
    argv = built.argv
    pairs = [
        f"{argv[i + 1]} {argv[i + 2]}"
        for i in range(len(argv) - 2)
        if argv[i] == "--ro-bind" and argv[i + 1] in built.writable.readonly
    ]
    return pairs, built


def test_uv_store_bound_read_only(tmp_path: Path) -> None:
    tree(
        tmp_path, "home/.local/share/uv/python/bin/", "data/py/", "data/xdg/uv/python/"
    )
    store = f"{tmp_path}/home/.local/share/uv/python"
    pairs, built = store_binds(tmp_path, {"PATH": f"{store}/bin:/usr/bin"})
    assert pairs == [f"{store} {store}"]
    assert built.writable.readonly == [store]
    # A PATH directory in it is not the session's to write: no guard there.
    assert setenv(built.argv, ENTRY_GUARD_ENV) == []
    # After the read-write bind it sits in.
    argv = built.argv
    assert argv.index(store) > argv.index(f"{tmp_path}/home/.local/share")
    # UV_PYTHON_INSTALL_DIR replaces the default; XDG_DATA_HOME adds uv's
    # outer default to the jail's, unless relative.
    pairs, _ = store_binds(tmp_path, {"UV_PYTHON_INSTALL_DIR": f"{tmp_path}/data/py"})
    assert pairs == [f"{tmp_path}/data/py {tmp_path}/data/py"]
    xdg = f"{tmp_path}/data/xdg/uv/python"
    pairs, _ = store_binds(tmp_path, {"XDG_DATA_HOME": f"{tmp_path}/data/xdg"})
    assert pairs == [f"{xdg} {xdg}", f"{store} {store}"]
    pairs, _ = store_binds(tmp_path, {"XDG_DATA_HOME": "data/xdg"})
    assert pairs == [f"{store} {store}"]


def test_uv_store_through_a_link_outside_the_jail(tmp_path: Path) -> None:
    """``~/.local/share`` a link into ``allow-write``: bound where it lies
    and where the jail sees it through the ``~/.local/share`` bind."""
    tree(tmp_path, "home/.local/", "data/share/uv/python/")
    (tmp_path / "home/.local/share").symlink_to(tmp_path / "data/share")
    real = f"{tmp_path}/data/share/uv/python"
    pairs, built = store_binds(tmp_path, {})
    assert pairs == [f"{real} {tmp_path}/home/.local/share/uv/python", f"{real} {real}"]
    assert built.writable.readonly == [real]


@pytest.mark.parametrize(
    "case",
    ["absent", "planted link", "not writable", "allow-write of it", "relative"],
)
def test_uv_store_skipped(tmp_path: Path, case: str) -> None:
    tree(tmp_path, "home/.local/share/", "data/elsewhere/", "ro/uv/python/")
    env: dict[str, str] = {}
    if case == "planted link":  # a session could have made it
        (tmp_path / "home/.local/share/uv").symlink_to(tmp_path / "data/elsewhere")
        tree(tmp_path, "data/elsewhere/python/")
    elif case == "not writable":
        env["UV_PYTHON_INSTALL_DIR"] = f"{tmp_path}/ro/uv/python"
    elif case == "allow-write of it":
        env["UV_PYTHON_INSTALL_DIR"] = f"{tmp_path}/data/elsewhere"
        env["CLAUDE_SANDBOX_ALLOW_WRITE"] = f"{tmp_path}/data/elsewhere"
    elif case == "relative":
        env["UV_PYTHON_INSTALL_DIR"] = "data/elsewhere"
    pairs, built = store_binds(tmp_path, env)
    assert pairs == []
    assert built.writable.readonly == []


# --- Pi (pi.sh) ---------------------------------------------------------------


def test_pi_profile(tmp_path: Path) -> None:
    home = tree(
        tmp_path, ".pi/agent/", ".claude/", ".codex/", ".cache/", ".claude.json"
    )
    argv = build({"HOME": str(home)}, "", PI_REAL, ["--provider", "openai"], agent="pi")
    assert pair(argv, "--bind", f"{home}/.pi")
    for other in (".claude", ".claude.json", ".codex"):
        assert f"{home}/{other}" not in argv
    assert "--no-chrome" not in argv
    assert setenv(argv, "IS_SANDBOX_AGENT") == ["pi"]
    assert setenv(argv, "PI_SKIP_VERSION_CHECK") == ["1"]
    assert argv[argv.index("--") :] == ["--", PI_REAL, "--provider", "openai"]
    # ...and no other agent's session sees Pi's state, yet each gets the
    # relayed model port.
    for agent, real in (("claude", REAL), ("codex", CODEX_REAL)):
        env = {"HOME": str(home), "CLAUDE_SANDBOX_LOCAL_MODEL_PORT": "8082"}
        argv = build(env, "", real, agent=agent)
        assert f"{home}/.pi" not in argv
        assert setenv(argv, "CLAUDE_SANDBOX_LOCAL_MODEL_PORT") == ["8082"]
