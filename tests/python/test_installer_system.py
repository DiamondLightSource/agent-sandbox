"""The installer's system steps (apt, the probes, the agent downloads),
against stand-ins for the tools: they need root, the network or a kernel
the test host may not have, so the bash comparison cannot reach them."""

import hashlib
import io
import os
import subprocess
import tarfile
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from claude_sandbox import config
from claude_sandbox.installer import __main__ as cli
from claude_sandbox.installer import install, system
from claude_sandbox.installer.steps import InstallError, Layout, Options

REPO = Path(__file__).resolve().parents[2]
SHIM = REPO / ".devcontainer/claude-sandbox/claude-shim"
Handler = Callable[[list[str], dict[str, Any]], "tuple[int, bytes] | int"]


class FakeRun:
    """Records each argv; ``handlers`` answer by tool name or URL."""

    def __init__(self, **handlers: Handler) -> None:
        self.calls: list[list[str]] = []
        self.handlers = handlers

    def __call__(
        self, argv: list[str], **kw: Any
    ) -> "subprocess.CompletedProcess[bytes]":
        self.calls.append(list(argv))
        key = argv[-1] if os.path.basename(argv[0]) == "curl" else argv[0]
        handler = self.handlers.get(os.path.basename(key)) or self.handlers.get(key)
        result = handler(argv, kw) if handler else 0
        code, out = result if isinstance(result, tuple) else (result, b"")
        return subprocess.CompletedProcess(argv, code, out, b"")


def reply(code: int = 0, out: bytes = b"") -> Handler:
    def handler(argv: list[str], kw: dict[str, Any]) -> tuple[int, bytes]:
        return code, out

    return handler


def present(name: str) -> str:
    return f"/usr/bin/{name}"


