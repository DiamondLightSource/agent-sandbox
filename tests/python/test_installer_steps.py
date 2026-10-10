"""The installer's file steps, each run against a fixture tree.

Until 5.0 these were compared with the bash ``install.sh`` step by step
(issue #72 phase 4); the expectations here are what that comparison held
the Python to, now asserted directly. Everything runs without root under
``INSTALL_PREFIX`` and ``INSTALL_USER_HOME`` in a temporary directory.
"""

import io
import os
import re
import shutil
from collections.abc import Callable, Mapping
from pathlib import Path

import pytest

from claude_sandbox.installer import install
from claude_sandbox.installer.actions import apply
from claude_sandbox.installer.steps import (
    CODEX_MANAGED_BODY,
    RUNTIME_FILES,
    STEPS,
    describe,
    from_env,
)

REPO = Path(__file__).resolve().parents[2]
SCRIPTS = REPO / ".devcontainer/claude-sandbox"
LIBEXEC = "prefix/usr/libexec/claude-sandbox"
MANAGED = "prefix/etc/claude-code/managed-settings.json"
USER_SETTINGS = "user/.claude/settings.json"
CODEX_CONF = "prefix/etc/codex/managed_config.toml"

Setup = Callable[[Path], None]


def tree(root: Path, files: Mapping[str, str | None]) -> None:
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


def source_copy(
    root: Path, drop: tuple[str, ...] = (), add: Setup | None = None
) -> Path:
    """A copy of what the installer reads from its tree, minus ``drop``."""
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
    if add:
        add(src)
    return src


def environment(root: Path, extra: dict[str, str] | None = None) -> dict[str, str]:
    env = {
        "PATH": os.environ["PATH"],
        "HOME": f"{root}/home",
        "INSTALL_PREFIX": f"{root}/prefix",
        "INSTALL_USER_HOME": f"{root}/user",
        "CLAUDE_SHARED_CONFIG": f"{root}/shared",
        "CLAUDE_SANDBOX_SMOKE": "1",
        "CLAUDE_SANDBOX_VERSION": "4.2.0",
    }
    for key, value in (extra or {}).items():
        if value:
            env[key] = value
        else:
            env.pop(key, None)
    return env


def step(
    root: Path,
    name: str,
    files: Mapping[str, str | None] | None = None,
    env: dict[str, str] | None = None,
    source: Path | None = None,
) -> str:
    """Run one step (or ``main``, the whole install) in ``root``; return the
    warnings, and for ``main`` the summary after a NUL."""
    for d in ("prefix", "user", "home"):
        (root / d).mkdir(parents=True, exist_ok=True)
    tree(root, files or {})
    layout, options = from_env(source or REPO, environment(root, env))
    err, out = io.StringIO(), io.StringIO()
    if name == "main":
        install(layout, options, err, out)
        return err.getvalue() + "\0" + out.getvalue()
    old = os.umask(0o022)
    try:
        apply(dict(STEPS)[name](layout, options), err)
    finally:
        os.umask(old)
    return err.getvalue().replace(str(root), "{root}")


def mode(path: Path) -> int:
    return path.lstat().st_mode & 0o7777


def link(root: Path, rel: str) -> str:
    return os.readlink(root / rel).replace(str(root), "{root}")


# --- the commands on PATH ---------------------------------------------------


@pytest.mark.parametrize("existing", [False, True])
def test_the_shims_own_every_name(tmp_path: Path, existing: bool) -> None:
    files = (
        {"prefix/usr/local/bin/claude@0600": "old\n", "prefix/usr/local/bin/codex": ""}
        if existing
        else {}
    )
    step(tmp_path, "shadow", files)
    bin_ = tmp_path / "prefix/usr/local/bin"
    shim = (SCRIPTS / "claude-shim").read_bytes()
    for name in ("claude", "codex", "pi"):
        assert (bin_ / name).read_bytes() == shim and mode(bin_ / name) == 0o755
    cli = bin_ / "claude-sandbox"
    assert cli.read_bytes() == (SCRIPTS / "claude-sandbox-shim").read_bytes()
    assert mode(tmp_path / LIBEXEC / "pi-run") == 0o755
    assert mode(tmp_path / LIBEXEC / "pi-system.md") == 0o644


