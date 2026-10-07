"""Installer behaviour the bash comparison cannot reach: mountpoints, image
builds, ownership, and failures part-way through a write."""

import errno
import os
import shutil
from pathlib import Path

import pytest

from claude_sandbox.installer import actions, steps
from claude_sandbox.installer.actions import (
    Entry,
    Move,
    ReplaceTree,
    Touch,
    Warn,
    Write,
)


def layout(tmp_path: Path, source: Path | None = None) -> steps.Layout:
    return steps.Layout(
        source=source or tmp_path / "src",
        user_home=tmp_path / "user",
        home=str(tmp_path / "home"),
        prefix=tmp_path / "prefix",
        shared=str(tmp_path / "shared"),
        owner=(os.getuid(), os.getgid()),
    )


def test_a_mounted_config_is_left_as_it_is(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert steps.is_mount("/proc") and not steps.is_mount("/no/such/path")
    (tmp_path / "home/.claude").mkdir(parents=True)
    (tmp_path / "shared").mkdir()
    lay = layout(tmp_path)

    def is_mount(path: str) -> bool:
        return path.endswith(".claude")

    monkeypatch.setattr(steps, "is_mount", is_mount)
    plan = steps.plan_shared_links(lay, steps.Options("1"))
    assert plan[0] == Warn(
        f"claude-sandbox: {lay.home}/.claude is an active mountpoint;"
        " leaving as-is (assumed already shared)."
    )
    assert steps.plan_shared_links(lay, steps.Options("1", image_build=True)) == []


def test_a_missing_source_file_stops_the_install(tmp_path: Path) -> None:
    with pytest.raises(steps.InstallError, match="cannot find"):
        steps.plan_shadow(layout(tmp_path), steps.Options("1"))


def test_system_files_get_the_owner(tmp_path: Path) -> None:
    repo = Path(__file__).resolve().parents[2]
    lay = layout(tmp_path, tmp_path / "src")
    shutil.copytree(repo / "skills", tmp_path / "src/skills")
    (tmp_path / "src/skills/extra").mkdir()
    (tmp_path / "src/skills/extra/SKILL.md").write_text("x")
    (tmp_path / "src/skills/extra/link").symlink_to("SKILL.md")
    shutil.copytree(repo / ".devcontainer", tmp_path / "src/.devcontainer")
    plan = steps.plan_skills(lay, steps.Options("1"))
    assert all(isinstance(a, ReplaceTree) and a.owner == lay.owner for a in plan)
    actions.apply(plan + steps.plan_conf(lay, steps.Options("1")))
    assert steps.plan_skills(lay, steps.Options("1")) == []


def test_a_failed_write_leaves_no_temporary_file(tmp_path: Path) -> None:
    (tmp_path / "dir/x").mkdir(parents=True)
    with pytest.raises(OSError):
        actions.apply([Write(tmp_path / "dir", b"data", 0o644)])
    assert os.listdir(tmp_path) == ["dir"]
    twice = (Entry("a", 0o755), Entry("a", 0o755))
    with pytest.raises(OSError):
        actions.apply([ReplaceTree(tmp_path / "dir", 0o755, twice)])
    assert os.listdir(tmp_path) == ["dir"]


def test_moves_across_filesystems_and_touch_without_following(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rename = os.rename

    def exdev(src: Path, dst: Path) -> None:
        raise OSError(errno.EXDEV, "cross-device")

    (tmp_path / "d").mkdir()
    (tmp_path / "d/f").write_text("x")
    (tmp_path / "f").write_text("y")
    (tmp_path / "dangling").symlink_to("nowhere")
    monkeypatch.setattr(os, "rename", exdev)
    actions.apply([Move(tmp_path / "d", tmp_path / "d2")])
    actions.apply([Move(tmp_path / "f", tmp_path / "f2")])
    assert (tmp_path / "d2/f").read_text() == "x" and not (tmp_path / "d").exists()
    assert (tmp_path / "f2").read_text() == "y" and not (tmp_path / "f").exists()
    with pytest.raises(FileExistsError):
        actions.apply([Move(tmp_path / "f2", tmp_path / "dangling")])
    monkeypatch.setattr(os, "rename", rename)
    with pytest.raises(FileNotFoundError):
        actions.apply([Move(tmp_path / "absent", tmp_path / "x")])
    with pytest.raises(FileExistsError):
        actions.apply([Touch(tmp_path / "dangling")])
    assert not (tmp_path / "nowhere").exists()


def test_commands_on_path_are_replaced_in_place(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target, other = tmp_path / "claude", tmp_path / "elsewhere"
    other.write_text("not ours")
    target.symlink_to(other)
    actions.apply([Write(target, b"shim", 0o755, in_place=True)])
    assert target.read_bytes() == b"shim" and not target.is_symlink()
    assert other.read_text() == "not ours" and target.stat().st_mode & 0o777 == 0o755

    def refuse(fd: int, uid: int, gid: int) -> None:
        raise PermissionError("chown")

    monkeypatch.setattr(os, "fchown", refuse)
    with pytest.raises(PermissionError):
        actions.apply([Write(target, b"x", 0o755, (0, 0), in_place=True)])
    assert not os.path.lexists(target)  # no partial file left


def test_the_install_stops_if_a_shadow_name_is_not_the_shadow(tmp_path: Path) -> None:
    repo = Path(__file__).resolve().parents[2]
    lay = steps.Layout(
        source=repo, user_home=tmp_path / "u", home=str(tmp_path / "h"),
        prefix=tmp_path / "p",
    )  # fmt: skip
    actions.apply(steps.plan_shadow(lay, steps.Options("1")))
    steps.check_shadow(lay, steps.Options("1"))
    (tmp_path / "p/usr/local/bin/pi").unlink()
    with pytest.raises(steps.InstallError, match="pi is not the shadow"):
        steps.check_shadow(lay, steps.Options("1"))
