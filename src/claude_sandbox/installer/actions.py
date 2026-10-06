"""Filesystem changes as data, and the one function that makes them.

Every installer step plans a list of these by reading the filesystem, and
``apply`` carries them out. A step that finds everything in place plans
nothing, so a second install writes nothing at all.
"""

import os
import shutil
import stat
import sys
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

# A file's owner as (uid, gid), or None to keep the process's own.
Owner = tuple[int, int] | None


@dataclass(frozen=True)
class MakeDirs:
    """``mkdir -p``: missing directories get the umask's mode."""

    path: Path


@dataclass(frozen=True)
class Write:
    """Replace ``path`` atomically: a temporary file beside it, then rename."""

    path: Path
    data: bytes
    mode: int
    owner: Owner = None


@dataclass(frozen=True)
class Touch:
    """Create an empty file (the umask's mode) unless one exists."""

    path: Path


@dataclass(frozen=True)
class Remove:
    """``rm -rf``: a file, a symlink or a whole tree."""

    path: Path


@dataclass(frozen=True)
class Symlink:
    path: Path
    target: str


@dataclass(frozen=True)
class Move:
    """``mv``, across filesystems if need be."""

    src: Path
    dst: Path


@dataclass(frozen=True)
class Entry:
    """One node of a tree: a directory (``data`` and ``link`` None), a file
    or a symlink. ``rel`` is relative to the tree's root."""

    rel: str
    mode: int
    data: bytes | None = None
    link: str | None = None


@dataclass(frozen=True)
class ReplaceTree:
    """Replace the directory ``path`` with exactly ``entries``."""

    path: Path
    mode: int
    entries: tuple[Entry, ...]
    owner: Owner = None


@dataclass(frozen=True)
class Warn:
    message: str


Action = MakeDirs | Write | Touch | Remove | Symlink | Move | ReplaceTree | Warn


def scan(root: Path) -> tuple[Entry, ...]:
    """The tree under ``root`` as entries, sorted, without following links."""
    entries: list[Entry] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        for name in [*dirnames, *sorted(filenames)]:
            path = Path(dirpath, name)
            rel = path.relative_to(root).as_posix()
            st = path.lstat()
            mode = stat.S_IMODE(st.st_mode)
            if stat.S_ISLNK(st.st_mode):
                entries.append(Entry(rel, 0o777, link=os.readlink(path)))
            elif stat.S_ISDIR(st.st_mode):
                entries.append(Entry(rel, mode))
            else:
                entries.append(Entry(rel, mode, data=path.read_bytes()))
    return tuple(sorted(entries, key=lambda e: e.rel))


def apply(actions: Iterable[Action], err: TextIO | None = None) -> None:
    """Carry out ``actions`` in order. Warnings go to ``err`` (stderr)."""
    for action in actions:
        match action:
            case MakeDirs(path):
                path.mkdir(parents=True, exist_ok=True)
            case Write(path, data, mode, owner):
                path.parent.mkdir(parents=True, exist_ok=True)
                _write(path, data, mode, owner)
            case Touch(path):
                os.close(os.open(path, os.O_WRONLY | os.O_CREAT, 0o666))
            case Remove(path):
                _remove(path)
            case Symlink(path, target):
                path.symlink_to(target)
            case Move(src, dst):
                shutil.move(src, dst)
            case ReplaceTree(path, mode, entries, owner):
                _replace_tree(path, mode, entries, owner)
            case Warn(message):
                print(message, file=err or sys.stderr)


def _write(path: Path, data: bytes, mode: int, owner: Owner) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            os.fchmod(f.fileno(), mode)
            if owner is not None:
                os.fchown(f.fileno(), *owner)
        os.replace(tmp, path)
    except BaseException:
        os.unlink(tmp)
        raise


def _remove(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    elif os.path.lexists(path):
        path.unlink()


def _replace_tree(
    path: Path, mode: int, entries: tuple[Entry, ...], owner: Owner
) -> None:
    """Build the tree beside ``path``, then swap it in with two renames."""
    path.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(dir=path.parent, prefix=f".{path.name}."))
    try:
        for e in sorted(entries, key=lambda e: e.rel):
            node = stage / e.rel
            if e.link is not None:
                node.symlink_to(e.link)
                if owner is not None:
                    os.lchown(node, *owner)
                continue
            if e.data is None:
                node.mkdir()
            else:
                node.write_bytes(e.data)
            node.chmod(e.mode)
            if owner is not None:
                os.chown(node, *owner)
        stage.chmod(mode)
        if owner is not None:
            os.chown(stage, *owner)
        old = stage.with_name(stage.name + ".old")
        if os.path.lexists(path):
            path.rename(old)
        stage.rename(path)
        _remove(old)
    except BaseException:
        _remove(stage)
        raise