def test_the_runtime_scripts(tmp_path: Path) -> None:
    step(tmp_path, "install_runtime_scripts")
    for src, dst, want in RUNTIME_FILES:
        placed = tmp_path / "prefix" / dst.lstrip("/")
        assert placed.read_bytes() == (REPO / src).read_bytes()
        assert mode(placed) == want


# --- the shared config links -------------------------------------------------

SHARED_NAMES = (".claude", ".claude.json", ".codex", ".pi", ".agents/skills")


def test_no_store_no_links(tmp_path: Path) -> None:
    step(tmp_path, "link_terminal_config")
    assert os.listdir(tmp_path / "home") == []


def test_links_to_a_fresh_store(tmp_path: Path) -> None:
    step(tmp_path, "link_terminal_config", {"shared": None})
    for name in SHARED_NAMES:
        assert link(tmp_path, f"home/{name}") == f"{{root}}/shared/{name}"
    assert (tmp_path / "shared/.claude").is_dir()
    # Claude's installer fails on an empty .claude.json (issue #103).
    assert (tmp_path / "shared/.claude.json").read_text() == "{}"


@pytest.mark.parametrize("old", ["", "token"])
def test_an_existing_shared_claude_json_is_kept(tmp_path: Path, old: str) -> None:
    step(tmp_path, "link_terminal_config", {"shared/.claude.json": old})
    assert link(tmp_path, "home/.claude.json") == "{root}/shared/.claude.json"
    assert (tmp_path / "shared/.claude.json").read_text() == old


def test_links_in_place_stay_and_others_are_repointed(tmp_path: Path) -> None:
    files = {
        "shared/.claude/x": "1",
        "home/.claude -> {root}/shared/.claude": None,
        "home/.codex -> elsewhere": None,
    }
    step(tmp_path, "link_terminal_config", files)
    assert link(tmp_path, "home/.codex") == "{root}/shared/.codex"
    assert (tmp_path / "shared/.claude/x").read_text() == "1"


def test_an_empty_store_is_seeded_from_home(tmp_path: Path) -> None:
    files = {
        "shared/.claude": None,
        "shared/.claude.json": "",
        "home/.claude/marker": "seedme",
        "home/.claude.json": "token",
        "home/.pi/agent/x": "pi",
        "home/.agents/skills/mine/SKILL.md": "mine",
    }
    err = step(tmp_path, "link_terminal_config", files)
    assert (tmp_path / "shared/.claude/marker").read_text() == "seedme"
    assert (tmp_path / "shared/.claude.json").read_text() == "token"
    assert (tmp_path / "shared/.pi/agent/x").read_text() == "pi"
    assert (tmp_path / "shared/.agents/skills/mine/SKILL.md").read_text() == "mine"
    for name in SHARED_NAMES:
        assert link(tmp_path, f"home/{name}") == f"{{root}}/shared/{name}"
    assert err.count("seeded shared config") == 4
    assert "from {root}/home/.claude.\n" in err


def test_a_populated_store_wins_and_home_is_backed_up(tmp_path: Path) -> None:
    files = {
        "shared/.claude/marker": "shared",
        "shared/.claude.json": "shared",
        "shared/.codex": "a file where a directory goes",
        "home/.claude/marker": "local",
        "home/.claude.json": "local",
        "home/.codex/auth.json": "local",
    }
    err = step(tmp_path, "link_terminal_config", files)
    home = tmp_path / "home"
    backups = sorted(p.name for p in home.iterdir() if ".pre-sandbox." in p.name)
    assert [re.sub(r"\d{8}-\d{6}", "T", b) for b in backups] == [
        ".claude.json.pre-sandbox.T",
        ".claude.pre-sandbox.T",
        ".codex.pre-sandbox.T",
    ]
    assert (home / backups[1] / "marker").read_text() == "local"
    assert (tmp_path / "shared/.claude/marker").read_text() == "shared"
    assert link(tmp_path, "home/.claude") == "{root}/shared/.claude"
    assert err.count("already populated; backed up") == 3


