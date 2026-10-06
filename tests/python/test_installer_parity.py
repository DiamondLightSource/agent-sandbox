"""The installer comparison harness (ADR 26, issue #72 phase 4).

Each scenario builds the same starting tree twice, runs one step of the
bash ``install.sh`` (through install_driver.sh) in one and the Python port
in the other, and requires identical results: every path, its type, mode,
bytes or link target, and the warnings printed. ``install.sh`` installs
into a prefix and a user home without root (``CLAUDE_SANDBOX_SMOKE=1``), so
it all runs in temporary directories.
"""

import io
import os
import re
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from claude_sandbox.installer import install
from claude_sandbox.installer.actions import Entry, apply, scan
from claude_sandbox.installer.steps import STEPS, from_env

REPO = Path(__file__).resolve().parents[2]
DRIVER = Path(__file__).with_name("install_driver.sh")
INSTALL_SH = Path(".devcontainer/claude-sandbox/install.sh")
BASH = shutil.which("bash") or "/bin/bash"

Setup = Callable[[Path], None]


def tree(root: Path, files: dict[str, str | None]) -> None:
    """Create ``files`` under ``root``: text, or a directory for None.
    A name ending ``@MODE`` sets the mode, ``-> TARGET`` makes a symlink."""
    for name, text in files.items():
        name, _, mode = name.partition("@")
        name, _, target = name.partition(" -> ")
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        if target:
            path.symlink_to(target.replace("{root}", str(root)))
        elif text is None:
            path.mkdir(exist_ok=True)
        else:
            path.write_text(text)
        if mode:
            path.chmod(int(mode, 8))


def files(spec: dict[str, str | None]) -> Setup:
    return lambda root: tree(root, spec)


def nothing(root: Path) -> None:
    pass


def source_copy(root: Path, drop: tuple[str, ...] = (), add: Setup = nothing) -> Path:
    """A copy of what install.sh reads from its tree, minus ``drop``."""
    src = root / "src"
    shutil.copytree(REPO / ".devcontainer", src / ".devcontainer", symlinks=True)
    shutil.copytree(REPO / "skills", src / "skills", symlinks=True)
    (src / ".claude").mkdir()
    shutil.copy2(REPO / ".claude/statusline-command.sh", src / ".claude")
    for rel in drop:
        if (src / rel).is_dir():
            shutil.rmtree(src / rel)
        else:
            (src / rel).unlink()
    add(src)
    return src


@dataclass(frozen=True)
class Scenario:
    id: str
    step: str
    setup: Setup = nothing
    env: dict[str, str] = field(default_factory=dict[str, str])
    # Build a copy of the source tree: (paths to drop, extra files).
    source: tuple[tuple[str, ...], Setup] | None = None


def environment(root: Path, extra: dict[str, str]) -> dict[str, str]:
    env = {
        "PATH": os.environ["PATH"],
        "HOME": f"{root}/home",
        "INSTALL_PREFIX": f"{root}/prefix",
        "INSTALL_USER_HOME": f"{root}/user",
        "CLAUDE_SHARED_CONFIG": f"{root}/shared",
        "CLAUDE_SANDBOX_SMOKE": "1",
        "CLAUDE_SANDBOX_VERSION": "4.2.0",
    }
    for key, value in extra.items():
        if value:
            env[key] = value.replace("{root}", str(root))
        else:
            env.pop(key, None)
    return env


def prepare(root: Path, sc: Scenario) -> tuple[Path, dict[str, str]]:
    root.mkdir()
    for d in ("prefix", "user", "home"):
        (root / d).mkdir()
    source = source_copy(root, *sc.source) if sc.source else REPO
    sc.setup(root)
    return source, environment(root, sc.env)


def run_bash(root: Path, sc: Scenario) -> tuple[int, str, str]:
    """The exit status, stderr and stdout (main()'s summary)."""
    source, env = prepare(root, sc)
    done = subprocess.run(
        [BASH, str(DRIVER), str(source / INSTALL_SH), sc.step],
        env=env,
        capture_output=True,
        text=True,
        check=False,
        umask=0o022,  # what install() sets for itself
    )
    return done.returncode, done.stderr, done.stdout


def run_python(root: Path, sc: Scenario) -> str:
    """The warnings; main()'s summary follows them, after a NUL."""
    source, env = prepare(root, sc)
    layout, options = from_env(source, env)
    err, out = io.StringIO(), io.StringIO()
    if sc.step == "main":
        install(layout, options, err, out)
        return err.getvalue() + "\0" + out.getvalue()
    else:
        old = os.umask(0o022)
        try:
            apply(dict(STEPS)[sc.step](layout, options), err)
        finally:
            os.umask(old)
    return err.getvalue()


