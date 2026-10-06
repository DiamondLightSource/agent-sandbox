"""The installer's steps, ported from ``.devcontainer/claude-sandbox/install.sh``.

Each ``plan_*`` function reads the filesystem and returns the actions that
bring it to the installed state; it writes nothing. The bash function each
one replaces is named in its docstring, and ``tests/python`` runs both on
the same fixtures and compares the trees they leave.

Not ported here (they stay in the bash until the wiring part of issue #72
phase 4): ``apt_install``, ``probe_or_refuse``, ``probe_userns_or_refuse``
and the three agent-binary downloads.
"""

import os
import subprocess
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from . import jsonfile
from .actions import (
    Action,
    Entry,
    MakeDirs,
    Move,
    Owner,
    Remove,
    ReplaceTree,
    Symlink,
    Touch,
    Warn,
    Write,
    scan,
)

LIBEXEC = "/usr/libexec/claude-sandbox"
VERSION_FILE = f"{LIBEXEC}/version"
INSTALLER_FILE = f"{LIBEXEC}/installer"
SKILLS_DIR = f"{LIBEXEC}/skills"
CONF = "/etc/claude-sandbox.conf"
MANAGED_SETTINGS = "/etc/claude-code/managed-settings.json"
CODEX_MANAGED_CONFIG = "/etc/codex/managed_config.toml"
CODEX_MARKER = "# Managed by claude-sandbox — do not edit by hand."
USER_STATUSLINE_COMMAND = "bash $HOME/.claude/statusline-command.sh"
SHARED_CONFIG = "/user-terminal-config"

CODEX_MANAGED_BODY = f"""{CODEX_MARKER}
#
# Root-cause removal of the update-driven sandbox bypass: a Codex self-update
# re-creates ~/.local/bin/codex, which would then resolve ahead of the shadow.
# Upgrade the image or rebuild the devcontainer to update the agent.
check_for_update_on_startup = false
"""

_SCRIPTS = ".devcontainer/claude-sandbox"
_STATUSLINE = ".claude/statusline-command.sh"

# (source relative to the tree, destination, mode). The shadow first, under
# all three names: it must own them on PATH before any vendor installer runs
# (Invariant 1). Then the helper CLI, as main() places them.
SHADOW_FILES = (
    (f"{_SCRIPTS}/claude-shadow", "/usr/local/bin/claude", 0o755),
    (f"{_SCRIPTS}/claude-shadow", "/usr/local/bin/codex", 0o755),
    (f"{_SCRIPTS}/claude-shadow", "/usr/local/bin/pi", 0o755),
    (f"{_SCRIPTS}/pi-run", f"{LIBEXEC}/pi-run", 0o755),
    (f"{_SCRIPTS}/pi-system.md", f"{LIBEXEC}/pi-system.md", 0o644),
    (f"{_SCRIPTS}/claude-sandbox", "/usr/local/bin/claude-sandbox", 0o755),
)
# install_runtime_scripts.
RUNTIME_FILES = (
    (f"{_SCRIPTS}/codex-launch", f"{LIBEXEC}/codex-launch", 0o755),
    (
        f"{_SCRIPTS}/verify-sandbox-battery.sh",
        f"{LIBEXEC}/verify-sandbox-battery.sh",
        0o755,
    ),
    (_STATUSLINE, f"{LIBEXEC}/statusline-command.sh", 0o755),
    (f"{_SCRIPTS}/pi-sandbox-tag.ts", f"{LIBEXEC}/pi-sandbox-tag.ts", 0o644),
)


class InstallError(RuntimeError):
    """A step cannot go ahead (the bash exits 1)."""


@dataclass(frozen=True)
class Layout:
    """Where things come from and go to.

    ``source`` is the tree being installed (a clone, or the wheel's bundled
    copy). ``prefix`` roots every system path (``INSTALL_PREFIX``),
    ``user_home`` the user's settings (``INSTALL_USER_HOME``); ``home`` and
    ``shared`` are ``$HOME`` and the shared config store the links use,
    kept as strings because the links' targets are compared as text.
    ``owner`` is given to system files (root's in production).
    """

    source: Path
    user_home: Path
    home: str
    prefix: Path = Path("/")
    shared: str = SHARED_CONFIG
    owner: Owner = None

    def system(self, path: str) -> Path:
        return self.prefix / path.lstrip("/")


