"""The PyPI entry point (ADR 23): locate the bundled bash, set env, exec.

Until issue #72 phase 5 the wheel's one module only hands over to the bash
launcher and installer; these pin what it execs and with which environment.
"""

import os
import sys
from typing import NoReturn

import pytest

import claude_sandbox
from claude_sandbox import _version


class Exec(Exception):
    """Raised by the stub execvpe in place of replacing the process."""

    def __init__(self, file: str, args: list[str], env: dict[str, str]) -> None:
        super().__init__(file)
        self.file, self.argv, self.env = file, list(args), dict(env)


def _execvpe(file: str, args: list[str], env: dict[str, str]) -> NoReturn:
    raise Exec(file, args, env)


def _main(monkeypatch: pytest.MonkeyPatch, version: str, *argv: str) -> Exec:
    """Run main() at a given wheel version with execvpe stubbed out."""
    monkeypatch.setattr(os, "execvpe", _execvpe)
    monkeypatch.setattr(_version, "__version__", version)
    monkeypatch.setattr(sys, "argv", ["claude-sandbox", *argv])
    for var in ("CLAUDE_SANDBOX_IMAGE", "CLAUDE_SANDBOX_VERSION"):
        monkeypatch.delenv(var, raising=False)
    with pytest.raises(Exec) as exc:
        claude_sandbox.main()
    return exc.value


@pytest.mark.parametrize(
    ("version", "tag"),
    [
        ("4.7.2", "4.7.2"),
        ("4.0.0a3", "4.0.0-alpha.3"),
        ("4.0.0b1", "4.0.0-beta.1"),
        ("4.0.0rc2", "4.0.0-rc.2"),
        ("4.7.2.dev5+g26a246c8a", None),  # between tags: no image of its own
    ],
)
def test_launcher_pins_the_image_to_the_git_tag(
    monkeypatch: pytest.MonkeyPatch, version: str, tag: str | None
) -> None:
    ex = _main(monkeypatch, version, "--help")
    assert ex.file == "bash"
    assert ex.argv[1].endswith(os.path.join("tree", "container", "claude-container"))
    assert ex.argv[2:] == ["--help"]
    assert ex.env["CLAUDE_SANDBOX_IMAGE"] == f"{claude_sandbox.IMAGE}:{tag or 'latest'}"
    assert ex.env["CLAUDE_SANDBOX_LAUNCHER_VERSION"] == (tag or version)
    assert ex.env["CLAUDE_SANDBOX_LAUNCHER"] == "uvx"


def test_install_runs_the_bundled_installer_in_a_container(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("container", "podman")
    ex = _main(monkeypatch, "4.0.0b1", "install", "--here")
    assert ex.argv[1].endswith(os.path.join("tree", "install"))
    assert ex.argv[2:] == ["--here"]
    assert ex.env["CLAUDE_SANDBOX_VERSION"] == "4.0.0-beta.1"
    assert ex.env["CLAUDE_SANDBOX_INSTALLER"] == "uvx"


def test_install_refuses_outside_a_container(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def absent(path: str) -> bool:  # neither /run/.containerenv nor /.dockerenv
        return False

    monkeypatch.setattr(os.path, "exists", absent)
    for var in ("container", "CLAUDE_SANDBOX_HOST_INSTALL"):
        monkeypatch.delenv(var, raising=False)
    with pytest.raises(SystemExit) as exc:
        _main(monkeypatch, "4.7.2", "install")
    assert exc.value.code == 1
    assert "refusing to install outside a container" in capsys.readouterr().err