STAMP = re.compile(r"pre-sandbox\.\d{8}-\d{6}")


def normal(root: Path, text: str) -> str:
    return STAMP.sub("pre-sandbox.STAMP", text.replace(str(root), "{root}"))


def listing(root: Path) -> list[Entry]:
    """The tree as entries, root-relative, timestamps and the root masked.
    The source copy is the input, not the result."""
    return [
        Entry(normal(root, e.rel), e.mode, e.data, e.link and normal(root, e.link))
        for e in scan(root)
        if not e.rel.startswith("src")
    ]


def compare(tmp_path: Path, sc: Scenario) -> list[Entry]:
    a, b = tmp_path / "bash", tmp_path / "python"
    code, bash_err, bash_out = run_bash(a, sc)
    assert code == 0, bash_err
    if sc.step == "main":
        bash_err += "\0" + bash_out
    py_err = run_python(b, sc)
    assert listing(b) == listing(a)
    assert normal(b, py_err) == normal(a, bash_err)
    return listing(b)


SKILLS_EXTRA = files(
    {
        "skills/README.md": "not a skill\n",
        "skills/notaskill/notes.md": "no SKILL.md\n",
        "skills/.hidden/SKILL.md": "hidden\n",
        "skills/extra/SKILL.md": "extra\n",
        "skills/extra/run.sh@0775": "#!/bin/sh\n",
        "skills/extra/data/table.txt@0664": "1 2\n",
        "skills/extra/data/.dot": "dot\n",
        "skills/extra/link -> run.sh": None,
    }
)
ADMIN = """{"permissions":{"defaultMode":"plan"},"env":{"FOO":"bar"},
"hooks":{"SessionStart":[{"hooks":[{"type":"command","command":"org.sh"}]}]},
"n":[1.0,0.10,1e2,-0,100000000000000000001,1E-7],"s":"é\\u007f\\u0001\\t/",
"e":{},"a":[],"x":null,"t":[true,false],"autoUpdates":true}"""
OURS = "# Managed by claude-sandbox — do not edit by hand.\n"

SCENARIOS = [
    Scenario("shadow-fresh", "shadow"),
    Scenario(
        "shadow-existing",
        "shadow",
        files(
            {
                "prefix/usr/local/bin/claude@0600": "old\n",
                "prefix/usr/local/bin/codex": "",
            }
        ),
    ),
    Scenario("links-no-store", "link_terminal_config", env={"HOME": "{root}/home"}),
    Scenario("links-fresh", "link_terminal_config", files({"shared": None})),
    Scenario(
        "links-in-place",
        "link_terminal_config",
        files(
            {
                "shared/.claude/x": "1",
                "home/.claude -> {root}/shared/.claude": None,
                "home/.codex -> elsewhere": None,
            }
        ),
    ),
    Scenario(
        "links-seed",
        "link_terminal_config",
        files(
            {
                "shared/.claude": None,
                "shared/.claude.json": "",
                "home/.claude/marker": "seedme",
                "home/.claude.json": "token",
                "home/.pi/agent/x": "pi",
                "home/.agents/skills/mine/SKILL.md": "mine",
            }
        ),
    ),
    Scenario(
        "links-adopt",
        "link_terminal_config",
        files(
            {
                "shared/.claude/marker": "shared",
                "shared/.claude.json": "shared",
                "shared/.codex": "a file where a directory goes",
                "home/.claude/marker": "local",
                "home/.claude.json": "local",
                "home/.codex/auth.json": "local",
            }
        ),
    ),
    Scenario(
        "links-read-only-store",
        "link_terminal_config",
        files(
            {
                "shared/.claude": None,
                "shared/.claude.json": "{}",
                "shared@0555": None,
            }
        ),
    ),
    Scenario("cred-dirs-fresh", "ensure_cred_dirs"),
    Scenario(
        "cred-dirs-existing",
        "ensure_cred_dirs",
        files({"user/.claude.json": "{}", "user/.codex/config.toml": "x"}),
    ),
    Scenario("conf-fresh", "install_conf"),
    Scenario(
        "conf-changed",
        "install_conf",
        files({"prefix/etc/claude-sandbox.conf@0600": "old"}),
    ),
    Scenario(
        "conf-absent-from-source",
        "install_conf",
        source=((".devcontainer/claude-sandbox.conf",), nothing),
    ),
    Scenario("version-fresh", "stamp_version"),
    Scenario(
        "version-changed",
        "stamp_version",
        files({"prefix/usr/libexec/claude-sandbox/version@0600": "4.1.0\n"}),
    ),
    Scenario("version-from-git", "stamp_version", env={"CLAUDE_SANDBOX_VERSION": ""}),
    Scenario(
        "version-no-git",
        "stamp_version",
        env={"CLAUDE_SANDBOX_VERSION": ""},
        source=((), nothing),
    ),
    Scenario(
        "installer-uvx",
        "stamp_installer",
        files({"prefix/usr/libexec/claude-sandbox": None}),
        env={"CLAUDE_SANDBOX_INSTALLER": "uvx"},
    ),
    Scenario(
        "installer-clone-removes-stamp",
        "stamp_installer",
        files({"prefix/usr/libexec/claude-sandbox/installer": "uvx\n"}),
    ),
    Scenario("runtime-scripts", "install_runtime_scripts"),
    Scenario("skills-fresh", "install_shipped_skills"),
    Scenario(
        "skills-replace-stale",
        "install_shipped_skills",
        files(
            {
                "prefix/usr/libexec/claude-sandbox/skills/gone/SKILL.md": "stale",
                "prefix/usr/libexec/claude-sandbox/skills/verify-sandbox@0700": None,
            }
        ),
    ),
    Scenario("skills-odd-source", "install_shipped_skills", source=((), SKILLS_EXTRA)),
    Scenario(
        "skills-none-in-source",
        "install_shipped_skills",
        files({"prefix/usr/libexec/claude-sandbox/skills/old/SKILL.md": "x"}),
        source=(("skills",), nothing),
    ),
    Scenario("managed-fresh", "wire_managed_settings"),
    Scenario(
        "managed-admin-policy",
        "wire_managed_settings",
        files({"prefix/etc/claude-code/managed-settings.json": ADMIN}),
    ),
    Scenario(
        "managed-env-null",
        "wire_managed_settings",
        files({"prefix/etc/claude-code/managed-settings.json": '{"env":null,"z":1}'}),
    ),
]
for _i, _text in enumerate(["{not json", "", "null", "false"]):
    SCENARIOS.append(
        Scenario(
            f"managed-invalid-{_i}",
            "wire_managed_settings",
            files({"prefix/etc/claude-code/managed-settings.json": _text}),
        )
    )