@dataclass(frozen=True)
class Options:
    """``CLAUDE_SANDBOX_VERSION``/``git describe``, ``CLAUDE_SANDBOX_INSTALLER``
    and ``STATUS=1``; ``image_build`` is ``--image-build``."""

    version: str
    installer: str = ""
    force_statusline: bool = False
    image_build: bool = False
    now: Callable[[], time.struct_time] = field(default=time.localtime)


def describe(source: Path) -> str:
    """What ``stamp_version`` records when no version is given."""
    try:
        out = subprocess.run(
            ["git", "-C", str(source), "describe", "--tags", "--always", "--dirty"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError):
        return "unknown"
    return out.rstrip("\n")


def from_env(source: Path, env: Mapping[str, str]) -> tuple[Layout, Options]:
    """The layout and options ``install.sh`` derives from its environment."""
    home = env.get("HOME", "")
    layout = Layout(
        source=source,
        prefix=Path(env.get("INSTALL_PREFIX") or "/"),
        user_home=Path(env.get("INSTALL_USER_HOME") or home),
        home=home,
        shared=env.get("CLAUDE_SHARED_CONFIG") or SHARED_CONFIG,
    )
    options = Options(
        version=env.get("CLAUDE_SANDBOX_VERSION") or describe(source),
        installer=env.get("CLAUDE_SANDBOX_INSTALLER", ""),
        force_statusline=env.get("STATUS", "0") == "1",
    )
    return layout, options


def _makedirs(path: Path) -> list[Action]:
    return [] if path.is_dir() else [MakeDirs(path)]


def _source(layout: Layout, rel: str) -> bytes:
    path = layout.source / rel
    if not path.is_file():
        raise InstallError(f"claude-sandbox: cannot find {path}")
    return path.read_bytes()


def _place(dst: Path, data: bytes, mode: int, owner: Owner) -> list[Action]:
    """``install_file``: content-compared, so a matching file is left alone
    (mode included, as in the bash)."""
    if dst.is_file() and dst.read_bytes() == data:
        return []
    return [Write(dst, data, mode, owner)]


def _place_all(layout: Layout, files: tuple[tuple[str, str, int], ...]) -> list[Action]:
    actions: list[Action] = []
    for src, dst, mode in files:
        actions += _place(layout.system(dst), _source(layout, src), mode, layout.owner)
    return actions


def plan_shadow(layout: Layout, options: Options) -> list[Action]:
    """main()'s ``install_file`` calls: the shadow, ``pi-run``, the Pi note
    and the helper CLI."""
    return _place_all(layout, SHADOW_FILES)


def plan_runtime_scripts(layout: Layout, options: Options) -> list[Action]:
    """``install_runtime_scripts``."""
    return _place_all(layout, RUNTIME_FILES)


def plan_cred_dirs(layout: Layout, options: Options) -> list[Action]:
    """``ensure_cred_dirs``: what the shadow binds must exist."""
    home = layout.user_home
    actions = _makedirs(home / ".config/gh") + _makedirs(home / ".config/glab-cli")
    if not (home / ".claude.json").exists():
        actions.append(Touch(home / ".claude.json"))
    return actions + _makedirs(home / ".codex") + _makedirs(home / ".pi/agent")


def plan_conf(layout: Layout, options: Options) -> list[Action]:
    """``install_conf``: skipped when the tree carries no conf."""
    src = layout.source / ".devcontainer/claude-sandbox.conf"
    if not src.is_file():
        return []
    return _place(layout.system(CONF), src.read_bytes(), 0o644, layout.owner)


def _stamp(dst: Path, value: str, owner: Owner) -> list[Action]:
    data = f"{value}\n".encode()
    if dst.is_file() and dst.read_bytes() == data:
        return []
    return [Write(dst, data, 0o644, owner)]


def plan_version(layout: Layout, options: Options) -> list[Action]:
    """``stamp_version``."""
    return _stamp(layout.system(VERSION_FILE), options.version, layout.owner)


def plan_installer(layout: Layout, options: Options) -> list[Action]:
    """``stamp_installer``: the front door, removed after a clone install so
    the record never lies."""
    dst = layout.system(INSTALLER_FILE)
    if not options.installer:
        return [Remove(dst)] if os.path.lexists(dst) else []
    return _stamp(dst, options.installer, layout.owner)


def plan_skills(layout: Layout, options: Options) -> list[Action]:
    """``install_shipped_skills`` (ADR 24): the tree is replaced, not merged,
    so a skill removed from the source disappears. Every directory under
    ``skills/`` with a ``SKILL.md``; files 0644, or 0755 if executable."""
    src = layout.source / "skills"
    dst = layout.system(SKILLS_DIR)
    if not src.is_dir():
        return [Remove(dst)] if os.path.lexists(dst) else []
    entries: list[Entry] = []
    for skill in sorted(os.listdir(src)):
        top = src / skill
        if skill.startswith(".") or not (top / "SKILL.md").is_file():
            continue
        entries.append(Entry(skill, 0o755))
        for e in scan(top):
            # chmod -R u=rwX,go=rX, which leaves links alone.
            mode = 0o755 if e.data is None or e.mode & 0o111 else 0o644
            mode = e.mode if e.link is not None else mode
            entries.append(Entry(f"{skill}/{e.rel}", mode, e.data, e.link))
    entries.sort(key=lambda e: e.rel)
    if dst.is_dir() and not dst.is_symlink():
        if (dst.stat().st_mode & 0o7777) == 0o755 and scan(dst) == tuple(entries):
            return []
    return [ReplaceTree(dst, 0o755, tuple(entries), layout.owner)]


def _settings(path: Path, warning: str) -> tuple[jsonfile.Json, list[Action]]:
    """The settings file's value (``{}`` when absent), or a warning."""
    if not path.is_file():
        return {}, []
    try:
        return jsonfile.loads(path.read_bytes()), []
    except jsonfile.NotJson:
        return None, [Warn(warning)]


def _rewrite(path: Path, value: jsonfile.Json, owner: Owner) -> list[Action]:
    data = (jsonfile.dumps(value) + "\n").encode()
    if path.is_file() and path.read_bytes() == data:
        return []
    return [Write(path, data, 0o644, owner)]


def plan_managed_settings(layout: Layout, options: Options) -> list[Action]:
    """``wire_managed_settings``: disable Claude's updater in the managed
    policy (ADR 13), keeping every other key and the administrator's hooks.
    A file that is not a JSON object is left alone with a warning, never a
    failed install. (The bash aborts on JSON that is not an object.)"""
    path = layout.system(MANAGED_SETTINGS)
    warning = (
        f"claude-sandbox: WARNING — {path} is not valid JSON.\n"
        'Skipping the updater settings. Set "env":{"DISABLE_AUTOUPDATER":"1"}\n'
        'and "autoUpdates": false in the managed policy.'
    )
    actions = _makedirs(path.parent)
    value, warned = _settings(path, warning)
    if warned:
        return actions + warned
    if not isinstance(value, dict):
        return actions + [Warn(warning)]
    env = value.get("env")
    if env is None:
        env = value["env"] = {}
    if not isinstance(env, dict):
        return actions + [Warn(warning)]
    env["DISABLE_AUTOUPDATER"] = "1"
    value["autoUpdates"] = False
    return actions + _rewrite(path, value, layout.owner)


def plan_codex_managed(layout: Layout, options: Options) -> list[Action]:
    """``wire_codex_managed``: a file this installer owns outright, marked on
    its first line. One it did not write is left alone, with a warning."""
    dest = layout.system(CODEX_MANAGED_CONFIG)
    actions = _makedirs(dest.parent)
    if dest.is_file():
        first = dest.read_bytes().split(b"\n", 1)[0]
        if first != CODEX_MARKER.encode():
            body = CODEX_MANAGED_BODY.split("\n", 1)[1].rstrip("\n")
            return actions + [
                Warn(
                    f"claude-sandbox: WARNING — {dest} exists and was not written"
                    " by us.\nLeaving it untouched, so the codex updater settings"
                    " is NOT active on this host.\nTo apply it, merge this in by"
                    f" hand:\n\n{body}"
                )
            ]
    return actions + _place(dest, CODEX_MANAGED_BODY.encode(), 0o644, layout.owner)


def plan_statusline(layout: Layout, options: Options) -> list[Action]:
    """``wire_user_statusline``: seed the status line script (``STATUS=1``
    replaces it), and point ``statusLine`` at it only when unset."""
    claude = layout.user_home / ".claude"
    settings = claude / "settings.json"
    script = claude / "statusline-command.sh"
    actions = _makedirs(claude)
    src = layout.source / _STATUSLINE
    if src.is_file():
        if options.force_statusline:
            actions += _place(script, src.read_bytes(), 0o755, None)
        elif not script.is_file():
            actions.append(Write(script, src.read_bytes(), 0o755))
    present = script.is_file() or any(
        isinstance(a, Write) and a.path == script for a in actions
    )
    warning = (
        f"claude-sandbox: WARNING — {settings} is not valid JSON;"
        " skipping statusline wiring."
    )
    had_file = settings.is_file()
    value, warned = _settings(settings, warning)
    if warned:
        return actions + warned
    if present:
        if not isinstance(value, dict):
            return actions + [Warn(warning)]
        if value.get("statusLine") is None:
            value["statusLine"] = {
                "type": "command",
                "command": USER_STATUSLINE_COMMAND,
            }
    if not had_file and value == {}:
        return actions
    return actions + _rewrite(settings, value, None)


def is_mount(path: str) -> bool:
    """``_is_mount``: a different device from the parent directory."""
    try:
        return os.stat(path).st_dev != os.stat(os.path.dirname(path)).st_dev
    except OSError:
        return False


def _is_empty(path: str, kind: str) -> bool:
    if kind == "dir":
        try:
            return not os.listdir(path)
        except NotADirectoryError:
            return False
    return os.stat(path).st_size == 0


def _ensure_shared(shared: str, kind: str) -> list[Action]:
    if os.path.exists(shared):
        return []
    if kind == "dir":
        return [MakeDirs(Path(shared))]
    return _makedirs(Path(shared).parent) + [Touch(Path(shared))]


def _share(target: str, shared: str, kind: str, now: time.struct_time) -> list[Action]:
    """``_share_path``: make ``target`` a symlink to ``shared`` without losing
    what either held. An existing link elsewhere is repointed; a mountpoint
    is left; a real target seeds an empty store or, if the store is
    populated, is backed up beside itself."""
    link = Symlink(Path(target), shared)
    if os.path.islink(target):
        if os.readlink(target) == shared:
            return []
        return _ensure_shared(shared, kind) + [Remove(Path(target)), link]
    if is_mount(target):
        return [
            Warn(
                f"claude-sandbox: {target} is an active mountpoint; leaving as-is"
                " (assumed already shared)."
            )
        ]
    if not os.path.exists(target):
        return _ensure_shared(shared, kind) + [link]
    if not os.path.exists(shared) or _is_empty(shared, kind):
        actions: list[Action] = []
        if os.path.exists(shared):
            actions.append(Remove(Path(shared)))
        return actions + [
            MakeDirs(Path(shared).parent),
            Move(Path(target), Path(shared)),
            Warn(f"claude-sandbox: seeded shared config {shared} from {target}."),
            link,
        ]
    backup = f"{target}.pre-sandbox.{time.strftime('%Y%m%d-%H%M%S', now)}"
    return [
        Move(Path(target), Path(backup)),
        Warn(
            f"claude-sandbox: {shared} already populated; backed up"
            f" {target} -> {backup}."
        ),
        link,
    ]


def plan_shared_links(layout: Layout, options: Options) -> list[Action]:
    """``link_terminal_config``: when the shared store is mounted, Claude's
    config follows the user across containers, and Codex's, Pi's and the
    shared user skills (ADR 25) too when the store is writable."""
    shared, home, now = layout.shared, layout.home, options.now()
    if options.image_build or not os.path.isdir(shared):
        return []
    actions = _share(f"{home}/.claude", f"{shared}/.claude", "dir", now)
    actions += _share(f"{home}/.claude.json", f"{shared}/.claude.json", "file", now)
    if not os.access(shared, os.W_OK):
        return actions + [
            Warn(
                f"claude-sandbox: {shared} is not writable; ~/.codex stays"
                " container-scoped (expect to sign in to codex again after a"
                " rebuild).\nclaude-sandbox: ~/.pi and ~/.agents/skills also stay"
                " container-scoped."
            )
        ]
    actions += _share(f"{home}/.codex", f"{shared}/.codex", "dir", now)
    actions += _share(f"{home}/.pi", f"{shared}/.pi", "dir", now)
    actions += _makedirs(Path(f"{home}/.agents"))
    return actions + _share(
        f"{home}/.agents/skills", f"{shared}/.agents/skills", "dir", now
    )


Step = Callable[[Layout, Options], list[Action]]

# main()'s order, for the steps ported so far. The bash runs apt_install and
# the userns probe after the shadow, and the agent downloads after the links.
STEPS: tuple[tuple[str, Step], ...] = (
    ("shadow", plan_shadow),
    ("link_terminal_config", plan_shared_links),
    ("ensure_cred_dirs", plan_cred_dirs),
    ("install_conf", plan_conf),
    ("stamp_version", plan_version),
    ("stamp_installer", plan_installer),
    ("install_runtime_scripts", plan_runtime_scripts),
    ("install_shipped_skills", plan_skills),
    ("wire_managed_settings", plan_managed_settings),
    ("wire_codex_managed", plan_codex_managed),
    ("wire_user_statusline", plan_statusline),
)