def test_a_read_only_store_shares_claude_only(tmp_path: Path) -> None:
    files = {"shared/.claude": None, "shared/.claude.json": "{}", "shared@0555": None}
    try:
        err = step(tmp_path, "link_terminal_config", files)
    finally:
        (tmp_path / "shared").chmod(0o755)
    assert sorted(os.listdir(tmp_path / "home")) == [".claude", ".claude.json"]
    assert "is not writable; ~/.codex stays container-scoped" in err


# --- credential directories, conf, stamps --------------------------------------


def test_credential_directories(tmp_path: Path) -> None:
    step(tmp_path, "ensure_cred_dirs", {"user/.codex/config.toml": "x"})
    user = tmp_path / "user"
    for d in (".config/gh", ".config/glab-cli", ".codex", ".pi/agent"):
        assert (user / d).is_dir()
    assert (user / ".claude.json").read_text() == "{}"
    assert (user / ".codex/config.toml").read_text() == "x"


@pytest.mark.parametrize("old", ["", "token"])
def test_an_existing_claude_json_is_kept(tmp_path: Path, old: str) -> None:
    step(tmp_path, "ensure_cred_dirs", {"user/.claude.json": old})
    assert (tmp_path / "user/.claude.json").read_text() == old


@pytest.mark.parametrize("old", [None, "old"])
def test_the_conf(tmp_path: Path, old: str | None) -> None:
    step(
        tmp_path,
        "install_conf",
        {"prefix/etc/claude-sandbox.conf@0600": old} if old else {},
    )
    conf = tmp_path / "prefix/etc/claude-sandbox.conf"
    assert (
        conf.read_bytes() == (REPO / ".devcontainer/claude-sandbox.conf").read_bytes()
    )
    assert mode(conf) == 0o644


def test_no_conf_in_the_tree_places_none(tmp_path: Path) -> None:
    src = source_copy(tmp_path, (".devcontainer/claude-sandbox.conf",))
    step(tmp_path, "install_conf", source=src)
    assert not (tmp_path / "prefix/etc").exists()


@pytest.mark.parametrize(
    ("version", "git", "want"),
    [("4.2.0", True, "4.2.0"), ("", True, None), ("", False, "unknown")],
)
def test_the_version_stamp(
    tmp_path: Path, version: str, git: bool, want: str | None
) -> None:
    files = {f"{LIBEXEC}/version@0600": "4.1.0\n"}
    src = None if git else source_copy(tmp_path)
    step(tmp_path, "stamp_version", files, {"CLAUDE_SANDBOX_VERSION": version}, src)
    stamp = tmp_path / LIBEXEC / "version"
    assert stamp.read_text() == f"{want or describe(REPO)}\n"
    assert mode(stamp) == 0o644


def test_the_installer_stamp(tmp_path: Path) -> None:
    step(tmp_path, "stamp_installer", env={"CLAUDE_SANDBOX_INSTALLER": "uvx"})
    assert (tmp_path / LIBEXEC / "installer").read_text() == "uvx\n"
    # A clone install removes it, so the record never lies.
    step(tmp_path, "stamp_installer")
    assert not (tmp_path / LIBEXEC / "installer").exists()


# --- shipped skills (ADR 24) ---------------------------------------------------


def shipped() -> list[str]:
    return sorted(
        d
        for d in os.listdir(REPO / "skills")
        if (REPO / "skills" / d / "SKILL.md").is_file()
    )


def test_skills_replace_what_was_there(tmp_path: Path) -> None:
    files = {
        f"{LIBEXEC}/skills/gone/SKILL.md": "stale",
        f"{LIBEXEC}/skills/verify-sandbox@0700": None,
    }
    step(tmp_path, "install_shipped_skills", files)
    skills = tmp_path / LIBEXEC / "skills"
    assert sorted(os.listdir(skills)) == shipped()
    assert mode(skills) == mode(skills / "verify-sandbox") == 0o755
    checks = skills / "verify-sandbox/references/checks.md"
    assert mode(checks) == 0o644


