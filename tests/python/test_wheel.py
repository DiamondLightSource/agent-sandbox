"""The built wheel: what ships, and that importing it needs only the stdlib.

These build the real wheel with `uv build`, unpack it, and run it in a
fresh `python -I` so neither the dev venv nor pytest's own imports leak in.
"""

from __future__ import annotations

import filecmp
import json
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]

# The wheel's tree/ mirrors these repository paths verbatim (ADR 23).
BUNDLED = [
    "install",
    "container/claude-container",
    ".devcontainer/claude-sandbox",
    ".devcontainer/claude-sandbox.conf",
    ".claude/statusline-command.sh",
    "skills",
]


@pytest.fixture(scope="module")
def unpacked_wheel(tmp_path_factory: pytest.TempPathFactory) -> Path:
    uv = shutil.which("uv")
    if uv is None:
        pytest.skip("uv is needed to build the wheel")
    out = tmp_path_factory.mktemp("dist")
    subprocess.run(
        [uv, "build", "--wheel", "--quiet", "-o", str(out), str(REPO)], check=True
    )
    (wheel,) = out.glob("claude_sandbox-*.whl")
    site = tmp_path_factory.mktemp("site")
    with zipfile.ZipFile(wheel) as zf:
        zf.extractall(site)
    return site


def _run_isolated(site: Path, code: str, *argv: str) -> str:
    """Run `code` under `python -I` with only the unpacked wheel added."""
    prelude = f"import sys; sys.path.insert(0, {str(site)!r})\n"
    result = subprocess.run(
        [sys.executable, "-I", "-c", prelude + code, *argv],
        check=True,
        capture_output=True,
        text=True,
        env={"PATH": os.environ.get("PATH", "")},
    )
    return result.stdout


def test_import_is_stdlib_only(unpacked_wheel: Path) -> None:
    """No third-party module is imported by the entry point (ADR 26)."""
    code = """
import json, sys, sysconfig
from pathlib import Path
import claude_sandbox
claude_sandbox._release_tag()
stdlib = [Path(sysconfig.get_paths()[k]).resolve() for k in ("stdlib", "platstdlib")]
pkg = Path(claude_sandbox.__file__).resolve().parent
bad = []
for name, mod in list(sys.modules.items()):
    f = getattr(mod, "__file__", None)
    if not f:
        continue  # builtin or frozen
    p = Path(f).resolve()
    if p.is_relative_to(pkg) or any(p.is_relative_to(s) for s in stdlib):
        continue
    bad.append(name)
print(json.dumps(bad))
"""
    assert json.loads(_run_isolated(unpacked_wheel, code)) == []


def test_bundled_tree_is_the_checkout(unpacked_wheel: Path) -> None:
    tree = unpacked_wheel / "claude_sandbox" / "tree"
    for rel in BUNDLED:
        src, dst = REPO / rel, tree / rel
        if src.is_dir():
            cmp = filecmp.dircmp(src, dst)
            assert not (cmp.left_only or cmp.right_only or cmp.diff_files), rel
        else:
            assert filecmp.cmp(src, dst, shallow=False), rel


def test_main_execs_the_bundled_launcher(unpacked_wheel: Path) -> None:
    """main() finds tree/ inside the installed package, not the checkout."""
    code = """
import json, os, sys
def execvpe(file, args, env):
    print(json.dumps([file, args]))
    sys.exit(0)
os.execvpe = execvpe
sys.argv = ["claude-sandbox", *sys.argv[1:]]
import claude_sandbox
claude_sandbox.main()
"""
    file, args = json.loads(_run_isolated(unpacked_wheel, code, "--help"))
    launcher = unpacked_wheel / "claude_sandbox" / "tree/container/claude-container"
    assert file == "bash"
    assert Path(args[1]).resolve() == launcher.resolve()
    assert launcher.is_file()
    assert args[2:] == ["--help"]