@pytest.fixture(autouse=True)
def tools(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(system, "find_tool", present)


def setup(tmp_path: Path) -> tuple[Layout, Options]:
    layout = Layout(
        source=REPO,
        user_home=tmp_path / "user",
        home=str(tmp_path / "home"),
        prefix=tmp_path / "prefix",
        shared=str(tmp_path / "shared"),
    )
    return layout, Options("1.0")


def executable(path: Path, text: str = "#!/bin/sh\n") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(0o755)


def test_platform_checks_and_apt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, options = setup(tmp_path)
    run = FakeRun(bwrap=reply(1))
    system.apt_install(options, run)
    assert [c[1] for c in run.calls] == ["update", "install", "install"]
    assert "bubblewrap" in run.calls[1] and run.calls[2][-1] == "glab"
    with pytest.raises(InstallError, match="user namespaces"):
        system.probe_userns_or_refuse(options, run)
    system.probe_userns_or_refuse(options, FakeRun())

    def missing(name: str) -> None:
        return None

    monkeypatch.setattr(system, "find_tool", missing)
    with pytest.raises(InstallError, match="Debian/Ubuntu only"):
        system.probe_or_refuse(options)
    with pytest.raises(InstallError, match="user namespaces"):  # no bwrap
        system.probe_userns_or_refuse(options, run)
    with pytest.raises(InstallError, match="apt-get is not installed"):
        system.apt_install(options, run)
    monkeypatch.setattr(system, "find_tool", present)
    with pytest.raises(InstallError, match="apt-get update failed .exit 100"):
        system.apt_install(options, FakeRun(**{"/usr/bin/apt-get": reply(100)}))
    smoke = replace(options, smoke=True)

    system.probe_or_refuse(smoke)
    system.apt_install(smoke, run)
    system.probe_userns_or_refuse(smoke, run)


def test_minimal_apt_leaves_out_nodejs_and_what_is_there(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#101: a minimal install asks dpkg first, and runs no apt at all, not
    even its update, when every package it needs and glab are installed.
    glab is not cut: a base without it gets the same try as a full install."""
    _, options = setup(tmp_path)
    minimal = replace(options, minimal=True)
    wanted = system.MINIMAL_PACKAGES
    assert "nodejs" not in wanted and set(wanted) == set(system.APT_PACKAGES) - {
        "nodejs"
    }
    ok = b"install ok installed\n"
    run = FakeRun(**{"dpkg-query": reply(0, ok * (len(wanted) + 1))})
    system.apt_install(minimal, run)
    query = ["/usr/bin/dpkg-query", "-W", "-f=${Status}\\n", *wanted, "glab"]
    assert run.calls == [query]
    # glab alone missing (dpkg-query fails, naming the rest installed), one
    # missing, one removed, or no dpkg-query: apt runs, glab's try included.
    for answer in (
        reply(1, ok * len(wanted)),
        reply(1, ok),
        reply(0, ok * (len(wanted) - 1) + b"deinstall ok config-files\n"),
        reply(0, ok),
    ):
        run = FakeRun(**{"dpkg-query": answer})
        system.apt_install(minimal, run)
        assert [c[1] for c in run.calls[1:]] == ["update", "install", "install"]
        assert run.calls[2][5:] == wanted and run.calls[3][-1] == "glab"

    def no_dpkg(name: str) -> str | None:
        return None if name == "dpkg-query" else present(name)

    monkeypatch.setattr(system, "find_tool", no_dpkg)
    run = FakeRun()
    system.apt_install(minimal, run)
    assert [c[1] for c in run.calls] == ["update", "install", "install"]
    # A full install keeps nodejs, and always runs apt.
    run = FakeRun(**{"dpkg-query": reply(0, ok * 20)})
    system.apt_install(options, run)
    assert run.calls[1][5:] == system.APT_PACKAGES


def test_claude_binary_is_moved_off_path(tmp_path: Path) -> None:
    layout, options = setup(tmp_path)
    unwrapped = Path(layout.home, ".local/bin/claude")

    def vendor(argv: list[str], kw: dict[str, Any]) -> int:
        assert kw["input"] == b"script"
        executable(unwrapped)
        return 0

    run = FakeRun(**{"install.sh": reply(0, b"script"), "bash": vendor})
    system.install_claude_binary(layout, options, run)
    real = layout.system(system.CLAUDE_REAL)
    assert real.is_file() and not unwrapped.exists()
    executable(unwrapped)  # an older install's copy goes on a re-run
    system.install_claude_binary(layout, options, FakeRun())
    assert not unwrapped.exists()
    real.unlink()
    with pytest.raises(InstallError, match="did not produce"):
        system.install_claude_binary(layout, options, FakeRun())
    with pytest.raises(InstallError, match="could not fetch"):
        system.install_claude_binary(
            layout, options, FakeRun(**{"install.sh": reply(1)})
        )


def codex_vendor(
    layout: Layout, binary: str = "#!/bin/sh\n", mode: int = 0o755
) -> Handler:
    """The vendor script: a release under CODEX_HOME, linked from PATH."""

    def sh(argv: list[str], kw: dict[str, Any]) -> int:
        release = Path(kw["env"]["CODEX_HOME"], "packages/standalone/releases/1")
        executable(release / "bin/codex", binary)
        (release / "bin/codex").chmod(mode)
        (release / "codex-package.json").write_text("{}")
        (release / "codex-package.json").chmod(0o666)
        link = Path(layout.home, ".local/bin/codex")
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to(release / "bin/codex")
        return 0

    return sh


def test_codex_release_is_copied_root_owned_and_purged(tmp_path: Path) -> None:
    layout, options = setup(tmp_path)
    err = io.StringIO()
    run = FakeRun(**{"install.sh": reply(0, b"s"), "sh": codex_vendor(layout)})
    system.install_codex_binary(layout, options, err, run)
    dist = layout.system(system.CODEX_DIST)
    assert (dist / "bin/codex").is_file() and err.getvalue() == ""
    assert (dist / "codex-package.json").stat().st_mode & 0o777 == 0o644
    assert not os.path.lexists(Path(layout.home, ".local/bin/codex"))
    system.install_codex_binary(layout, options, err, FakeRun())  # already there
    assert run.calls[-1][0] == "/usr/bin/sh"


@pytest.mark.parametrize(
    ("case", "warning"),
    [
        ("fetch-fails", "installer failed"),
        ("shadow", "shadow itself"),
        ("nothing", "no release"),
        ("not-executable", "is not\n  executable"),
    ],
)
def test_codex_failures_warn_and_purge(tmp_path: Path, case: str, warning: str) -> None:
    layout, options = setup(tmp_path)
    handlers: dict[str, Handler] = {
        "install.sh": reply(1) if case == "fetch-fails" else reply(0, b"s"),
        "sh": {
            "shadow": codex_vendor(layout, SHIM.read_text()),
            "nothing": reply(0),
            "not-executable": codex_vendor(layout, mode=0o644),
        }.get(case, codex_vendor(layout)),
    }
    err = io.StringIO()
    system.install_codex_binary(layout, options, err, FakeRun(**handlers))
    assert warning in err.getvalue()
    assert not os.path.lexists(Path(layout.home, ".local/bin/codex"))
    copied = layout.system(system.CODEX_DIST).exists()
    assert copied == (case == "not-executable")
    system.install_codex_binary(layout, replace(options, with_codex=False), err)


def pi_release(tmp_path: Path, checksum: str | None = None) -> dict[str, Handler]:
    """A Pi release: the latest redirect, the archive and its SHA256SUMS."""
    archive = tmp_path / "pi.tar.gz"
    executable(tmp_path / "build/pi/pi")
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(tmp_path / "build/pi", arcname="pi")
    data = archive.read_bytes()
    digest = checksum or hashlib.sha256(data).hexdigest()

    def untar(argv: list[str], kw: dict[str, Any]) -> int:
        with tarfile.open(argv[2]) as tar:
            tar.extractall(argv[-1], filter="tar")
        return 0

    return {
        "latest": reply(0, f"{system.PI_RELEASES}/tag/v1.2.3".encode()),
        "pi-linux-x64.tar.gz": reply(0, data),
        "SHA256SUMS": reply(0, f"{digest}  pi-linux-x64.tar.gz\n".encode()),
        "/usr/bin/tar": untar,
    }


def test_pi_release_is_checked_and_placed(tmp_path: Path) -> None:
    layout, options = setup(tmp_path)
    err = io.StringIO()
    system.install_pi_binary(
        layout, options, err, FakeRun(**pi_release(tmp_path)), "x86_64"
    )
    dest = layout.system(system.PI_DIST)
    assert (dest / "pi").is_file() and (
        dest / ".sandbox-version"
    ).read_text() == "1.2.3\n"
    run = FakeRun()
    system.install_pi_binary(layout, options, err, run, "x86_64")
    pinned = replace(options, pi_version="1.2.3")
    system.install_pi_binary(layout, pinned, err, run, "x86_64")
    assert run.calls == [] and err.getvalue() == ""


@pytest.mark.parametrize(
    ("machine", "version", "broken", "warning"),
    [
        ("riscv64", "latest", "", "supports Linux x64/arm64"),
        ("x86_64", "latest", "latest", "could not resolve"),
        ("x86_64", "not-a-version", "", "invalid Pi release version"),
        ("x86_64", "1.2.4", "SHA256SUMS", "download failed"),
        ("x86_64", "1.2.4", "checksum", "validation failed"),
    ],
)
def test_pi_failures_keep_what_is_installed(
    tmp_path: Path, machine: str, version: str, broken: str, warning: str
) -> None:
    layout, options = setup(tmp_path)
    handlers = pi_release(tmp_path, "0" * 64 if broken == "checksum" else None)
    if broken in handlers:
        handlers[broken] = reply(22)
    err = io.StringIO()
    options = replace(options, pi_version=version)
    system.install_pi_binary(layout, options, err, FakeRun(**handlers), machine)
    assert warning in err.getvalue()
    assert not layout.system(system.PI_DIST).exists()
    system.install_pi_binary(layout, replace(options, with_pi=False), err)


def test_pi_upgrade_replaces_only_a_good_release(tmp_path: Path) -> None:
    """1.2.3 installed; a pinned 1.2.4 whose archive fails its checksum
    leaves 1.2.3 in place and runnable; a good 1.2.4 replaces it."""
    layout, options = setup(tmp_path)
    err = io.StringIO()
    system.install_pi_binary(
        layout, options, err, FakeRun(**pi_release(tmp_path / "a")), "x86_64"
    )
    dest = layout.system(system.PI_DIST)
    pinned = replace(options, pi_version="1.2.4")
    bad = pi_release(tmp_path / "b", "0" * 64)
    system.install_pi_binary(layout, pinned, err, FakeRun(**bad), "x86_64")
    assert "validation failed" in err.getvalue()
    assert (dest / ".sandbox-version").read_text() == "1.2.3\n"
    assert os.access(dest / "pi", os.X_OK)
    good = pi_release(tmp_path / "c")
    system.install_pi_binary(layout, pinned, err, FakeRun(**good), "x86_64")
    assert (dest / ".sandbox-version").read_text() == "1.2.4\n"
    assert os.access(dest / "pi", os.X_OK)


def test_summary_does_not_claim_a_skipped_step(tmp_path: Path) -> None:
    layout, options = setup(tmp_path)
    managed = layout.system("/etc/claude-code/managed-settings.json")
    managed.parent.mkdir(parents=True)
    managed.write_text("[1]")
    out, err = io.StringIO(), io.StringIO()
    install(layout, replace(options, smoke=True), err, out)
    assert "the updater is NOT disabled" in out.getvalue()
    assert "python:      interpreter in " in out.getvalue()
    assert layout.system("/usr/local/bin/claude").read_bytes() == SHIM.read_bytes()


def test_module_entry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for key, value in {
        "CLAUDE_SANDBOX_SMOKE": "1",
        "CLAUDE_SANDBOX_VERSION": "1.0",
        "INSTALL_PREFIX": str(tmp_path / "prefix"),
        "INSTALL_USER_HOME": str(tmp_path / "user"),
        "HOME": str(tmp_path / "home"),
        "CLAUDE_SHARED_CONFIG": str(tmp_path / "none"),
    }.items():
        monkeypatch.setenv(key, value)
    assert cli.main(["--source", str(REPO), "--image-build"]) == 0
    # The image entrypoint's share: links, credential directories, conf.
    (tmp_path / "shared").mkdir()
    (tmp_path / "home").mkdir()
    monkeypatch.setenv("CLAUDE_SHARED_CONFIG", str(tmp_path / "shared"))
    (tmp_path / "prefix/etc/claude-sandbox.conf").unlink()
    assert cli.main(["--source", str(REPO), "--container-start"]) == 0
    assert (tmp_path / "home/.claude").is_symlink()
    assert (tmp_path / "prefix/etc/claude-sandbox.conf").is_file()
    assert cli.main(["--source", str(REPO), "--probe-userns"]) == 0
    # A step that cannot go ahead exits with its code.
    assert cli.main(["--source", str(tmp_path / "nowhere")]) == 1


# --- issue #71: the egress jail's device, reported at install -----------------


def test_a_missing_tun_is_reported_when_the_jail_is_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conf = tmp_path / "conf"
    tun = str(tmp_path / "tun")
    assert config.tun_missing(str(conf), {}, tun)
    assert not config.tun_missing(str(conf), {"CLAUDE_SANDBOX_EGRESS_JAIL": "0"}, tun)
    conf.write_text("egress-jail = 0\n")
    assert not config.tun_missing(str(conf), {}, tun)
    # The environment wins over the conf, as at launch.
    assert config.tun_missing(str(conf), {"CLAUDE_SANDBOX_EGRESS_JAIL": "1"}, tun)
    assert not config.tun_missing(str(conf), {}, str(conf))  # the device is there

    def unreadable(path: str, env: object) -> dict[str, str]:
        raise PermissionError(path)

    monkeypatch.setattr(config, "parse_config", unreadable)
    assert config.tun_missing(str(conf), {}, tun)  # the default: on


def test_the_install_and_container_start_warn_but_not_the_image_build(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from claude_sandbox.installer import container_start

    layout, options = setup(tmp_path)
    monkeypatch.setattr(config, "TUN", str(tmp_path / "tun"))
    monkeypatch.setattr(config, "passt_version", lambda: "")  # not this host's
    monkeypatch.delenv("CLAUDE_SANDBOX_EGRESS_JAIL", raising=False)
    assert '"runArgs": ["--device=/dev/net/tun"]' in system.TUN_WARNING

    def warned(options: Options) -> str:
        err = io.StringIO()
        system.warn_if_no_tun(layout, options, err)
        return err.getvalue()

    assert warned(replace(options, image_build=True)) == ""
    assert warned(replace(options, smoke=True)) == ""
    # A whole install, outside smoke mode, warns last.
    executable(layout.system(system.CLAUDE_REAL))
    err = io.StringIO()
    plain = replace(options, with_codex=False, with_pi=False)
    install(layout, plain, err, io.StringIO(), FakeRun())
    assert err.getvalue().endswith(system.TUN_WARNING + "\n")
    err = io.StringIO()
    container_start(layout, options, err)
    assert err.getvalue().endswith(system.TUN_WARNING + "\n")
    (tmp_path / "tun").touch()
    assert warned(options) == ""


# --- issue #85: a passt too old to attach, reported at install ---------------

BOOKWORM = "0.0~git20230309.7c7625d-1"


def test_an_old_passt_is_too_old_with_the_jail_on(tmp_path: Path) -> None:
    conf = str(tmp_path / "conf")
    assert config.passt_date("1:0.0~git20240220.1e6f92b-1") == "20240220"
    assert config.passt_too_old(conf, {}, BOOKWORM)
    assert not config.passt_too_old(conf, {}, "0.0~git20230908.05627dc-1")
    assert not config.passt_too_old(conf, {}, "unknown")  # no false alarm
    assert not config.passt_too_old(conf, {}, "2025_09_01.abcdef0-1")
    off = {"CLAUDE_SANDBOX_EGRESS_JAIL": "0"}
    assert not config.passt_too_old(conf, off, BOOKWORM)


def test_the_passt_version_comes_from_dpkg(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    replies = [(0, f"installed {BOOKWORM}"), (0, f"config-files {BOOKWORM}"), (1, "")]

    def output(argv: list[str]) -> tuple[int, str]:
        calls.append(argv)
        return replies[len(calls) - 1]

    monkeypatch.setattr(config, "output", output)
    found = {"dpkg-query": "/usr/bin/dpkg-query"}
    monkeypatch.setattr(config, "find_tool", found.get)
    assert config.passt_version() == BOOKWORM
    assert calls[0][0] == "/usr/bin/dpkg-query" and calls[0][-1] == "passt"
    assert config.passt_version() == ""  # removed, not purged
    assert config.passt_version() == ""  # not installed
    found.clear()
    assert config.passt_version() == ""


def test_the_install_warns_of_an_old_passt_but_not_the_image_build(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    layout, options = setup(tmp_path)
    monkeypatch.setattr(config, "passt_version", lambda: BOOKWORM)
    monkeypatch.delenv("CLAUDE_SANDBOX_EGRESS_JAIL", raising=False)

    def warned(options: Options) -> str:
        err = io.StringIO()
        system.warn_if_old_passt(layout, options, err)
        return err.getvalue()

    assert warned(replace(options, image_build=True)) != ""  # its author can fix it
    assert warned(replace(options, smoke=True)) == ""
    assert warned(options) == system.PASST_WARNING.format(version=BOOKWORM) + "\n"
    assert f"jail: {BOOKWORM}, older than" in warned(options)
    monkeypatch.setattr(config, "passt_version", lambda: "0.0~git20240220.1e6f92b-1")
    assert warned(options) == ""
