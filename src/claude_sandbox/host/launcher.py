"""One named container per project; every session is an exec into it.

A port of ``container/claude-container``: the same engine calls, the same
create argv, the same messages. Read that script's header for the model
(the keeper, container-scoped forge logins, create-time options, the
filesystem view); the comments here say only where Python differs.

All engine calls go through :func:`run` and :func:`interactive`, the
seams the tests replace.
"""

import os
import re
import shutil
import subprocess
import sys
import termios
from collections.abc import Mapping

from .. import release_tag
from .options import Options

IMAGE = "ghcr.io/diamondlightsource/claude-sandbox:latest"
VERSION_LABEL = "io.diamondlightsource.claude-sandbox.launcher-version"
REVISION_LABEL = "org.opencontainers.image.revision"
RAW_URL = "https://raw.githubusercontent.com/DiamondLightSource/claude-sandbox"
# Idle PID 1, also used to identify containers owned by this launcher.
KEEPER_CMD = 'trap "exit 0" TERM INT; while :; do sleep 60 & wait $!; done'
KEEPER_MARK = "sleep 60 & wait"
# Turn off the DEC mouse modes a TUI killed mid-draw leaves on.
MOUSE_RESET = "\033[?1000l\033[?1002l\033[?1003l\033[?1005l\033[?1006l\033[?1015l"
# Programs run inside the container are named by absolute path (ADR 26: no
# executable found through PATH): the sandbox's own commands, and the shell.
IN_CONTAINER = "/usr/local/bin"
SH = "/bin/sh"
# The `shell` verb: the user's shell, looked up on a fixed PATH, if the image
# has it; else bash.
SHELL_SCRIPT = (
    'if p="$(PATH=/usr/local/bin:/usr/bin:/bin command -v -- "$1")"; then'
    ' shift; exec "$p" "$@"; fi; shift; exec /bin/bash "$@"'
)
SHELLS = frozenset({"zsh", "bash", "fish", "ksh", "tcsh", "dash", "sh"})
# CLAUDE_SANDBOX_* variables that are the launcher's own, not the sandbox's.
# IMPL, CONTEXT and NESTED are not passed by the Python CLI (the bash passes
# NESTED): the host's opt-in must not opt the container's own install in,
# and nothing the host sets may tell the container that it is a host.
NOT_PASSED = frozenset(
    "CLAUDE_SANDBOX_" + v
    for v in (
        "IMAGE ENGINE SHARED_CONFIG CONF ALLOW_WRITE SHELL CACHE TAG GPU"
        " ALLOW_DEVICES IMPL CONTEXT NESTED"
    ).split()
)
DIGITS = "0123456789"
_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")


def run(
    argv: list[str], *, stdout: int | None = None, stderr: int | None = None
) -> "subprocess.CompletedProcess[str]":
    return subprocess.run(argv, stdout=stdout, stderr=stderr, text=True, check=False)


def interactive(argv: list[str]) -> int:
    """Run ``argv`` on this terminal; the child, not us, answers Ctrl-C.

    As bash waits out a foreground child, so does this: an interrupt that
    reaches us while the session runs is the session's to handle.
    """
    proc = subprocess.Popen(argv)
    while True:
        try:
            rc = proc.wait()
        except KeyboardInterrupt:
            continue
        # Killed by a signal: report it as a shell does, 128 + its number.
        return rc if rc >= 0 else 128 - rc


def version(env: Mapping[str, str]) -> str:
    """The launcher version: the wheel's tag, as the uvx front door passes it."""
    return env.get("CLAUDE_SANDBOX_LAUNCHER_VERSION") or release_tag()


def say(message: str) -> None:
    """A ``claude-sandbox:`` headline on stderr."""
    print(f"claude-sandbox: {message}", file=sys.stderr)


def note(label: str, detail: str) -> None:
    """One aligned detail line under a headline."""
    print(f"  {label:<11} {detail}", file=sys.stderr)


