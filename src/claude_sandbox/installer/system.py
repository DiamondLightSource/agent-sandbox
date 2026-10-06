"""The installer's steps that touch the system rather than files it owns:
the platform checks, apt, and the three agent downloads, ported from
``install.sh``'s functions of the same names.

Each runs its tools by absolute path (``tools.find_tool``, ADR 26) through
an injectable ``Run``, so the tests can stand in for apt, curl and the
vendors' install scripts. ``CLAUDE_SANDBOX_SMOKE=1`` skips them all, as it
does in the bash.
"""

import filecmp
import hashlib
import os
import platform
import re
import shutil
import subprocess
import tempfile
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import TextIO

from ..tools import find_tool
from .steps import LIBEXEC, InstallError, Layout, Options

Run = Callable[..., "subprocess.CompletedProcess[bytes]"]

CODEX_DIST = f"{LIBEXEC}/codex-dist"
CODEX_REAL = f"{CODEX_DIST}/bin/codex"
CLAUDE_REAL = f"{LIBEXEC}/claude"
PI_DIST = f"{LIBEXEC}/pi-dist"
APT_PACKAGES = (
    "bubblewrap jq curl ca-certificates git nodejs gh passt socat iproute2"
    " ripgrep fd-find"
).split()
PI_RELEASES = "https://github.com/earendil-works/pi/releases"
USERNS_REFUSAL = """\
claude-sandbox: refusing — kernel unprivileged user namespaces are
forbidden. The bwrap sandbox cannot start without them.

On Ubuntu 24.04:
    sudo sysctl -w kernel.apparmor_restrict_unprivileged_userns=0
On rootful Docker with default AppArmor: rebuild the devcontainer
under rootless podman, or relax AppArmor for bwrap."""


def _tool(name: str) -> str:
    path = find_tool(name)
    if path is None:
        raise InstallError(f"claude-sandbox: {name} is not installed.")
    return path


def _executable(path: str | Path) -> bool:
    return os.path.isfile(path) and os.access(path, os.X_OK)


def _check(run: Run, argv: list[str], what: str, **kw: object) -> None:
    """Run ``argv``; a failure stops the install with one line, not a
    traceback."""
    code = run(argv, check=False, **kw).returncode
    if code:
        raise InstallError(f"claude-sandbox: {what} failed (exit {code}).")


def probe_or_refuse(options: Options) -> None:
    """``probe_or_refuse``: Debian and Ubuntu only."""
    if not options.smoke and find_tool("apt-get") is None:
        raise InstallError(
            "claude-sandbox: refusing — Debian/Ubuntu only (no apt-get installed)."
        )


def apt_install(options: Options, run: Run = subprocess.run) -> None:
    """``apt_install``: the sandbox's own dependencies; glab where the
    distribution has it."""
    if options.smoke:
        return
    apt = _tool("apt-get")
    env = {**os.environ, "DEBIAN_FRONTEND": "noninteractive"}
    _check(run, [apt, "update", "-qq"], "apt-get update", env=env)
    quiet = [apt, "install", "-y", "-qq", "--no-install-recommends"]
    _check(run, [*quiet, *APT_PACKAGES], "apt-get install", env=env)
    run([*quiet, "glab"], env=env, check=False, stderr=subprocess.DEVNULL)