def test_only_directories_with_a_skill_ship(tmp_path: Path) -> None:
    def extra(src: Path) -> None:
        tree(
            src,
            {
                "skills/README.md": "not a skill\n",
                "skills/notaskill/notes.md": "no SKILL.md\n",
                "skills/.hidden/SKILL.md": "hidden\n",
                "skills/extra/SKILL.md": "extra\n",
                "skills/extra/run.sh@0775": "#!/bin/sh\n",
                "skills/extra/data/table.txt@0664": "1 2\n",
                "skills/extra/data/.dot": "dot\n",
                "skills/extra/link -> run.sh": None,
            },
        )

    step(tmp_path, "install_shipped_skills", source=source_copy(tmp_path, add=extra))
    skills = tmp_path / LIBEXEC / "skills"
    assert sorted(os.listdir(skills)) == sorted([*shipped(), "extra"])
    assert mode(skills / "extra/run.sh") == 0o755
    assert mode(skills / "extra/data/table.txt") == 0o644
    assert (skills / "extra/data/.dot").read_text() == "dot\n"
    assert os.readlink(skills / "extra/link") == "run.sh"


def test_no_skills_in_the_tree_removes_them(tmp_path: Path) -> None:
    files = {f"{LIBEXEC}/skills/old/SKILL.md": "x"}
    step(
        tmp_path,
        "install_shipped_skills",
        files,
        source=source_copy(tmp_path, ("skills",)),
    )
    assert os.listdir(tmp_path / LIBEXEC) == []


# --- managed settings (ADR 13) -------------------------------------------------

# Every value and the key order are kept; numbers are written as Python
# reads them.
ADMIN = """{"permissions":{"defaultMode":"plan"},"env":{"FOO":"bar"},
"hooks":{"SessionStart":[{"hooks":[{"type":"command","command":"org.sh"}]}]},
"n":[1.0,0.10,1e2,-0,100000000000000000001,1E-7],"s":"é\\u007f\\u0001\\t/",
"e":{},"a":[],"x":null,"t":[true,false],"autoUpdates":true}"""
ADMIN_MERGED = """{
  "permissions": {
    "defaultMode": "plan"
  },
  "env": {
    "FOO": "bar",
    "DISABLE_AUTOUPDATER": "1"
  },
  "hooks": {
    "SessionStart": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "org.sh"
          }
        ]
      }
    ]
  },
  "n": [
    1.0,
    0.1,
    100.0,
    0,
    100000000000000000001,
    1e-07
  ],
  "s": "é\x7f\\u0001\\t/",
  "e": {},
  "a": [],
  "x": null,
  "t": [
    true,
    false
  ],
  "autoUpdates": false,
  "extraKnownMarketplaces": {
    "claude-sandbox": {
      "source": {
        "source": "github",
        "repo": "DiamondLightSource/claude-sandbox"
      }
    }
  }
}
"""
MARKETPLACE_JSON = """  "extraKnownMarketplaces": {
    "claude-sandbox": {
      "source": {
        "source": "github",
        "repo": "DiamondLightSource/claude-sandbox"
      }
    }
  }
"""
UPDATER_OFF = (
    '{\n  "env": {\n    "DISABLE_AUTOUPDATER": "1"\n  },\n  "autoUpdates": false,\n'
    + MARKETPLACE_JSON
    + "}\n"
)
# An administrator's own entry of the same name is kept, and an
# extraKnownMarketplaces that is not an object is left as it is.
ADMIN_MARKET = (
    '{"extraKnownMarketplaces":'
    '{"claude-sandbox":{"source":{"source":"git","url":"u"}}}}'
)
ADMIN_MARKET_MERGED = """{
  "extraKnownMarketplaces": {
    "claude-sandbox": {
      "source": {
        "source": "git",
        "url": "u"
      }
    }
  },
  "env": {
    "DISABLE_AUTOUPDATER": "1"
  },
  "autoUpdates": false
}
"""


@pytest.mark.parametrize(
    ("before", "after"),
    [
        (None, UPDATER_OFF),
        (ADMIN, ADMIN_MERGED),
        (
            '{"env":null,"z":1}',
            '{\n  "env": {\n    "DISABLE_AUTOUPDATER": "1"\n  },\n  "z": 1,\n'
            '  "autoUpdates": false,\n' + MARKETPLACE_JSON + "}\n",
        ),
        (ADMIN_MARKET, ADMIN_MARKET_MERGED),
        (
            '{"extraKnownMarketplaces":[]}',
            '{\n  "extraKnownMarketplaces": [],\n  "env": {\n'
            '    "DISABLE_AUTOUPDATER": "1"\n  },\n  "autoUpdates": false\n}\n',
        ),
    ],
)
def test_the_updater_is_disabled_in_the_managed_policy(
    tmp_path: Path, before: str | None, after: str
) -> None:
    step(tmp_path, "wire_managed_settings", {MANAGED: before} if before else {})
    assert (tmp_path / MANAGED).read_text() == after
    assert mode(tmp_path / MANAGED) == 0o644


