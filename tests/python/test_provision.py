"""Interpreter provisioning, against a stand-in for uv.

The real run (uv downloading CPython into a bare ``node:22-slim``) is
measured in a container, not here; this pins what provisioning asks uv
for, what it removes, and when it refuses.
"""

import os
import shutil
import struct
from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest

from claude_sandbox.installer import provision as p

TRUE = Path(shutil.which("true") or "/bin/true").resolve()
NAME = "cpython-3.13.16-linux-x86_64-gnu"


class FakeUv:
    """Builds the layout uv and python-build-standalone produce."""

    def __init__(self, root: Path, home_in_venv: str | None = None) -> None:
        self.root, self.calls = root, list[tuple[list[str], dict[str, str]]]()
        self.found: str | None = None
        self.home_in_venv = home_in_venv

    def __call__(self, argv: Sequence[str], env: Mapping[str, str]) -> str:
        self.calls.append((list(argv), dict(env)))
        store = Path(env["UV_PYTHON_INSTALL_DIR"])
        home = store / NAME
        match list(argv[1:3]):
            case ["python", "install"]:
                for d in ("include", "share/man", "lib/tcl9.0", "lib/python3.13/test"):
                    (home / d).mkdir(parents=True)
                lib = home / "lib/python3.13"
                for f in ("os.py", "tkinter/__init__.py", "site-packages/pip/x.py"):
                    (lib / f).parent.mkdir(parents=True, exist_ok=True)
                    (lib / f).write_text("")
                (lib / "os.py").chmod(0o666)
                (home / "bin").mkdir()
                shutil.copy(TRUE, home / "bin/python3.13")
                (lib / "lib-dynload").mkdir()
                shutil.copy(TRUE, lib / "lib-dynload/_dbm.cpython-313.so")
                shutil.copy(TRUE, lib / "lib-dynload/_tkinter.cpython-313.so")
                (home / "lib/libpython3.13.so.1.0").write_bytes(b"\x7fELF")
                (home / "lib/libpython3.13.so").symlink_to("libpython3.13.so.1.0")
                (store / "cpython-3.13-linux-x86_64-gnu").symlink_to(home)
                (store / "cpython-3.12.1-linux-x86_64-gnu").mkdir()
            case ["python", "find"]:
                return (
                    self.found
                    or f"{store}/cpython-3.13-linux-x86_64-gnu/bin/python3.13\n"
                )
            case ["venv", *_]:
                venv = Path(argv[-1])
                site = venv / "lib/python3.13/site-packages"
                site.mkdir(parents=True)
                (site / "_virtualenv.pth").write_text("import _virtualenv")
                (site / "_virtualenv.py").write_text("")
                (venv / "bin").mkdir()
                (venv / "bin/python").symlink_to(home / "bin/python3.13")
                (venv / "bin/activate").write_text("")
                (venv / "bin/claude-sandbox").write_text("#!venv/bin/python\n")
                cfg = (
                    f"home = {self.home_in_venv or home / 'bin'}\n"
                    if self.home_in_venv != ""
                    else ""
                )
                (venv / "pyvenv.cfg").write_text(cfg + "version_info = 3.13.16\n")
            case _:
                pass
        return ""


def test_provision_pins_prunes_and_hardens(tmp_path: Path) -> None:
    root = tmp_path / "libexec"
    uv = FakeUv(root)
    env = {"PATH": "/usr/bin", "UV_CACHE_DIR": "/home/u/.cache/uv", "PYTHONPATH": "x"}
    python = p.provision(
        "/usr/bin/uv",
        "/w.whl",
        root,
        environ=env,
        owner=(os.getuid(), os.getgid()),
        runner=uv,
    )
    home = root / "python" / NAME
    assert python == root / "venv/bin/python"
    argvs = [argv for argv, _ in uv.calls]
    assert argvs[:2] == [
        ["/usr/bin/uv", "python", "install", "--no-bin", "3.13.16"],
        ["/usr/bin/uv", "python", "find", "3.13.16"],
    ]
    assert argvs[2] == [
        "/usr/bin/uv",
        "venv",
        "--clear",
        "--python",
        "3.13.16",
        f"{root}/venv",
    ]
    assert argvs[3][-3:] == ["--no-deps", "--reinstall", "/w.whl"]
    assert argvs[4][:6] == [
        f"{home}/bin/python3.13",
        "-I",
        "-m",
        "compileall",
        "-q",
        "-j0",
    ]
    assert argvs[5][:3] == [str(python), "-I", "-c"] and "tomllib" in argvs[5]
    for _, used in uv.calls:
        assert used["UV_PYTHON_INSTALL_DIR"] == f"{root}/python"
        assert used["UV_NO_CACHE"] == "1" and "UV_CACHE_DIR" not in used
        assert "PYTHONPATH" not in used
    left = sorted(str(q.relative_to(root)) for q in root.rglob("*"))
    assert left == [
        "python",
        f"python/{NAME}",
        f"python/{NAME}/bin",
        f"python/{NAME}/bin/python3.13",
        f"python/{NAME}/lib",
        f"python/{NAME}/lib/python3.13",
        f"python/{NAME}/lib/python3.13/lib-dynload",
        f"python/{NAME}/lib/python3.13/lib-dynload/_dbm.cpython-313.so",
        f"python/{NAME}/lib/python3.13/os.py",
        f"python/{NAME}/lib/python3.13/site-packages",
        "venv",
        "venv/bin",
        "venv/bin/python",
        "venv/lib",
        "venv/lib/python3.13",
        "venv/lib/python3.13/site-packages",
        "venv/pyvenv.cfg",
    ]
    assert (home / "lib/python3.13/os.py").stat().st_mode & 0o777 == 0o644