def probe_userns_or_refuse(options: Options, run: Run = subprocess.run) -> None:
    """``probe_userns_or_refuse``: bwrap must be able to start a sandbox."""
    if options.smoke:
        return
    bwrap = find_tool("bwrap")
    if bwrap is None:
        raise InstallError(USERNS_REFUSAL)
    argv = [bwrap, "--ro-bind", "/", "/", "--unshare-user-try", "--unshare-pid"]
    quiet = {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    if run([*argv, "--", "/bin/true"], check=False, **quiet).returncode:
        raise InstallError(USERNS_REFUSAL)


def _fetch(url: str, run: Run, *extra: str) -> bytes | None:
    """``curl -fsSL URL``'s output, or None when it fails."""
    curl = find_tool("curl")
    if curl is None:
        return None
    done = run([curl, "-fsSL", *extra, url], capture_output=True, check=False)
    return done.stdout if done.returncode == 0 else None


def install_claude_binary(
    layout: Layout, options: Options, run: Run = subprocess.run
) -> None:
    """``install_claude_binary``: the official installer, then the binary
    moved off the user's PATH to /usr/libexec, where only the shadow runs it."""
    if options.smoke:
        return
    real = layout.system(CLAUDE_REAL)
    unwrapped = Path(layout.home, ".local/bin/claude")
    if _executable(real):
        unwrapped.unlink(missing_ok=True)
        return
    script = _fetch("https://claude.ai/install.sh", run)
    if script is None:
        raise InstallError("claude-sandbox: could not fetch the Claude installer.")
    _check(run, [_tool("bash")], "the Claude installer", input=script)
    if not _executable(unwrapped):
        raise InstallError(
            "claude-sandbox: official installer did not produce $HOME/.local/bin/claude"
        )
    real.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(unwrapped, real)


def _codex_purge(layout: Layout, stage: str | None = None) -> None:
    """``codex_purge_vendor_tree``: no writable copy of the package and no
    unwrapped ``codex`` may remain."""
    if stage is not None:
        shutil.rmtree(stage, ignore_errors=True)
    shutil.rmtree(Path(layout.home, ".codex/packages"), ignore_errors=True)
    for name in ("codex", "codex-code-mode-host"):
        Path(layout.home, ".local/bin", name).unlink(missing_ok=True)


def _is_shadow(layout: Layout, path: str) -> bool:
    scripts = layout.source / ".devcontainer/claude-sandbox"
    return any(
        (scripts / name).is_file() and filecmp.cmp(path, scripts / name, shallow=False)
        for name in ("claude-shadow", "claude-shim")
    )


def _codex_release(layout: Layout, stage: str) -> tuple[str, list[str]]:
    """The vendor's release directory, or "" with the warning why not."""
    link = f"{layout.home}/.local/bin/codex"
    release = ""
    if os.path.islink(link) or _executable(link):
        resolved = os.path.realpath(link)
        if os.path.isfile(resolved):
            if _is_shadow(layout, resolved):
                return "", [
                    f"claude-sandbox: WARNING — {resolved} is the claude-sandbox",
                    "  shadow itself, not a real codex binary; skipping codex"
                    " relocation.",
                ]
            if resolved.endswith("/bin/codex"):
                release = resolved.removesuffix("/bin/codex")
            elif resolved.endswith("/codex"):
                release = resolved.removesuffix("/codex")
    for current in (
        f"{stage}/packages/standalone/current",
        f"{layout.home}/.codex/packages/standalone/current",
    ):
        if not release and os.path.isdir(current):
            release = os.path.realpath(current)
    if not release or not os.path.isdir(release):
        return "", [
            "claude-sandbox: WARNING — the Codex CLI installer ran but no release",
            "  directory was found; skipping codex relocation.",
        ]
    binary = f"{release}/bin/codex"
    if os.path.isfile(binary) and _is_shadow(layout, binary):
        return "", [
            f"claude-sandbox: WARNING — {binary} is the claude-sandbox",
            "  shadow itself, not a real codex binary; skipping codex relocation.",
        ]
    return release, []


def install_codex_binary(
    layout: Layout, options: Options, err: TextIO, run: Run = subprocess.run
) -> None:
    """``install_codex_binary``: best effort. The vendor's installer unpacks
    into a temporary CODEX_HOME; the whole release directory is copied to
    root-owned /usr/libexec, and every writable copy is purged."""
    if options.smoke or not options.with_codex:
        return
    dist, real = layout.system(CODEX_DIST), layout.system(CODEX_REAL)
    if _executable(real):
        _codex_purge(layout)
        return
    stage = tempfile.mkdtemp()
    script = _fetch("https://chatgpt.com/codex/install.sh", run)
    env = {
        **os.environ,
        "CODEX_HOME": stage,
        "CODEX_NON_INTERACTIVE": "1",
        "TAR_OPTIONS": "--no-same-owner",
    }
    if script is None or run([_tool("sh")], input=script, env=env).returncode:
        print(
            "claude-sandbox: WARNING — the Codex CLI installer failed; skipping"
            " codex.\n  The codex shadow is still installed and will refuse to"
            f" launch until\n  a real binary lands at {real}. Re-run ./install to"
            " retry.",
            file=err,
        )
        _codex_purge(layout, stage)
        return
    release, warning = _codex_release(layout, stage)
    if warning:
        print("\n".join(warning), file=err)
        _codex_purge(layout, stage)
        return
    shutil.rmtree(dist, ignore_errors=True)
    dist.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(release, dist, symlinks=True)
    for dirpath, dirnames, filenames in os.walk(dist):
        for name in [".", *dirnames, *filenames]:
            node = os.path.join(dirpath, name)
            if not os.path.islink(node):
                os.chmod(node, os.stat(node).st_mode & ~0o022 & 0o7777)
    if not _executable(real):
        print(
            f"claude-sandbox: WARNING — copied {release} but {real} is not\n"
            "  executable; codex will refuse to launch.",
            file=err,
        )
    _codex_purge(layout, stage)


def _pi_checksum(sums: str, asset: str) -> str | None:
    """The SHA256SUMS line for ``asset`` (``sha256sum``'s text or binary
    form), when there is exactly one."""
    found = [
        fields[0]
        for fields in (line.split() for line in sums.splitlines())
        if len(fields) >= 2 and fields[1] in (asset, f"*{asset}")
    ]
    ok = len(found) == 1 and re.fullmatch(r"[a-fA-F0-9]{64}", found[0])
    return found[0].lower() if ok else None


def install_pi_binary(
    layout: Layout,
    options: Options,
    err: TextIO,
    run: Run = subprocess.run,
    machine: str = platform.machine(),
) -> None:
    """``install_pi_binary``: the standalone release, checked against its
    SHA256SUMS; an existing installation is kept on any failure."""
    if options.smoke or not options.with_pi:
        return
    arch = {"x86_64": "x64", "aarch64": "arm64", "arm64": "arm64"}.get(machine)
    if arch is None:
        print(
            "claude-sandbox: WARNING — Pi standalone supports Linux x64/arm64;"
            " skipping.",
            file=err,
        )
        return
    dest = layout.system(PI_DIST)
    if _executable(dest / "pi"):
        if options.pi_version == "latest":
            return
        stamp = dest / ".sandbox-version"
        if stamp.is_file() and stamp.read_text().strip() == options.pi_version:
            return
    version = options.pi_version
    if version == "latest":
        url = _fetch(
            f"{PI_RELEASES}/latest",
            run,
            "--retry",
            "6",
            "-I",
            "-o",
            "/dev/null",
            "-w",
            "%{url_effective}",
        )
        if url is None:
            print(
                "claude-sandbox: WARNING — could not resolve the latest Pi"
                " release; existing installation preserved.",
                file=err,
            )
            return
        version = url.decode().removeprefix(f"{PI_RELEASES}/tag/v")
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+([.-][A-Za-z0-9.-]+)?", version):
        print(
            "claude-sandbox: WARNING — invalid Pi release version; existing"
            " installation preserved.",
            file=err,
        )
        return
    asset = f"pi-linux-{arch}.tar.gz"
    base = f"{PI_RELEASES}/download/v{version}"
    with tempfile.TemporaryDirectory() as stage:
        archive = _fetch(f"{base}/{asset}", run, "--retry", "6")
        sums = _fetch(f"{base}/SHA256SUMS", run, "--retry", "6")
        if archive is None or sums is None:
            print(
                "claude-sandbox: WARNING — Pi download failed; its shadow remains"
                " installed.",
                file=err,
            )
            return
        Path(stage, asset).write_bytes(archive)
        checksum = _pi_checksum(sums.decode(errors="replace"), asset)
        tar = [_tool("tar"), "-xzf", f"{stage}/{asset}", "--no-same-owner"]
        if (
            checksum != hashlib.sha256(archive).hexdigest()
            or run([*tar, "-C", stage], check=False).returncode
            or not _executable(f"{stage}/pi/pi")
        ):
            print(
                "claude-sandbox: WARNING — Pi release validation failed; existing"
                " installation preserved.",
                file=err,
            )
            return
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.rmtree(dest, ignore_errors=True)
        shutil.move(f"{stage}/pi", dest)
        (dest / ".sandbox-version").write_text(f"{version}\n")


def summary(layout: Layout, options: Options, skipped: Sequence[str], venv: str) -> str:
    """main()'s closing report. A step that warned and skipped is reported
    as not done, never as done."""
    p = layout.system

    def state(path: str, yes: str, no: str) -> str:
        return yes if os.access(p(path), os.X_OK) else no

    skills = p(f"{LIBEXEC}/skills")
    listed = os.listdir(skills) if skills.is_dir() else []
    shipped = len([name for name in listed if not name.startswith(".")])
    version = p(f"{LIBEXEC}/version")
    stamped = version.read_text().rstrip("\n") if version.is_file() else ""
    managed = "/etc/claude-code/managed-settings.json"
    codex_conf = "/etc/codex/managed_config.toml"
    lines = [
        "claude-sandbox: install complete.",
        "  shadow:      "
        + ", ".join(str(p(f"/usr/local/bin/{a}")) for a in ("claude", "codex", "pi")),
    ]
    if options.impl == "python":
        lines.append(
            f"  python:      the Python shadow (opt-in), interpreter in {p(venv)}"
        )
    lines += [
        f"  real pi:     {p(PI_DIST)}/pi "
        + state(
            f"{PI_DIST}/pi",
            "installed (standalone, ro in sandbox)",
            "NOT installed — pi will refuse to launch",
        ),
        f"  cli:         {p('/usr/local/bin/claude-sandbox')} ({stamped})",
        f"  real claude: {p(CLAUDE_REAL)}",
        f"  real codex:  {p(CODEX_REAL)} "
        + state(
            CODEX_REAL,
            "installed (whole package, ro in sandbox)",
            "NOT installed — `codex` will refuse to launch",
        ),
        f"  config:      {p('/etc/claude-sandbox.conf')}",
        f"  battery:     {p(f'{LIBEXEC}/verify-sandbox-battery.sh')} (off-PATH, ro"
        " in sandbox; /verify-sandbox phase 1)",
        f"  skills:      {skills} ({shipped} shipped; ro-bound into each agent's"
        " skills dir in-session)",
        f"  managed:     {p(managed)} "
        + (
            "(NOT changed — see the warning above; the updater is NOT disabled)"
            if "wire_managed_settings" in skipped
            else "(updater disabled)"
        ),
        f"  codex conf:  {p(codex_conf)} "
        + (
            "(NOT ours — left unchanged; see the warning above)"
            if "wire_codex_managed" in skipped
            else "(updater settings)"
        ),
        f"  statusline:  {layout.user_home}/.claude/settings.json (preference only)",
        "  run `claude-sandbox verify` for the live battery, or use the shipped"
        " verify-sandbox skill for the full audit.",
    ]
    return "\n".join(lines) + "\n"
