"""The PyPI entry point (ADR 23): locate the bundled bash, set env, exec.

Until issue #72 phase 5 the wheel's one module only hands over to the bash
launcher and installer, so these tests pin that hand-over: what it execs,
with which environment, and that it stays standard library only.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Iterator
from importlib import metadata
from typing import NoReturn

import pytest

import claude_sandbox
from claude_sandbox import _version


class Exec(Exception):
    """Raised by the stub execvpe in place of replacing the process."""

    def __init__(self, file: str, args: list[str], env: dict[str, str]) -> None:
        super().__init__(file)
        self.file = file
        self.args_ = args
        self.env = env


def _stub_execvpe(file: str, args: list[str], env: dict[str, str]) -> NoReturn:
    raise Exec(file, list(args), dict(env))


@pytest.fixture
def run_main(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(os, "execvpe", _stub_execvpe)
    for var in (
        "CLAUDE_SANDBOX_IMAGE",
        "CLAUDE_SANDBOX_LAUNCHER_VERSION",
        "CLAUDE_SANDBOX_VERSION",
        "CLAUDE_SANDBOX_HOST_INSTALL",
    ):
        monkeypatch.delenv(var, raising=False)
    yield


def _main(monkeypatch: pytest.MonkeyPatch, *argv: str) -> Exec:
    monkeypatch.setattr(sys, "argv", ["claude-sandbox", *argv])
    with pytest.raises(Exec) as exc:
        claude_sandbox.main()
    return exc.value


@pytest.mark.parametrize(
    ("wheel", "tag"),
    [
        ("4.7.2", "4.7.2"),
        ("4.0.0a3", "4.0.0-alpha.3"),
        ("4.0.0b1", "4.0.0-beta.1"),
        ("4.0.0rc2", "4.0.0-rc.2"),
        ("4.7.2.dev5+g26a246c8a", "4.7.2.dev5+g26a246c8a"),
    ],
)
def test_release_tag_maps_pep440_back_to_git_tag(
    monkeypatch: pytest.MonkeyPatch, wheel: str, tag: str
) -> None:
    monkeypatch.setattr(_version, "__version__", wheel)
    assert claude_sandbox._release_tag() == tag  # pyright: ignore[reportPrivateUsage]


def test_version_metadata_resolves() -> None:
    """hatch-vcs writes _version.py and the dist metadata from one tag."""
    assert metadata.version("claude-sandbox") == _version.__version__
    assert _version.__version__[0].isdigit()


@pytest.mark.usefixtures("run_main")
def test_launcher_pins_image_to_release(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_version, "__version__", "4.0.0b1")
    ex = _main(monkeypatch, "--help")
    assert ex.file == "bash"
    assert ex.args_[1].endswith(os.path.join("tree", "container", "claude-container"))
    assert ex.args_[2:] == ["--help"]
    assert ex.env["CLAUDE_SANDBOX_IMAGE"] == f"{claude_sandbox.IMAGE}:4.0.0-beta.1"
    assert ex.env["CLAUDE_SANDBOX_LAUNCHER_VERSION"] == "4.0.0-beta.1"
    assert ex.env["CLAUDE_SANDBOX_LAUNCHER"] == "uvx"


@pytest.mark.usefixtures("run_main")
def test_dev_build_falls_back_to_latest_image(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_version, "__version__", "4.7.2.dev5+g26a246c8a")
    ex = _main(monkeypatch, "claude")
    assert ex.env["CLAUDE_SANDBOX_IMAGE"] == f"{claude_sandbox.IMAGE}:latest"


@pytest.mark.usefixtures("run_main")
def test_install_refuses_outside_a_container(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(claude_sandbox, "_in_container", lambda: False)
    monkeypatch.setattr(sys, "argv", ["claude-sandbox", "install"])
    with pytest.raises(SystemExit) as exc:
        claude_sandbox.main()
    assert exc.value.code == 1
    assert "refusing to install outside a container" in capsys.readouterr().err


@pytest.mark.usefixtures("run_main")
def test_install_execs_bundled_installer_in_a_container(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(claude_sandbox, "_in_container", lambda: True)
    monkeypatch.setattr(_version, "__version__", "4.7.2")
    ex = _main(monkeypatch, "install", "--here")
    assert ex.args_[1].endswith(os.path.join("tree", "install"))
    assert ex.args_[2:] == ["--here"]
    assert ex.env["CLAUDE_SANDBOX_VERSION"] == "4.7.2"
    assert ex.env["CLAUDE_SANDBOX_INSTALLER"] == "uvx"