def test_libpython_stays_when_something_links_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    FakeUv(tmp_path)(
        ["uv", "python", "install"], {"UV_PYTHON_INSTALL_DIR": str(tmp_path)}
    )
    home = tmp_path / NAME
    assert home / "lib/libpython3.13.so.1.0" in p.plan_prune(home)
    for needed in (["libpython3.13.so.1.0"], None):  # links it, or unreadable

        def fake(path: Path, needed: list[str] | None = needed) -> list[str] | None:
            return needed

        monkeypatch.setattr(p, "elf_needed", fake)
        assert home / "lib/libpython3.13.so.1.0" not in p.plan_prune(home)


@pytest.mark.parametrize(
    ("found", "home_in_venv", "message"),
    [
        ("/root/.cache/uv/python/bin/python3.13", None, "outside"),
        (None, "/elsewhere/bin", "the venv runs /elsewhere/bin"),
        (None, "", "names no home"),
    ],
)
def test_provision_refuses_an_interpreter_it_did_not_place(
    tmp_path: Path, found: str | None, home_in_venv: str | None, message: str
) -> None:
    uv = FakeUv(tmp_path, home_in_venv)
    uv.found = found
    with pytest.raises(p.ProvisionError, match=message):
        p.provision("uv", "w.whl", tmp_path, environ={}, owner=None, runner=uv)


def elf(phnum: int, *segments: tuple[int, int, int, int]) -> bytes:
    """An ELF64 header and program headers (type, offset, vaddr, size)."""
    head = b"\x7fELF\x02\x01".ljust(0x20, b"\0") + struct.pack("<Q", 64)
    head = head.ljust(0x36, b"\0") + struct.pack("<HH", 56, phnum)
    head = head.ljust(64, b"\0")
    for kind, offset, vaddr, size in segments:
        head += struct.pack("<IIQQQQQQ", kind, 0, offset, vaddr, 0, size, size, 0)
    return head


def test_elf_needed(tmp_path: Path) -> None:
    needed = p.elf_needed(TRUE)
    assert needed is not None and any(n.startswith("libc.so") for n in needed)
    cases = {
        "text": b"#!/bin/sh\n",
        "static": elf(0),
        "truncated": elf(3),
        "no-strtab": elf(1, (2, 64 + 56, 0, 16)) + struct.pack("<qQ", 0, 0),
    }
    for name, data in cases.items():
        (tmp_path / name).write_bytes(data)
    assert p.elf_needed(tmp_path / "text") is None
    assert p.elf_needed(tmp_path / "static") == []
    assert p.elf_needed(tmp_path / "truncated") is None
    assert p.elf_needed(tmp_path / "no-strtab") is None


def test_run_and_size(tmp_path: Path) -> None:
    assert p.run(["pwd"], {"PATH": os.environ["PATH"]}) == "/\n"
    (tmp_path / "a").write_bytes(b"12345")
    (tmp_path / "l").symlink_to("a")
    assert p.size(tmp_path) == 5 + len("a")


def test_main(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    seen: list[tuple[str, str, Path]] = []

    def fake(uv: str, package: str, root: Path, owner: p.Owner = None) -> Path:
        seen.append((uv, package, root))
        return root / "venv/bin/python"

    monkeypatch.setattr(p, "provision", fake)
    p.main(["w.whl", "--uv", "/opt/uv", "--root", "/nonexistent"])
    assert seen == [("/opt/uv", "w.whl", Path("/nonexistent"))]
    assert "MB interpreter" in capsys.readouterr().out
    monkeypatch.delenv("UV", raising=False)

    def which(name: str) -> None:
        return None

    monkeypatch.setattr(shutil, "which", which)
    with pytest.raises(SystemExit):
        p.main(["w.whl"])
