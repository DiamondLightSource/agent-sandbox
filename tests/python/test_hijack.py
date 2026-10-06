"""The interpreter cannot be redirected (ADR 26).

Builds a venv holding the package, points a copy of the real shim at it,
plants every start-up hook an attacker who controls the workspace, $HOME or
the environment could use, launches through the shim, and asserts none of
them ran. The control run drops ``-I`` from the same shim and shows the
plants are live, so a pass is not an accident of the fixture.
"""

import os
import shutil
import subprocess
import sys
import venv
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SHIM = REPO / ".devcontainer/claude-sandbox/claude-shim"
INSTALLED = "/usr/libexec/claude-sandbox/venv/bin/python"


def plant(path: Path, marker: Path, *, pth: bool = False) -> None:
    """A hook that records it ran by creating ``marker``."""
    code = f"open({str(marker)!r}, 'w').close()"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"import os; {code}\n" if pth else f"{code}\n")


@pytest.fixture(scope="module")
def built_venv(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("venv")
    venv.EnvBuilder(with_pip=False, symlinks=True).create(root)
    (site,) = root.glob("lib/python3.*/site-packages")
    shutil.copytree(
        REPO / "src/claude_sandbox",
        site / "claude_sandbox",
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    return root


@pytest.mark.parametrize("isolated", [True, False], ids=["shim", "control-no-I"])
def test_planted_hooks_never_run(
    built_venv: Path, tmp_path: Path, isolated: bool
) -> None:
    shim = SHIM.read_text()
    assert f"exec {INSTALLED} -I -m claude_sandbox" in shim
    shim = shim.replace(INSTALLED, str(built_venv / "bin/python"))
    if not isolated:
        shim = shim.replace(" -I ", " ")
    claude = tmp_path / "bin/claude"
    claude.parent.mkdir()
    claude.write_text(shim)
    claude.chmod(0o755)

    marks = tmp_path / "marks"
    marks.mkdir()
    home, evil, work, fake = (tmp_path / d for d in ("home", "evil", "work", "fake"))
    version = f"python{sys.version_info.major}.{sys.version_info.minor}"
    user_site = home / ".local/lib" / version / "site-packages"
    # PYTHONPATH: start-up hooks, and a package that would replace ours.
    plant(evil / "sitecustomize.py", marks / "pythonpath-sitecustomize")
    plant(evil / "usercustomize.py", marks / "pythonpath-usercustomize")
    plant(evil / "claude_sandbox/__init__.py", marks / "pythonpath-package")
    plant(evil / "claude_sandbox/__main__.py", marks / "pythonpath-package-main")
    # $HOME: the user site-packages directory, with a .pth file.
    plant(user_site / "usercustomize.py", marks / "user-usercustomize")
    plant(user_site / "evil.pth", marks / "user-pth", pth=True)
    # The workspace (the cwd): `-m` would put it first on sys.path.
    plant(work / "sitecustomize.py", marks / "cwd-sitecustomize")
    plant(work / "claude_sandbox/__init__.py", marks / "cwd-package")
    plant(work / "claude_sandbox/__main__.py", marks / "cwd-package-main")
    # PATH: a python that would run if the shim looked one up.
    fake.mkdir()
    for name in ("python", "python3"):
        (fake / name).write_text(f"#!/bin/sh\ntouch {marks / ('path-' + name)}\n")
        (fake / name).chmod(0o755)
    # The real agent the recursion guard execs inside an existing sandbox.
    real = home / ".local/bin/claude"
    real.parent.mkdir(parents=True)
    real.write_text('#!/bin/sh\necho "REAL-CLAUDE $*"\n')
    real.chmod(0o755)

    env = {
        "HOME": str(home),
        "PATH": f"{fake}:/usr/bin:/bin",
        "PYTHONPATH": str(evil),
        "PYTHONSTARTUP": str(evil / "sitecustomize.py"),
        "PYTHONUSERBASE": str(home / ".local"),
        "IS_SANDBOX": "1",  # launch through the recursion guard: no bwrap
    }
    proc = subprocess.run(
        [str(claude), "--chrome", "hello"],
        cwd=work,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    ran = sorted(os.listdir(marks))
    if isolated:
        assert ran == []
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout == "REAL-CLAUDE --no-chrome hello\n"
    else:
        # Without -I the workspace and PYTHONPATH take over the interpreter.
        assert "cwd-package" in ran
        assert "pythonpath-sitecustomize" in ran
