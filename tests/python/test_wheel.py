"""The built wheel, run the way an installed one runs.

Builds the real wheel with `uv build`, unpacks it, and runs its entry point
in a fresh `python -I` so neither the dev venv nor pytest's imports leak in.
"""

import json
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]

# The modules the launch path will import (ADR 26: standard library only).
LAUNCH_PATH = ("bwrap", "config", "errors", "gitconfig", "profiles")

# Stub execvpe, run main(), then report what it would exec, where the package
# was imported from, and every top-level module that importing and running it,
# and importing the launch-path modules, added which is neither the stdlib nor
# the package. Site-packages stay on the path (python -I keeps the venv's), so
# a third-party import resolves and is reported rather than failing to import;
# what `site` loaded before the package (a venv's _virtualenv shim) is the
# environment, not the launch path.
DRIVER = f"""
import importlib, json, os, sys
before = set(sys.modules)
def execvpe(file, args, env):
    import claude_sandbox
    for name in {LAUNCH_PATH!r}:
        importlib.import_module("claude_sandbox." + name)
    foreign = sorted({{
        name.partition(".")[0] for name in set(sys.modules) - before
    }} - set(sys.stdlib_module_names) - {{"__main__", "claude_sandbox"}})
    print(json.dumps({{"file": file, "args": args, "foreign": foreign,
                      "pkg": claude_sandbox.__file__}}))
    sys.exit(0)
os.execvpe = execvpe
sys.argv = ["claude-sandbox", "--help"]
import claude_sandbox
claude_sandbox.main()
"""


def test_wheel_launch_path_uses_only_the_stdlib(
    tmp_path: Path,
) -> None:
    uv = shutil.which("uv")
    if uv is None:
        pytest.skip("uv is needed to build the wheel")
    subprocess.run(
        [uv, "build", "--wheel", "--quiet", "-o", str(tmp_path), str(REPO)],
        check=True,
    )
    (wheel,) = tmp_path.glob("claude_sandbox-*.whl")
    site = tmp_path / "site"
    with zipfile.ZipFile(wheel) as zf:
        zf.extractall(site)

    out = subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            f"import sys; sys.path.insert(0, {str(site)!r})\n" + DRIVER,
        ],
        check=True,
        capture_output=True,
        text=True,
        env={"PATH": os.environ.get("PATH", "")},
    ).stdout
    result = json.loads(out)

    launcher = site / "claude_sandbox" / "tree" / "container" / "claude-container"
    assert launcher.is_file()
    assert Path(result["pkg"]).resolve().is_relative_to(site.resolve())
    assert result["file"] == "bash"
    assert Path(result["args"][1]).resolve() == launcher.resolve()
    # ADR 26: no third-party import on the launch path.
    assert result["foreign"] == []
