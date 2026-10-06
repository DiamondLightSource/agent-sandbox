"""The interpreter the shadow shim runs (ADR 26, "The interpreter cannot be
redirected"): a pinned, uv-managed CPython and a venv holding only this
package, under ``/usr/libexec/claude-sandbox/``, root-owned, pruned and
byte-compiled.

Never from uv's cache: ``~/.cache`` is writable from inside the jail, so
the interpreter is installed straight into ``UV_PYTHON_INSTALL_DIR`` under
``/usr/libexec``, and the venv is pinned to the resolved patch directory,
not uv's ``cpython-3.13-…`` minor-version symlink.

Not wired in yet: the bootstrap that will call ``provision`` is the second
part of issue #72 phase 4.
"""

import argparse
import mmap
import os
import stat
import struct
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

from .actions import Owner, Remove, apply

PYTHON_VERSION = "3.13.16"
ROOT = Path("/usr/libexec/claude-sandbox")

# The installed interpreter is checked against what the package imports
# plus these, so that a later module using the usual stdlib still runs.
MARGIN = (
    "argparse ast compileall configparser ctypes dataclasses decimal errno "
    "fcntl hashlib importlib.resources io json mmap os pathlib pty re runpy "
    "selectors shlex shutil signal socket stat struct subprocess sys tempfile "
    "termios textwrap time tomllib tty typing zipfile"
).split()

# What the venv never uses, relative to the interpreter's directory. Kept:
# all of lib-dynload but _tkinter (the rest of the extensions, _ctypes
# included, are built into the static binary), pydoc_data, and the
# config-3.x directory sysconfig reads.
PRUNE = (
    "bin/idle*",
    "bin/pip*",
    "include",  # C headers
    "share",  # man pages and terminfo, for the interactive REPL only
    "lib/pkgconfig",
    "lib/itcl*",  # Tcl/Tk
    "lib/libtcl*",
    "lib/libtk*",
    "lib/tcl*",
    "lib/thread*",
    "lib/tk*",
    "lib/python3.*/ensurepip",
    "lib/python3.*/idlelib",
    "lib/python3.*/lib-dynload/_tkinter.*",
    "lib/python3.*/site-packages/pip",
    "lib/python3.*/site-packages/pip-*",
    "lib/python3.*/test",
    "lib/python3.*/*/test",
    "lib/python3.*/*/tests",
    "lib/python3.*/tkinter",
    "lib/python3.*/turtle.py",
    "lib/python3.*/turtledemo",
)

# Run inside the provisioned venv: import every stdlib module the package
# names, every module of the package, and the margin.
VERIFY = """
import ast, importlib, pathlib, pkgutil, sys
import claude_sandbox
pkg = pathlib.Path(claude_sandbox.__file__).parent
names = set(sys.argv[1:])
for py in pkg.rglob("*.py"):
    for node in ast.walk(ast.parse(py.read_bytes())):
        if isinstance(node, ast.Import):
            names.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and not node.level and node.module:
            names.add(node.module)
stdlib = {n for n in names if n.partition(".")[0] in sys.stdlib_module_names}
own = {m.name for m in pkgutil.walk_packages([str(pkg)], "claude_sandbox.")}
for name in sorted(stdlib) + sorted(own - {"claude_sandbox.__main__"}):
    importlib.import_module(name)
assert sys.flags.isolated and "_virtualenv" not in sys.modules
assert sys.prefix != sys.base_prefix, "not running in the venv"
print(len(stdlib), "stdlib and", len(own), "package modules import")
"""

Run = Callable[[Sequence[str], Mapping[str, str]], str]


class ProvisionError(RuntimeError):
    pass


def run(argv: Sequence[str], env: Mapping[str, str]) -> str:
    """Run a command from ``/`` (no workspace config), returning stdout."""
    return subprocess.run(
        argv, env=dict(env), cwd="/", check=True, stdout=subprocess.PIPE, text=True
    ).stdout


def uv_env(environ: Mapping[str, str], root: Path) -> dict[str, str]:
    """The caller's environment without anything that steers uv or Python,
    plus the settings that keep everything under ``root``."""
    env = {
        k: v
        for k, v in environ.items()
        if not k.startswith(("UV_", "PYTHON", "VIRTUAL_ENV", "CONDA_"))
    }
    env.update(
        UV_PYTHON_INSTALL_DIR=str(root / "python"),
        UV_NO_CACHE="1",
        UV_NO_CONFIG="1",
        UV_PYTHON_PREFERENCE="only-managed",
    )
    return env


def elf_needed(path: Path) -> list[str] | None:
    """The shared libraries a 64-bit little-endian ELF file links, or None
    when ``path`` is not one this can read."""
    with path.open("rb") as f, mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as m:
        if m[:6] != b"\x7fELF\x02\x01":
            return None
        try:
            (phoff,) = struct.unpack_from("<Q", m, 0x20)
            phentsize, phnum = struct.unpack_from("<HH", m, 0x36)
            loads: list[tuple[int, int, int]] = []
            dynamic: tuple[int, int] | None = None
            for i in range(phnum):
                kind, _, offset, vaddr, _, size = struct.unpack_from(
                    "<IIQQQQ", m, phoff + i * phentsize
                )
                if kind == 1:  # PT_LOAD
                    loads.append((vaddr, offset, size))
                elif kind == 2:  # PT_DYNAMIC
                    dynamic = (offset, size)
            if dynamic is None:
                return []
            tags = [
                struct.unpack_from("<qQ", m, at)
                for at in range(dynamic[0], dynamic[0] + dynamic[1], 16)
            ]
            strtab = next(v for t, v in tags if t == 5)  # DT_STRTAB
            base = next(o + strtab - va for va, o, n in loads if va <= strtab < va + n)
            names: list[str] = []
            for tag, value in tags:
                if tag == 0:  # DT_NULL
                    break
                if tag == 1:  # DT_NEEDED
                    start = base + value
                    names.append(m[start : m.find(b"\0", start)].decode())
            return names
        except (struct.error, StopIteration):
            return None