SCENARIOS.extend(
    [
        Scenario("codex-fresh", "wire_codex_managed"),
        Scenario(
            "codex-ours-outdated",
            "wire_codex_managed",
            files({"prefix/etc/codex/managed_config.toml@0600": OURS + "old = 1\n"}),
        ),
        Scenario(
            "codex-foreign",
            "wire_codex_managed",
            files({"prefix/etc/codex/managed_config.toml": "# ACME policy\nx = 1\n"}),
        ),
        Scenario(
            "codex-empty",
            "wire_codex_managed",
            files({"prefix/etc/codex/managed_config.toml": ""}),
        ),
        Scenario("statusline-fresh", "wire_user_statusline"),
        Scenario(
            "statusline-keeps-theirs",
            "wire_user_statusline",
            files(
                {
                    "user/.claude/settings.json": '{"model":"opus","statusLine":'
                    '{"type":"command","command":"theirs.sh"},"hooks":{}}',
                    "user/.claude/statusline-command.sh": "echo custom\n",
                }
            ),
        ),
        Scenario(
            "statusline-unset",
            "wire_user_statusline",
            files({"user/.claude/settings.json": '{"statusLine":null,"n":1.50}'}),
        ),
        Scenario(
            "statusline-forced",
            "wire_user_statusline",
            files({"user/.claude/statusline-command.sh@0700": "echo custom\n"}),
            env={"STATUS": "1"},
        ),
        Scenario(
            "statusline-invalid",
            "wire_user_statusline",
            files({"user/.claude/settings.json": "{"}),
        ),
        Scenario(
            "statusline-no-script",
            "wire_user_statusline",
            source=((".claude/statusline-command.sh",), nothing),
        ),
        Scenario(
            "statusline-no-script-array",
            "wire_user_statusline",
            files({"user/.claude/settings.json": "[1,{}]"}),
            source=((".claude/statusline-command.sh",), nothing),
        ),
        Scenario("main-fresh", "main", files({"shared": None})),
        Scenario(
            "main-uvx-over-old",
            "main",
            files(
                {
                    "prefix/etc/claude-code/managed-settings.json": ADMIN,
                    "user/.claude/settings.json": '{"model":"opus"}',
                    "prefix/usr/libexec/claude-sandbox/skills/gone/SKILL.md": "x",
                }
            ),
            env={"CLAUDE_SANDBOX_INSTALLER": "uvx"},
        ),
    ]
)


@pytest.mark.parametrize("sc", SCENARIOS, ids=lambda sc: sc.id)
def test_python_step_matches_bash(tmp_path: Path, sc: Scenario) -> None:
    assert compare(tmp_path, sc)


