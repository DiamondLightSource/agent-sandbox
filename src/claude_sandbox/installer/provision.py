"""The interpreter the shadow shim runs (ADR 26, "The interpreter cannot be
redirected"): a pinned, uv-managed CPython and a venv holding only this
package, under ``/usr/libexec/claude-sandbox/``, root-owned, pruned and
byte-compiled.

Never from uv's cache: ``~/.cache`` is writable from inside the jail, so
the interpreter is installed straight into ``UV_PYTHON_INSTALL_DIR`` under
``/usr/libexec``, and the venv is pinned to the resolved patch directory,
not uv's ``cpython-3.13-…`` minor-version symlink.

``install.sh``'s bootstrap runs ``main`` with the pinned interpreter it has
just had uv install, when ``CLAUDE_SANDBOX_IMPL=python`` is set.
"""

import argparse
import mmap
import os
import shutil
import stat
import struct
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

from .actions import Owner, Remove, apply

PYTHON_VERSION = "3.13.16"
# The uv the bootstrap fetches: uv only installs the CPython releases it
# knows, and 0.12.23 knows 3.13.16 (0.8.15 does not). Bump them together.
# install.sh reads these four lines; the checksums are of astral-sh/uv's
# release archives uv-<arch>-unknown-linux-gnu.tar.gz.
UV_VERSION = "0.12.23"
UV_SHA256_X86_64 = "9167d72b3319674b6303c4cbe071854bba13ebdf3d76b1a7cbdc175471fb66d6"
UV_SHA256_AARCH64 = "6524bd338177ed50d035d39354e12545e993bbeba2ecbddf0480c5b3a81d313f"
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
own = {m for m in own if not m.endswith(".__main__")}
for name in sorted(stdlib) + sorted(own):
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


def _harden(node: str, owner: Owner) -> None:
    if owner is not None:
        os.lchown(node, *owner)
    st = os.lstat(node)
    if not stat.S_ISLNK(st.st_mode) and st.st_mode & 0o022:
        os.chmod(node, stat.S_IMODE(st.st_mode) & ~0o022)


def harden(path: Path, owner: Owner) -> None:
    """Give ``owner`` ``path`` and everything under it, and take group and
    other write away: the jail must not be able to change what it runs."""
    _harden(str(path), owner)
    for dirpath, dirnames, filenames in os.walk(path):
        for name in [*dirnames, *filenames]:
            _harden(os.path.join(dirpath, name), owner)


def provision(
    uv: str,
    package: str,
    root: Path = ROOT,
    version: str = PYTHON_VERSION,
    environ: Mapping[str, str] = os.environ,
    owner: Owner = (0, 0),
    runner: Run = run,
) -> Path:
    """Install CPython ``version`` and a venv holding a copy of ``package``
    (the ``claude_sandbox`` directory of a clone or of an installed wheel)
    under ``root``; return the venv's interpreter."""
    old = os.umask(0o022)
    try:
        return _provision(uv, package, root, version, environ, owner, runner)
    finally:
        os.umask(old)


def _provision(
    uv: str,
    package: str,
    root: Path,
    version: str,
    environ: Mapping[str, str],
    owner: Owner,
    runner: Run,
) -> Path:
    env = uv_env(environ, root)
    reported = runner([uv, "--version"], env).split()
    if reported[1:2] != [UV_VERSION]:
        raise ProvisionError(f"{uv} is not uv {UV_VERSION}: {' '.join(reported)}")
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
    # Standard library only, with no dependencies, so a copy is the whole
    # install: no build, no index. The wheel's bundled bash (tree/) and any
    # bytecode stay behind.
    for site in venv.glob("lib/python3.*/site-packages"):
        shutil.copytree(
            package,
            site / "claude_sandbox",
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "tree"),
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
    # And uv's own bookkeeping, which nothing reads once this is done.
    apply(Remove(p) for p in store.glob("cpython-*") if p != home)
    leftovers = [store / n for n in (".temp", ".lock", ".gitignore")]
    leftovers += [venv / n for n in (".gitignore", "CACHEDIR.TAG")]
    apply(Remove(p) for p in leftovers)
    apply(Remove(p) for p in plan_prune(home))
    sources = [*home.glob("lib/python3.*"), *venv.glob("lib/python3.*/site-packages")]
    runner(
        [str(interpreter), "-I", "-m", "compileall", "-q", "-j0"]
        + [str(p) for p in sources],
        env,
    )
    _harden(str(root), owner)
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
    parser.add_argument("package", help="the claude_sandbox package directory")
    # ADR 26: nothing run as root is found through PATH or the environment.
    parser.add_argument("--uv", required=True, help="absolute path to uv")
    parser.add_argument("--root", type=Path, default=ROOT)
    args = parser.parse_args(argv)
    if not os.path.isabs(args.uv):
        parser.error("--uv must be an absolute path")
    owner = (0, 0) if os.geteuid() == 0 else None
    # uv runs from /, so a relative path would not resolve.
    package = os.path.abspath(args.package)
    python = provision(args.uv, package, args.root, owner=owner)
    print(f"{python}: {size(args.root / 'python') / 1e6:.1f} MB interpreter")


if __name__ == "__main__":
    main(sys.argv[1:])