def cksum(data: bytes) -> int:
    """POSIX ``cksum``: the CRC the bash names containers with."""
    crc = 0
    for byte in data + len(data).to_bytes((len(data).bit_length() + 7) // 8, "little"):
        crc ^= byte << 24
        for _ in range(8):
            crc = (crc << 1) ^ 0x04C11DB7 if crc & 0x80000000 else crc << 1
    return ~crc & 0xFFFFFFFF


def basename(path: str) -> str:
    """``basename(1)``, which keeps ``/`` and ignores trailing slashes."""
    stripped = path.rstrip("/")
    return stripped.rsplit("/", 1)[-1] if stripped else path[:1]


def slug(path: str) -> str:
    """``basename | tr -c 'a-zA-Z0-9_.-' '-'`` without the newline's dash."""
    return re.sub(rb"[^a-zA-Z0-9_.-]", b"-", os.fsencode(basename(path))).decode()


def names(project: str) -> tuple[str, str]:
    """The container name and the short tag for prompts, as the bash makes them."""
    s, h = slug(project), cksum(os.fsencode(project))
    return f"claude-sandbox-{s}-{h}", f"{s[:20]}-{h % 65536:04x}"


def project_dir(env: Mapping[str, str]) -> str:
    """bash's ``$PWD``: the inherited logical path when it is this directory."""
    pwd = env.get("PWD", "")
    try:
        if pwd.startswith("/") and os.path.samefile(pwd, "."):
            return pwd
    except OSError:
        pass
    return os.getcwd()


def _order(c: str) -> int:
    """verrevcmp's order: digits and the end sort lowest, ``~`` below them,
    letters by code, anything else after every letter."""
    if not c or c in DIGITS:
        return 0
    if c.isascii() and c.isalpha():
        return ord(c)
    return -1 if c == "~" else ord(c) + 256


def vercmp(a: str, b: str) -> int:
    """``sort -V``'s comparison (verrevcmp): <0, 0 or >0."""
    i = j = 0
    while i < len(a) or j < len(b):
        while a[i : i + 1] not in DIGITS or b[j : j + 1] not in DIGITS:
            diff = _order(a[i : i + 1]) - _order(b[j : j + 1])
            if diff:
                return diff
            i, j = i + 1, j + 1
        si, sj = i, j
        while i < len(a) and a[i] in DIGITS:
            i += 1
        while j < len(b) and b[j] in DIGITS:
            j += 1
        diff = int(a[si:i] or 0) - int(b[sj:j] or 0)
        if diff:
            return diff
    return 0


def detect_shell(env: Mapping[str, str], pid: int = 0, proc: str = "/proc") -> str:
    """The interactive shell this was run from, walking up through uvx.

    ``$SHELL`` is only the login shell, so it is the fallback. Reads
    ``/proc/PID/stat`` directly: ps fails under a bound /proc.
    """
    pid = pid or os.getppid()
    for _ in range(6):
        if pid <= 1:
            break
        try:
            with open(f"{proc}/{pid}/stat", encoding="utf-8", errors="replace") as f:
                stat = f.read()
        except OSError:
            break
        comm = stat[stat.find("(") + 1 : stat.rfind(")")].removeprefix("-")
        if comm in SHELLS:
            return comm
        try:
            pid = int(stat[stat.rfind(") ") + 2 :].split(" ")[1])
        except (IndexError, ValueError):
            break
    return basename(env.get("SHELL") or "bash")


def wait_for_key() -> None:
    """Wait for one key, unechoed; Ctrl-C cancels the launch."""
    print("Press any key to continue, Ctrl-C to cancel.", end="", file=sys.stderr)
    sys.stderr.flush()
    fd = sys.stdin.fileno()
    saved = termios.tcgetattr(fd)
    mode = termios.tcgetattr(fd)
    mode[3] &= ~(termios.ICANON | termios.ECHO)
    mode[6][termios.VMIN], mode[6][termios.VTIME] = 1, 0
    try:
        termios.tcsetattr(fd, termios.TCSANOW, mode)
        os.read(fd, 1)
    except KeyboardInterrupt:
        raise SystemExit(130) from None
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)
    print(file=sys.stderr)


