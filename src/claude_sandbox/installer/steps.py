"""The installer's steps (ADR 26), which the bash ``install.sh`` once held.

Each ``plan_*`` function reads the filesystem and returns the actions that
bring it to the installed state; it writes nothing. Its docstring names the
bash function it replaced, the name the install summary and the docs use.

The steps that run tools rather than write files the installer owns (the
probes, apt and the three agent downloads) are in ``system``; ``install``
in ``__init__`` runs both in main()'s order.
"""

import json
import os
import stat
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import cast

from ..profiles import LIBEXEC
from ..tools import output
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

VERSION_FILE = f"{LIBEXEC}/version"
INSTALLER_FILE = f"{LIBEXEC}/installer"
SKILLS_DIR = f"{LIBEXEC}/skills"
CONF = "/etc/claude-sandbox.conf"
MANAGED_SETTINGS = "/etc/claude-code/managed-settings.json"
CODEX_MANAGED_CONFIG = "/etc/codex/managed_config.toml"
# This repository's plugin marketplace (.claude-plugin/marketplace.json),
# made known to Claude through the managed policy so users need no
# `marketplace add`; installing a plugin from it stays their choice (ADR 24).
MARKETPLACE = "claude-sandbox"
MARKETPLACE_REPO = "DiamondLightSource/claude-sandbox"
CODEX_MARKER = "# Managed by claude-sandbox — do not edit by hand."
USER_STATUSLINE_COMMAND = "bash $HOME/.claude/statusline-command.sh"
SHARED_CONFIG = "/user-terminal-config"
# ADR 26: the installer runs as root and execs nothing found through PATH.
GIT = "/usr/bin/git"

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
# (Invariant 1). Then the helper CLI. Both are three-line shims that run the
# Python ones from the root-owned venv with -I.
SHADOW_NAMES = ("/usr/local/bin/claude", "/usr/local/bin/codex", "/usr/local/bin/pi")
SHADOW_SOURCE = f"{_SCRIPTS}/claude-shim"
CLI_SOURCE = f"{_SCRIPTS}/claude-sandbox-shim"
HELPER_FILES = (
    (f"{_SCRIPTS}/pi-run", f"{LIBEXEC}/pi-run", 0o755),
    (f"{_SCRIPTS}/pi-system.md", f"{LIBEXEC}/pi-system.md", 0o644),
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
    """A step cannot go ahead: the installer exits ``code``, as the bash
    does (1, or 2 for a usage error)."""

    def __init__(self, message: str, code: int = 1) -> None:
        super().__init__(message)
        self.code = code


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
    """``CLAUDE_SANDBOX_VERSION`` (empty: ``git describe``, when stamped),
    ``CLAUDE_SANDBOX_INSTALLER``,
    ``STATUS=1``, ``CLAUDE_SANDBOX_SMOKE``, ``WITH_CODEX``,
    ``WITH_PI`` and ``PI_VERSION``; ``image_build`` is ``--image-build``."""

    version: str
    installer: str = ""
    force_statusline: bool = False
    image_build: bool = False
    smoke: bool = False
    with_codex: bool = True
    with_pi: bool = True
    pi_version: str = "latest"
    now: Callable[[], time.struct_time] = field(default=time.localtime)


def describe(source: Path) -> str:
    """What ``stamp_version`` records when no version is given."""
    argv = [GIT, "-c", "core.fsmonitor=false", "-C", str(source)]
    rc, out = output([*argv, "describe", "--tags", "--always", "--dirty"])
    return out if rc == 0 else "unknown"


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
        version=env.get("CLAUDE_SANDBOX_VERSION", ""),
        installer=env.get("CLAUDE_SANDBOX_INSTALLER", ""),
        force_statusline=env.get("STATUS", "0") == "1",
        smoke=env.get("CLAUDE_SANDBOX_SMOKE", "0") == "1",
        with_codex=env.get("WITH_CODEX", "1") == "1",
        with_pi=env.get("WITH_PI", "1") == "1",
        pi_version=env.get("PI_VERSION") or "latest",
    )
    return layout, options


def _makedirs(path: Path) -> list[Action]:
    return [] if path.is_dir() else [MakeDirs(path)]