# Not one JSON value, a value that is no settings, and what could not be
# written back: an overflowing float, a lone surrogate, nesting too deep.
BAD_JSON = (
    "{not json",
    "",
    "null",
    "false",
    "{} {}",
    '{"a":NaN}',
    '{"a":1e400}',
    '{"s":"\\ud800"}',
    '{"a":' * 100_000 + "1" + "}" * 100_000,
)


@pytest.mark.parametrize(
    ("name", "path", "text"),
    [
        *(("wire_managed_settings", MANAGED, t) for t in BAD_JSON),
        # ADR 13: warn and skip, never fail over a settings file the
        # installer does not own (the bash aborted on these).
        ("wire_managed_settings", MANAGED, "[1]"),
        ("wire_managed_settings", MANAGED, '{"env":"x"}'),
        ("wire_user_statusline", USER_SETTINGS, "{"),
        ("wire_user_statusline", USER_SETTINGS, "[1]"),
        # Not followed when reading user files as root.
        ("wire_user_statusline", USER_SETTINGS + " -> other.json", "{}"),
    ],
)
def test_a_settings_file_it_cannot_merge_is_left_with_a_warning(
    tmp_path: Path, name: str, path: str, text: str
) -> None:
    path, _, target = path.partition(" -> ")
    files = (
        {f"{path} -> {target}": None, f"user/.claude/{target}": text} if target else {}
    )
    err = step(tmp_path, name, files or {path: text})
    assert "WARNING" in err
    assert ("not valid JSON" in err) == (text in BAD_JSON or text == "{")
    assert ("not a JSON object" in err) == text.startswith("[")
    assert (tmp_path / path).read_text() == text
    assert (tmp_path / path).is_symlink() == bool(target)


# --- Codex's managed config ---------------------------------------------------


@pytest.mark.parametrize(
    "old", [None, "# Managed by claude-sandbox — do not edit by hand.\nold = 1\n"]
)
def test_codex_config_we_own_is_written(tmp_path: Path, old: str | None) -> None:
    step(tmp_path, "wire_codex_managed", {f"{CODEX_CONF}@0600": old} if old else {})
    assert (tmp_path / CODEX_CONF).read_text() == CODEX_MANAGED_BODY
    assert mode(tmp_path / CODEX_CONF) == 0o644


@pytest.mark.parametrize("theirs", ["# ACME policy\nx = 1\n", ""])
def test_codex_config_we_did_not_write_is_left(tmp_path: Path, theirs: str) -> None:
    err = step(tmp_path, "wire_codex_managed", {CODEX_CONF: theirs})
    assert (tmp_path / CODEX_CONF).read_text() == theirs
    assert "was not written by us" in err
    assert err.endswith("check_for_update_on_startup = false\n")


# --- the user's status line ------------------------------------------------------

OURS = (
    '"statusLine": {\n    "type": "command",\n'
    '    "command": "bash $HOME/.claude/statusline-command.sh"\n  }'
)
STATUSLINE = REPO / ".claude/statusline-command.sh"


