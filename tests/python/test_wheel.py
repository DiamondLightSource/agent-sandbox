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
from typing import Any

import pytest

REPO = Path(__file__).resolve().parents[2]

# The modules the launch path imports (ADR 26: standard library only).
LAUNCH_PATH = (
    "bwrap",
    "config",
    "errors",
    "gitconfig",
    "jail",
    "profiles",
    "shadow",
    "tools",
)

# Stub execvpe, run `claude-sandbox install` (in a container, so it would
# exec the bundled bootstrap), then import every module of the package (but
# __main__, which runs when imported; the shadow test below covers it) and report
# what main would exec, where the package was imported from, and every
# top-level module that all this added which is neither the stdlib nor the
# package (ADR 26: the package has no runtime dependencies). Site-packages stay
# on the path (python -I keeps the venv's), so a third-party import resolves
# and is reported rather than failing to import; what `site` loaded before the
# package (a venv's _virtualenv shim) is the environment, not the package.
DRIVER = """
import importlib, json, os, pkgutil, sys
before = set(sys.modules)
def execvpe(file, args, env):
    import claude_sandbox
    for mod in pkgutil.walk_packages(claude_sandbox.__path__, "claude_sandbox."):
        if mod.name != "claude_sandbox.__main__":  # importing it runs it
            importlib.import_module(mod.name)
    foreign = sorted({
        name.partition(".")[0] for name in set(sys.modules) - before
    } - set(sys.stdlib_module_names) - {"__main__", "claude_sandbox"})
    mine = sorted(m for m in sys.modules if m.startswith("claude_sandbox."))
    print(json.dumps({"file": file, "args": args, "foreign": foreign,
                      "pkg": claude_sandbox.__file__, "modules": mine}))
    sys.exit(0)
os.execvpe = execvpe
sys.argv = ["claude-sandbox", "install"]
import claude_sandbox
claude_sandbox.main()
"""

# The shim's own route: `python -I -m claude_sandbox _shadow pi -- x`, run
# through __main__ as -m runs it, inside a sandbox (IS_SANDBOX=1) so the
# recursion guard execs the agent at once. execve is stubbed to report every
# top-level module loaded by then that is neither the stdlib nor the package,
# and which of the package's modules were loaded.
SHADOW_DRIVER = """
import json, os, runpy, sys
before = set(sys.modules)
def execve(path, argv, env):
    loaded = set(sys.modules) - before
    foreign = sorted({name.partition(".")[0] for name in loaded}
                     - set(sys.stdlib_module_names) - {"__main__", "claude_sandbox"})
    own = sorted(n for n in loaded if n.startswith("claude_sandbox"))
    import claude_sandbox
    print(json.dumps({"argv": argv, "foreign": foreign, "own": own,
                      "pkg": claude_sandbox.__file__}))
    sys.stdout.flush()
    os._exit(0)
os.execve = execve
sys.argv = ["claude_sandbox", "_shadow", "pi", "--", "x"]
runpy.run_module("claude_sandbox", run_name="__main__", alter_sys=True)
"""


@pytest.fixture(scope="module")
def site(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The built wheel, unpacked."""
    uv = shutil.which("uv")
    if uv is None:
        pytest.skip("uv is needed to build the wheel")
    tmp_path = tmp_path_factory.mktemp("wheel")
    subprocess.run(
        [uv, "build", "--wheel", "--quiet", "-o", str(tmp_path), str(REPO)],
        check=True,
    )
    (wheel,) = tmp_path.glob("claude_sandbox-*.whl")
    site = tmp_path / "site"
    with zipfile.ZipFile(wheel) as zf:
        zf.extractall(site)
    return site


def run_isolated(site: Path, driver: str, env: dict[str, str]) -> dict[str, Any]:
    out = subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            f"import sys; sys.path.insert(0, {str(site)!r})\n" + driver,
        ],
        check=True,
        capture_output=True,
        text=True,
        env={"PATH": os.environ.get("PATH", ""), **env},
    ).stdout
    result: dict[str, Any] = json.loads(out)
    assert Path(result["pkg"]).resolve().is_relative_to(site.resolve())
    return result


IN_CONTAINER = {"container": "podman"}

# What the bundled tree holds: the bootstrap and what the installer places.
TREE = {
    "install",
    ".claude/statusline-command.sh",
    ".devcontainer/claude-sandbox.conf",
    *(
        f".devcontainer/claude-sandbox/{name}"
        for name in (
            "alerts-prompt.sh",
            "claude-sandbox-shim",
            "claude-shim",
            "codex-launch",
            "install.sh",
            "pi-run",
            "pi-sandbox-tag.ts",
            "pi-system.md",
            "verify-sandbox-battery.sh",
        )
    ),
}


def test_wheel_imports_only_the_stdlib(site: Path) -> None:
    result = run_isolated(site, DRIVER, IN_CONTAINER)
    tree = site / "claude_sandbox" / "tree"
    assert result["file"] == "/bin/bash"
    assert Path(result["args"][1]).resolve() == (tree / "install").resolve()
    shipped = {str(p.relative_to(tree)) for p in tree.rglob("*") if p.is_file()}
    assert {p for p in shipped if not p.startswith("skills/")} == TREE
    assert "skills/verify-sandbox/SKILL.md" in shipped
    # ADR 26: no runtime dependencies, so no third-party import anywhere.
    modules = {f"claude_sandbox.{m}" for m in (*LAUNCH_PATH, "cli")}
    assert modules <= set(result["modules"])
    assert result["foreign"] == []


def test_the_stdlib_check_can_fail(site: Path) -> None:
    planted = site / "claude_sandbox" / "planted.py"
    planted.write_text("import pytest\n")
    try:
        assert "pytest" in run_isolated(site, DRIVER, IN_CONTAINER)["foreign"]
    finally:
        planted.unlink()


def test_shadow_entry_uses_only_the_stdlib(site: Path) -> None:
    result = run_isolated(site, SHADOW_DRIVER, {"IS_SANDBOX": "1", "HOME": "/h"})
    assert result["argv"] == ["/usr/libexec/claude-sandbox/pi-run", "x"]
    # The whole launch path was imported on the way, and nothing else.
    assert {f"claude_sandbox.{m}" for m in LAUNCH_PATH} <= set(result["own"])
    assert result["foreign"] == []