def _read(path: Path) -> bytes | None:
    """A regular file's bytes, or None for anything else, a symlink included:
    the installer runs as root and does not follow links where it reads."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return None
    with os.fdopen(fd, "rb") as f:
        return f.read() if stat.S_ISREG(os.fstat(fd).st_mode) else None


def _source(layout: Layout, rel: str) -> bytes:
    path = layout.source / rel
    if not path.is_file():
        raise InstallError(f"claude-sandbox: cannot find {path}")
    return path.read_bytes()


def _place(dst: Path, data: bytes, mode: int, owner: Owner) -> list[Action]:
    """``install_file``: content-compared, so a matching file is left alone
    (mode included, as in the bash)."""
    if _read(dst) == data:
        return []
    return [Write(dst, data, mode, owner)]


def _place_all(layout: Layout, files: tuple[tuple[str, str, int], ...]) -> list[Action]:
    actions: list[Action] = []
    for src, dst, mode in files:
        actions += _place(layout.system(dst), _source(layout, src), mode, layout.owner)
    return actions


def plan_shadow(layout: Layout, options: Options) -> list[Action]:
    """main()'s ``install_file`` calls: the shadow shim, ``pi-run``, the Pi
    note and the helper CLI's shim."""
    shadow = tuple((SHADOW_SOURCE, name, 0o755) for name in SHADOW_NAMES)
    cli = ((CLI_SOURCE, "/usr/local/bin/claude-sandbox", 0o755),)
    # The commands on PATH are replaced as install(1) replaces them, unlink
    # then create, not by rename. In GitHub's rootless podman, renaming over
    # an image-layer file in /usr/local/bin (a directory several layers
    # write to) left no file at all, and the shadow must never be missing
    # (Invariant 1). check_shadow verifies the result.
    return [
        replace(a, in_place=True)
        if isinstance(a, Write) and a.path.parent == layout.system("/usr/local/bin")
        else a
        for a in _place_all(layout, shadow + HELPER_FILES + cli)
    ]


def check_shadow(layout: Layout, options: Options) -> None:
    """Refuse to go on unless every shadow name holds the shadow (Invariant
    1): a missing or different file would let a vendor binary run unwrapped."""
    data = _source(layout, SHADOW_SOURCE)
    for name in SHADOW_NAMES:
        if _read(layout.system(name)) != data:
            raise InstallError(
                f"claude-sandbox: {layout.system(name)} is not the shadow after"
                " placing it; refusing to continue."
            )


def plan_runtime_scripts(layout: Layout, options: Options) -> list[Action]:
    """``install_runtime_scripts``."""
    return _place_all(layout, RUNTIME_FILES)


def plan_cred_dirs(layout: Layout, options: Options) -> list[Action]:
    """``ensure_cred_dirs``: what the shadow binds must exist."""
    home = layout.user_home
    actions = _makedirs(home / ".config/gh") + _makedirs(home / ".config/glab-cli")
    # A link there, even a dangling one, is left as it is.
    if not os.path.lexists(home / ".claude.json"):
        actions.append(Touch(home / ".claude.json"))
    return actions + _makedirs(home / ".codex") + _makedirs(home / ".pi/agent")


def plan_conf(layout: Layout, options: Options) -> list[Action]:
    """``install_conf``: skipped when the tree carries no conf."""
    src = layout.source / ".devcontainer/claude-sandbox.conf"
    if not src.is_file():
        return []
    return _place(layout.system(CONF), src.read_bytes(), 0o644, layout.owner)


def plan_version(layout: Layout, options: Options) -> list[Action]:
    """``stamp_version``: ``git describe`` of the tree when no version is
    given, run only here, so the entrypoint's steps never run git."""
    version = options.version or describe(layout.source)
    data = f"{version}\n".encode()
    return _place(layout.system(VERSION_FILE), data, 0o644, layout.owner)


def plan_installer(layout: Layout, options: Options) -> list[Action]:
    """``stamp_installer``: the front door, removed after a clone install so
    the record never lies."""
    dst = layout.system(INSTALLER_FILE)
    if not options.installer:
        return [Remove(dst)] if os.path.lexists(dst) else []
    return _place(dst, f"{options.installer}\n".encode(), 0o644, layout.owner)


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


def _no_constant(name: str) -> object:
    raise ValueError(f"{name} is not JSON")


def _object(value: object) -> dict[str, object] | None:
    return cast(dict[str, object], value) if isinstance(value, dict) else None