def plan_prune(home: Path) -> list[Path]:
    """What to delete from the interpreter at ``home``, by ``PRUNE`` and one
    measured case: ``libpython`` duplicates a static ``python3.x`` binary,
    so it goes only when neither the binary nor any extension module that
    remains links it."""
    doomed = sorted({p for pattern in PRUNE for p in home.glob(pattern)})
    binaries = sorted(home.glob("bin/python3.*[0-9]"))
    binaries += [
        p for p in home.glob("lib/python3.*/lib-dynload/*.so") if p not in doomed
    ]
    needs = [elf_needed(p) for p in binaries]
    if binaries and all(
        n is not None and not any(lib.startswith("libpython") for lib in n)
        for n in needs
    ):
        doomed += sorted(home.glob("lib/libpython3*.so*"))
    return doomed


def size(path: Path) -> int:
    """Bytes under ``path``, as ``du`` would count them (no links followed)."""
    total = 0
    for dirpath, _, filenames in os.walk(path):
        for name in filenames:
            total += os.lstat(os.path.join(dirpath, name)).st_size
    return total


def harden(path: Path, owner: Owner) -> None:
    """Give ``owner`` everything under ``path``, and take group and other
    write away: the jail must not be able to change what it runs."""
    for dirpath, dirnames, filenames in os.walk(path):
        for name in [".", *dirnames, *filenames]:
            node = os.path.join(dirpath, name)
            if owner is not None:
                os.lchown(node, *owner)
            st = os.lstat(node)
            if not stat.S_ISLNK(st.st_mode) and st.st_mode & 0o022:
                os.chmod(node, stat.S_IMODE(st.st_mode) & ~0o022)


def provision(
    uv: str,
    package: str,
    root: Path = ROOT,
    version: str = PYTHON_VERSION,
    environ: Mapping[str, str] = os.environ,
    owner: Owner = (0, 0),
    runner: Run = run,
) -> Path:
    """Install CPython ``version`` and a venv holding ``package`` (a wheel)
    under ``root``; return the venv's interpreter."""
    env = uv_env(environ, root)
    runner([uv, "python", "install", "--no-bin", version], env)
    found = runner([uv, "python", "find", version], env).strip()
    interpreter = Path(os.path.realpath(found))
    store = Path(os.path.realpath(root / "python"))
    if not interpreter.is_relative_to(store):
        raise ProvisionError(f"uv found {interpreter}, outside {store}")
    venv = root / "venv"
    # By version, not path: given a path, uv points the venv at the
    # minor-version symlink so that it follows upgrades.
    runner([uv, "venv", "--clear", "--python", version, str(venv)], env)
    venv_home = _venv_home(venv / "pyvenv.cfg")
    if venv_home != interpreter.parent:
        raise ProvisionError(f"the venv runs {venv_home}, not {interpreter.parent}")
    python = venv / "bin" / "python"
    # Stdlib only, no runtime dependencies: nothing else may come along.
    runner(
        [uv, "pip", "install", "--python", str(python), "--no-deps", "--reinstall"]
        + [package],
        env,
    )
    # uv's _virtualenv.pth costs every start a few ms; the console script
    # and activate scripts would run the venv without -I.
    for extra in [
        *venv.glob("lib/python3.*/site-packages/_virtualenv.*"),
        *(p for p in (venv / "bin").iterdir() if not p.name.startswith("python")),
    ]:
        extra.unlink()
    home = interpreter.parent.parent
    # Earlier pins and uv's minor-version links: nothing may run them now.
    apply(Remove(p) for p in store.glob("cpython-*") if p != home)
    apply(Remove(p) for p in plan_prune(home))
    sources = [*home.glob("lib/python3.*"), *venv.glob("lib/python3.*/site-packages")]
    runner(
        [str(interpreter), "-I", "-m", "compileall", "-q", "-j0"]
        + [str(p) for p in sources],
        env,
    )
    harden(store, owner)
    harden(venv, owner)
    runner([str(python), "-I", "-c", VERIFY, *MARGIN], env)
    return python


def _venv_home(cfg: Path) -> Path:
    for line in cfg.read_text().splitlines():
        key, _, value = line.partition("=")
        if key.strip() == "home":
            return Path(value.strip())
    raise ProvisionError(f"{cfg} names no home")


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Install the pinned interpreter.")
    parser.add_argument("package", help="the claude-sandbox wheel to install")
    # ADR 26: nothing run as root is found through PATH or the environment.
    parser.add_argument("--uv", required=True, help="absolute path to uv")
    parser.add_argument("--root", type=Path, default=ROOT)
    args = parser.parse_args(argv)
    if not os.path.isabs(args.uv):
        parser.error("--uv must be an absolute path")
    owner = (0, 0) if os.geteuid() == 0 else None
    python = provision(args.uv, args.package, args.root, owner=owner)
    print(f"{python}: {size(args.root / 'python') / 1e6:.1f} MB interpreter")


if __name__ == "__main__":
    main(sys.argv[1:])