class Launcher:
    """One run of the launcher: its settings, read once from the environment."""

    def __init__(self, opts: Options, env: Mapping[str, str]) -> None:
        self.opts, self.env = opts, env
        self.warned = False
        self.skipped_parent = ""
        self.version = version(env)
        uvx = env.get("CLAUDE_SANDBOX_LAUNCHER") == "uvx"
        self.self_name = "uvx claude-sandbox" if uvx else "claude-container"
        self.image = env.get("CLAUDE_SANDBOX_IMAGE") or IMAGE
        self.engine = env.get("CLAUDE_SANDBOX_ENGINE") or "podman"
        home = env.get("HOME") or os.path.expanduser("~")
        self.home = home
        self.shared = env.get("CLAUDE_SANDBOX_SHARED_CONFIG") or (
            f"{home}/.config/terminal-config"
        )
        self.conf = (
            env.get("CLAUDE_SANDBOX_CONF") or f"{home}/.config/claude-sandbox.conf"
        )
        # Unset means the default volume; set but empty means none.
        self.cache = env.get("CLAUDE_SANDBOX_CACHE", "claude-sandbox-cache")
        self.project = project_dir(env)
        self.name, self.tag = names(self.project)

    # --- engine calls -------------------------------------------------------

    def out(self, *args: str) -> str | None:
        """Stdout of an engine query, or None when it fails; stderr dropped."""
        done = run(
            [self.engine, *args], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
        )
        return done.stdout.rstrip("\n") if done.returncode == 0 else None

    def call(self, *args: str, quiet: bool = False) -> int:
        """An engine command with stdout dropped (and stderr, when quiet)."""
        err = subprocess.DEVNULL if quiet else None
        return run(
            [self.engine, *args], stdout=subprocess.DEVNULL, stderr=err
        ).returncode

    def must(self, *args: str) -> None:
        """An engine command that ends the run when it fails, as under set -e."""
        rc = self.call(*args)
        if rc:
            raise SystemExit(rc)

    def inspect(self, fmt: str, name: str) -> str | None:
        return self.out("container", "inspect", "-f", fmt, name)

    def exists(self, name: str) -> bool:
        return self.call("container", "inspect", name, quiet=True) == 0

    def running(self) -> bool:
        return self.inspect("{{.State.Running}}", self.name) == "true"

    def is_keeper(self, name: str) -> bool:
        return KEEPER_MARK in (self.inspect('{{join .Config.Cmd " "}}', name) or "")

    # --- checks -------------------------------------------------------------

    def warn(self, detail: str) -> None:
        note("warning", detail)
        self.warned = True

    def require_engine(self) -> None:
        if shutil.which(self.engine, path=self.env.get("PATH")) is None:
            say(f"{self.engine} not found (set CLAUDE_SANDBOX_ENGINE=docker?)")
            raise SystemExit(1)

    def warn_if_outdated(self) -> None:
        """Notify only: this runs unsandboxed on the host, so updating it
        stays a deliberate act, never something it does to itself."""
        fmt = "{{index .Config.Labels %s}}"
        img_ver = self.out(
            "image", "inspect", "-f", fmt % f'"{VERSION_LABEL}"', self.image
        )
        if not img_ver or img_ver == self.version:
            return
        c = vercmp(self.version, img_ver)
        if c < 0 or (c == 0 and self.version < img_ver):
            rev = self.out(
                "image", "inspect", "-f", fmt % f'"{REVISION_LABEL}"', self.image
            )
            say(f"launcher v{self.version} is older than the image (v{img_ver})")
            if self.self_name.startswith("uvx"):
                note("update", "uvx claude-sandbox@latest")
                note("pin", f"uvx claude-sandbox=={img_ver}")
            else:
                url = f"{RAW_URL}/{rev or 'main'}/container/claude-container"
                note("update", f"curl -fsSLO {url}")
        else:
            say(f"launcher v{self.version} is newer than the local image (v{img_ver})")
            note("pull", f"{self.engine} pull {self.image}")
            note("rebuild", f"{self.self_name} --recreate")
        self.warned = True

    # --- clean --------------------------------------------------------------

    def clean(self, force: bool, images: bool) -> None:
        """Remove this launcher's stopped containers (running too with
        force), unused image tags with images, and orphaned venvs."""
        removed = kept = 0
        listing = self.out(
            "ps", "-a", "--filter", "name=^claude-sandbox-", "--format", "{{.Names}}"
        )
        for n in (listing or "").split("\n"):
            if not n or not self.is_keeper(n):
                continue
            if not force and self.inspect("{{.State.Running}}", n) == "true":
                say(f"kept {n} (running; --force removes it and ends its sessions)")
                kept += 1
                continue
            if self.call("rm", "-f", n) == 0:
                say(f"removed {n}")
                removed += 1
        say(f"{removed} container(s) removed, {kept} running kept")
        if images:
            ref = "reference=*/diamondlightsource/claude-sandbox"
            tags = self.out(
                "images", "--filter", ref, "--format", "{{.Repository}}:{{.Tag}}"
            )
            for img in (tags or "").split("\n"):
                # rmi refuses an image a remaining container uses: that is right.
                if img and self.call("rmi", img, quiet=True) == 0:
                    say(f"removed image {img}")
        self.clean_venvs()

    def clean_venvs(self) -> None:
        """Venvs on the cache volume whose project container is gone, listed
        and removed from throwaway containers with the entrypoint bypassed."""
        if not self.cache:
            return
        vol = f"{self.cache}:/cache"
        found = self.out(
            "run", "--rm", "--entrypoint", "find", "-v", vol, self.image,
            "/cache/venv-for", "-name", "pyvenv.cfg", "-printf", "%h\\n",
        )  # fmt: skip
        removed = 0
        for venv in (found or "").split("\n"):
            if not venv:
                continue
            path = venv.removeprefix("/cache/venv-for")
            if self.exists(names(path)[0]):
                continue
            rm = (
                "run",
                "--rm",
                "--entrypoint",
                "rm",
                "-v",
                vol,
                self.image,
                "-rf",
                venv,
            )
            if self.call(*rm) == 0:
                say(f"removed venv for {path}")
                removed += 1
        say(f"{removed} venv(s) removed")

    # --- create -------------------------------------------------------------

    def create_args(self) -> list[str]:
        """The ``create`` argv: the bash's, flag for flag."""
        o, env, pwd = self.opts, self.env, self.project
        args = [
            "create", "--name", self.name,
            "--device", "/dev/net/tun",
            "--security-opt", "label=disable",
            "-v", f"{self.shared}:/user-terminal-config",
            "-e", f"TERM={env.get('TERM') or 'xterm-256color'}",
            "-e", f"CLAUDE_SANDBOX_TAG={self.tag}",
        ]  # fmt: skip
        engine = basename(self.engine)
        if o.gpu:
            if engine == "podman":
                args += ["--device", "nvidia.com/gpu=all"]
            elif engine == "docker":
                args += ["--gpus", "all"]
            else:
                say("--gpu requires podman or docker")
                raise SystemExit(1)
        allow_devices = [d for d in [env.get("CLAUDE_SANDBOX_ALLOW_DEVICES", "")] if d]
        for device in o.devices:
            args += ["--device", device]
            allow_devices.append(device)
        if o.devices and engine == "podman":
            args += ["--group-add", "keep-groups"]
        if allow_devices:
            args += ["-e", "CLAUDE_SANDBOX_ALLOW_DEVICES=" + "\n".join(allow_devices)]
        if o.gpu:
            args += ["-e", "CLAUDE_SANDBOX_GPU=1"]
        elif env.get("CLAUDE_SANDBOX_GPU"):
            args += ["-e", f"CLAUDE_SANDBOX_GPU={env['CLAUDE_SANDBOX_GPU']}"]
        parent = os.path.dirname(pwd)
        self.skipped_parent = ""
        if o.peers and parent != "/":
            if f"{self.home}/".startswith(f"{parent}/"):
                self.skipped_parent = parent
            else:
                propagation = "bind-propagation=slave"
                args += [
                    "--mount",
                    f"type=bind,src={parent},dst={parent},{propagation}",
                ]
        args += ["-v", f"{pwd}:{pwd}", "-w", pwd]
        if o.host_net:
            args.append("--network=host")
        args += ["-e", f"LANG={env.get('LANG') or 'en_US.UTF-8'}"]
        if self.cache:
            args += ["-v", f"{self.cache}:/cache"]
        args += [
            "-e", f"UV_PROJECT_ENVIRONMENT=/cache/venv-for{pwd}",
            "-e", f"VIRTUAL_ENV=/cache/venv-for{pwd}",
            "-e", "PRE_COMMIT_HOME=/cache/pre-commit",
            "-e", "UV_PYTHON_CACHE_DIR=/cache/uv-python",
        ]  # fmt: skip
        # X11 for the unsandboxed shell only: the shadow masks it from agents.
        if env.get("DISPLAY"):
            args += ["-e", f"DISPLAY={env['DISPLAY']}"]
            if os.path.isdir("/tmp/.X11-unix"):
                args += ["-v", "/tmp/.X11-unix:/tmp/.X11-unix:ro"]
            xauth = env.get("XAUTHORITY") or f"{self.home}/.Xauthority"
            if os.path.isfile(xauth):
                args += ["-v", f"{xauth}:/root/.Xauthority:ro"]
        if os.path.isfile(f"{self.home}/.gitconfig"):
            args += ["-v", f"{self.home}/.gitconfig:/root/.gitconfig-host:ro"]
        # The conf READ-ONLY at the canonical path (Invariant 4).
        if os.path.isfile(self.conf):
            args += ["-v", f"{self.conf}:/etc/claude-sandbox.conf:ro"]
        for m in o.mounts_ro:
            args += ["--mount", f"type=bind,src={m},dst={m},ro,bind-propagation=slave"]
        allow_write = [w for w in [env.get("CLAUDE_SANDBOX_ALLOW_WRITE", "")] if w]
        for m in o.mounts_rw:
            args += ["--mount", f"type=bind,src={m},dst={m},bind-propagation=slave"]
            allow_write.append(m)
        if allow_write:
            args += ["-e", "CLAUDE_SANDBOX_ALLOW_WRITE=" + "\n".join(allow_write)]
        # The rest of CLAUDE_SANDBOX_*, frozen at create (bash: compgen -v).
        for v in sorted(env):
            if (
                v.startswith("CLAUDE_SANDBOX_")
                and _NAME.match(v)
                and v not in NOT_PASSED
            ):
                args += ["-e", f"{v}={env[v]}"]
        return [*args, self.image, "bash", "-c", KEEPER_CMD]

    def create(self) -> None:
        os.makedirs(self.shared, exist_ok=True)
        self.must(*self.create_args())
        say(f"created {self.name}")
        note("image", self.image)
        note("forge auth", f"{self.self_name} shell, then claude-sandbox gh-auth")
        if self.skipped_parent:
            self.warn(
                f"did not mount {self.skipped_parent}: it contains your home directory"
            )

    def reuse(self) -> None:
        """Say what an existing container ignores or lacks; one line if nothing."""
        if not self.is_keeper(self.name):
            say(f"{self.name} does not have the expected keeper command")
            note(
                "rebuild",
                f"{self.self_name} --recreate  (forge logins must be re-done)",
            )
            raise SystemExit(1)
        say(f"reusing {self.name}")
        rebuild = False
        local_img = self.out("image", "inspect", "-f", "{{.Id}}", self.image)
        ctr_img = self.inspect("{{.Image}}", self.name)
        if local_img and ctr_img and local_img != ctr_img:
            created = (self.inspect("{{.Created}}", self.name) or "")[:16].replace(
                "T", " "
            )
            self.warn(
                f"runs an older image than the one pulled (created {created or '?'})"
            )
            rebuild = True
        mounts = self.inspect(
            "{{range .Mounts}}{{println .Destination}}{{end}}", self.name
        )
        if not self.opts.peers and os.path.dirname(self.project) in (
            mounts or ""
        ).split("\n"):
            self.warn("mounts the parent directory; peers are now off by default")
            rebuild = True
        if self.opts.create_opts:
            self.warn(
                "ignored on an existing container: " + " ".join(self.opts.create_opts)
            )
            rebuild = True
        if rebuild:
            note("rebuild", f"{self.self_name} --recreate")

    # --- the session ----------------------------------------------------------

    def session(self, command: list[str], *, pause: bool) -> int:
        """Create or reuse the container, then exec ``command`` in it."""
        if self.opts.recreate and self.exists(self.name):
            self.must("rm", "-f", self.name)
        if self.exists(self.name):
            self.reuse()
        else:
            self.create()
        if not self.running():
            self.must("start", self.name)
            if not self.running():
                # The entrypoint refused (typically: no nested user namespaces).
                say(f"{self.name} exited during start:")
                sys.stderr.flush()
                run([self.engine, "logs", self.name], stdout=2)  # logs >&2
                raise SystemExit(1)
        # An agent redraws the terminal at once: hold a warning for a key.
        if pause and self.warned and os.isatty(0) and os.isatty(2):
            wait_for_key()
        rc = interactive([self.engine, "exec", "-it", self.name, *command])
        if os.isatty(1):
            sys.stdout.write(MOUSE_RESET)
            sys.stdout.flush()
        # Stop the keeper when ours was the last session.
        if self.inspect("{{len .ExecIDs}}", self.name) == "0":
            self.call("stop", "-t", "2", self.name, quiet=True)
        return rc


def launcher(opts: Options) -> Launcher:
    """A launcher for this run: engine present, outdated image noted."""
    it = Launcher(opts, dict(os.environ))
    it.require_engine()
    it.warn_if_outdated()
    return it
