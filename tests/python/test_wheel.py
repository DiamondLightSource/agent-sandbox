"""The built wheel, run the way an installed one runs.

Builds the real wheel with `uv build`, unpacks it, and runs its entry point
in a fresh `python -I` so neither the dev venv nor pytest's imports leak in.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]

# Stub execvpe, run main(), then report what it would exec and every module
# imported from outside the stdlib and the package itself.
DRIVER = """
import json, os, sys, sysconfig
from pathlib import Path
def execvpe(file, args, env):
    import claude_sandbox
    pkg = Path(claude_sandbox.__file__).resolve().parent
    std = [Path(sysconfig.get_paths()[k]).resolve() for k in ("stdlib", "platstdlib")]
    foreign = sorted(
        name for name, mod in sys.modules.items()
        if getattr(mod, "__file__", None)
        and not any(Path(mod.__file__).resolve().is_relative_to(d) for d in [pkg, *std])
    )
    print(json.dumps({"file": file, "args": args, "foreign": foreign}))
    sys.exit(0)
os.execvpe = execvpe
sys.argv = ["claude-sandbox", "--help"]
import claude_sandbox
claude_sandbox.main()
"""


def test_wheel_execs_its_bundled_launcher_using_only_the_stdlib(
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
    assert result["file"] == "bash"
    assert Path(result["args"][1]).resolve() == launcher.resolve()
    # ADR 26: no third-party import on the launch path.
    assert result["foreign"] == []