def snapshot(root: Path) -> dict[str, tuple[int, int, int]]:
    """Every node's inode, mtime and ctime: a write of any kind changes one."""
    out: dict[str, tuple[int, int, int]] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        for name in [".", *dirnames, *filenames]:
            st = os.lstat(os.path.join(dirpath, name))
            out[os.path.join(dirpath, name)] = (
                st.st_ino,
                st.st_mtime_ns,
                st.st_ctime_ns,
            )
    return out


@pytest.mark.parametrize("first", ["python", "bash"])
def test_reinstall_writes_nothing(tmp_path: Path, first: str) -> None:
    """A second install, over Python's or the bash's, touches nothing at all
    (the bash rewrites the settings files and recreates the skills on every
    run; smoke.sh only checks the bytes)."""
    sc = Scenario(
        "again",
        "main",
        files(
            {
                "shared": None,
                "prefix/etc/claude-code/managed-settings.json": ADMIN,
                "prefix/etc/codex/managed_config.toml": "# foreign\n",
            }
        ),
        env={"CLAUDE_SANDBOX_INSTALLER": "uvx"},
    )
    root = tmp_path / "root"
    if first == "bash":
        assert run_bash(root, sc)[0] == 0
    else:
        run_python(root, sc)
    before = snapshot(root)
    layout, options = from_env(REPO, environment(root, sc.env))
    err = io.StringIO()
    install(layout, options, err)
    assert snapshot(root) == before
    assert "was not written by us" in err.getvalue()


BAD_JSON = ("{} {}", '{"a":NaN}')
MANAGED = "prefix/etc/claude-code/managed-settings.json"
USER_SETTINGS = "user/.claude/settings.json"


@pytest.mark.parametrize(
    ("step", "path", "text", "bash_ok"),
    [
        # JSON that is not an object: the bash's jq merge fails under set -e
        # and aborts the install. ADR 13: warn and skip, never fail over a
        # settings file the installer does not own.
        ("wire_managed_settings", MANAGED, "[1]", False),
        ("wire_managed_settings", MANAGED, '{"env":"x"}', False),
        ("wire_user_statusline", USER_SETTINGS, "[1]", False),
        # What jq accepts and Python's json does not: several documents, and
        # NaN (which jq rewrites as null). Left alone, with the warning.
        ("wire_managed_settings", MANAGED, "{} {}", True),
        ("wire_managed_settings", MANAGED, '{"a":NaN}', True),
        # The bash reads a symlinked settings file through the link and
        # replaces the link; the Python does not follow symlinks when
        # reading user files as root.
        ("wire_user_statusline", USER_SETTINGS + " -> other.json", "{}", True),
    ],
)
def test_known_divergence_python_warns_and_leaves_the_file(
    tmp_path: Path, step: str, path: str, text: str, bash_ok: bool
) -> None:
    path, _, link = path.partition(" -> ")
    spec = {f"{path} -> {link}": None, f"user/.claude/{link}": text} if link else {}
    sc = Scenario("divergence", step, files(spec or {path: text}))
    assert (run_bash(tmp_path / "bash", sc)[0] == 0) == bash_ok
    err = run_python(tmp_path / "python", sc)
    assert "WARNING" in err and ("not valid JSON" in err) == (text in BAD_JSON)
    assert ("not a JSON object" in err) == text.startswith("[")
    assert (tmp_path / "python" / path).read_text() == text
    assert (tmp_path / "python" / path).is_symlink() == bool(link)


def test_install_sets_its_own_umask(tmp_path: Path) -> None:
    old = os.umask(0)
    try:
        run_python(tmp_path / "root", Scenario("umask", "main"))
    finally:
        os.umask(old)
    assert (tmp_path / "root/prefix/etc/claude-code").stat().st_mode & 0o777 == 0o755


def test_python_install_over_a_bash_install_places_the_shim(tmp_path: Path) -> None:
    """Invariant 1: switching an installed sandbox to the Python one leaves
    the shim, byte for byte, at every shadow name, and the helper CLI shim."""
    root = tmp_path / "root"
    assert run_bash(root, Scenario("bash", "main"))[0] == 0
    env = environment(root, {"CLAUDE_SANDBOX_IMPL": "python"})
    layout, options = from_env(REPO, env)
    install(layout, options, io.StringIO(), io.StringIO())
    scripts = REPO / ".devcontainer/claude-sandbox"
    for name in ("claude", "codex", "pi"):
        placed = root / "prefix/usr/local/bin" / name
        assert placed.read_bytes() == (scripts / "claude-shim").read_bytes()
    cli = root / "prefix/usr/local/bin/claude-sandbox"
    assert cli.read_bytes() == (scripts / "claude-sandbox-shim").read_bytes()