@pytest.mark.parametrize(
    ("files", "env", "settings", "script"),
    [
        ({}, {}, "{\n  " + OURS + "\n}\n", None),
        # Theirs is kept, script and setting both.
        (
            {
                USER_SETTINGS: '{"model":"opus","statusLine":'
                '{"type":"command","command":"theirs.sh"},"hooks":{}}',
                "user/.claude/statusline-command.sh": "echo custom\n",
            },
            {},
            '{\n  "model": "opus",\n  "statusLine": {\n    "type": "command",\n'
            '    "command": "theirs.sh"\n  },\n  "hooks": {}\n}\n',
            "echo custom\n",
        ),
        (
            {USER_SETTINGS: '{"statusLine":null,"n":1.50}'},
            {},
            "{\n  " + OURS + ',\n  "n": 1.5\n}\n',
            None,
        ),
        # STATUS=1 replaces the script, not the setting.
        (
            {"user/.claude/statusline-command.sh@0700": "echo custom\n"},
            {"STATUS": "1"},
            "{\n  " + OURS + "\n}\n",
            None,
        ),
    ],
)
def test_the_status_line(
    tmp_path: Path,
    files: dict[str, str | None],
    env: dict[str, str],
    settings: str,
    script: str | None,
) -> None:
    step(tmp_path, "wire_user_statusline", files, env)
    assert (tmp_path / USER_SETTINGS).read_text() == settings
    placed = tmp_path / "user/.claude/statusline-command.sh"
    assert placed.read_text() == (script or STATUSLINE.read_text())
    if script is None:
        assert mode(placed) == 0o755


@pytest.mark.parametrize(
    ("before", "after"), [(None, None), ("[1,{}]", "[\n  1,\n  {}\n]\n")]
)
def test_no_status_line_script_no_setting(
    tmp_path: Path, before: str | None, after: str | None
) -> None:
    src = source_copy(tmp_path, (".claude/statusline-command.sh",))
    step(
        tmp_path,
        "wire_user_statusline",
        {USER_SETTINGS: before} if before else {},
        source=src,
    )
    settings = tmp_path / USER_SETTINGS
    assert (settings.read_text() if settings.exists() else None) == after
    assert not (tmp_path / "user/.claude/statusline-command.sh").exists()


# --- the whole install -----------------------------------------------------------


def test_a_fresh_install_and_its_summary(tmp_path: Path) -> None:
    out = step(tmp_path, "main", {"shared": None}).replace(str(tmp_path), "{root}")
    warnings, summary = out.split("\0")
    assert warnings == ""
    assert summary.startswith("claude-sandbox: install complete.\n")
    assert (
        "  python:      interpreter in {root}/prefix/usr/libexec/claude-sandbox/venv"
        in summary
    )
    assert (
        "  cli:         {root}/prefix/usr/local/bin/claude-sandbox (4.2.0)\n" in summary
    )
    assert (
        "(updater disabled; claude-sandbox plugin marketplace known)" in summary
        and "(updater settings)" in summary
    )
    assert "NOT installed — pi will refuse to launch" in summary
    assert (tmp_path / MANAGED).read_text() == UPDATER_OFF
    assert link(tmp_path, "home/.claude") == "{root}/shared/.claude"
    assert not (tmp_path / LIBEXEC / "installer").exists()


def test_a_uvx_install_over_an_older_one(tmp_path: Path) -> None:
    files = {
        MANAGED: ADMIN,
        USER_SETTINGS: '{"model":"opus"}',
        f"{LIBEXEC}/skills/gone/SKILL.md": "x",
    }
    step(tmp_path, "main", files, {"CLAUDE_SANDBOX_INSTALLER": "uvx"})
    assert (tmp_path / MANAGED).read_text() == ADMIN_MERGED
    assert (tmp_path / LIBEXEC / "installer").read_text() == "uvx\n"
    assert sorted(os.listdir(tmp_path / LIBEXEC / "skills")) == shipped()
    assert '"model": "opus"' in (tmp_path / USER_SETTINGS).read_text()


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


def test_reinstall_writes_nothing(tmp_path: Path) -> None:
    """A second install touches nothing at all (smoke.sh checks only the
    bytes)."""
    files = {"shared": None, MANAGED: ADMIN, CODEX_CONF: "# foreign\n"}
    env = {"CLAUDE_SANDBOX_INSTALLER": "uvx"}
    step(tmp_path, "main", files, env)
    before = snapshot(tmp_path)
    err = step(tmp_path, "main", None, env)
    assert snapshot(tmp_path) == before
    assert "was not written by us" in err


def test_install_sets_its_own_umask(tmp_path: Path) -> None:
    old = os.umask(0)
    try:
        step(tmp_path, "main")
    finally:
        os.umask(old)
    assert (tmp_path / "prefix/etc/claude-code").stat().st_mode & 0o777 == 0o755