def _dump(value: object) -> bytes:
    # allow_nan=False: a float that overflows (1e400) parses to inf, and
    # json.dumps would write Infinity, which is not JSON.
    return (
        json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + "\n"
    ).encode()


def _settings(path: Path, warning: str) -> tuple[object, list[Action]]:
    """The settings file's value (``{}`` when absent), or a warning. A
    symlink is not followed: it is left alone, with a warning. So is a
    file that is not one JSON value, or is ``null`` or ``false``, or holds
    what cannot be written back (NaN, a float that overflows, a lone
    surrogate, nesting too deep)."""
    if path.is_symlink():
        return None, [Warn(f"claude-sandbox: WARNING — {path} is a symlink; skipped.")]
    data = _read(path)
    if data is None:
        return {}, []
    try:
        text = data.decode("utf-8-sig", errors="replace")
        value: object = json.loads(text, parse_constant=_no_constant)
        _dump(value)
    except (ValueError, UnicodeEncodeError, RecursionError):
        return None, [Warn(warning)]
    if value is None or value is False:
        return None, [Warn(warning)]
    return value, []


def plan_managed_settings(layout: Layout, options: Options) -> list[Action]:
    """``wire_managed_settings``: disable Claude's updater in the managed
    policy (ADR 13) and make this repository's plugin marketplace known
    (ADR 24, amended), keeping every other key and the administrator's hooks.
    A file that is not a JSON object is left alone with a warning, never a
    failed install. When
    this warns the updater is not disabled, and an install summary must
    not say it is."""
    path = layout.system(MANAGED_SETTINGS)

    def skipping(problem: str) -> Warn:
        return Warn(
            f"claude-sandbox: WARNING — {path}{problem}.\n"
            'Skipping the updater settings. Set "env":{"DISABLE_AUTOUPDATER":"1"}\n'
            'and "autoUpdates": false in the managed policy.'
        )

    warning = skipping(" is not valid JSON").message
    actions = _makedirs(path.parent)
    value, warned = _settings(path, warning)
    if warned:
        return actions + warned
    policy = _object(value)
    if policy is None:
        return actions + [skipping(" is not a JSON object")]
    if policy.get("env") is None:
        policy["env"] = {}
    env = _object(policy["env"])
    if env is None:
        return actions + [skipping(": env is not an object")]
    env["DISABLE_AUTOUPDATER"] = "1"
    policy["autoUpdates"] = False
    if policy.get("extraKnownMarketplaces") is None:
        policy["extraKnownMarketplaces"] = {}
    markets = _object(policy["extraKnownMarketplaces"])
    # An administrator's entry of the same name (a fork, a pinned ref) is
    # kept. A value that is not an object is theirs to fix and is left as it
    # is: Claude Code reports the invalid key, and a Warn here would mark the
    # updater settings as skipped in the summary.
    if markets is not None:
        markets.setdefault(
            MARKETPLACE, {"source": {"source": "github", "repo": MARKETPLACE_REPO}}
        )
    return actions + _place(path, _dump(policy), 0o644, layout.owner)


def plan_codex_managed(layout: Layout, options: Options) -> list[Action]:
    """``wire_codex_managed``: a file this installer owns outright, marked on
    its first line. One it did not write is left alone, with a warning."""
    dest = layout.system(CODEX_MANAGED_CONFIG)
    actions = _makedirs(dest.parent)
    if os.path.lexists(dest):
        first = (_read(dest) or b"").split(b"\n", 1)[0]
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
    # A symlinked script counts as the owner's own: kept, and not read.
    if src.is_file():
        if options.force_statusline:
            actions += _place(script, src.read_bytes(), 0o755, None)
        elif not (script.is_file() or script.is_symlink()):
            actions.append(Write(script, src.read_bytes(), 0o755))
    present = (
        script.is_file()
        or script.is_symlink()
        or any(isinstance(a, Write) and a.path == script for a in actions)
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
        user = _object(value)
        if user is None:
            return actions + [
                Warn(
                    f"claude-sandbox: WARNING — {settings} is not a JSON object;"
                    " skipping statusline wiring."
                )
            ]
        if user.get("statusLine") is None:
            user["statusLine"] = {"type": "command", "command": USER_STATUSLINE_COMMAND}
    if not had_file and value == {}:
        return actions
    return actions + _place(settings, _dump(value), 0o644, None)


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

# The file steps in main()'s order. ``install`` runs apt_install and the
# userns probe before the links, and the agent downloads after them.
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
