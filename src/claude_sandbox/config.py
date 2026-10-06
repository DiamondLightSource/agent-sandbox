"""The host-global ``/etc/claude-sandbox.conf`` and the port checks.

Ported from ``parse_config``, ``resolve_workspace_root`` and the local-model
and callback-port helpers in ``.devcontainer/claude-sandbox/claude-shadow``.
The conf lives at /etc (placed by install.sh), NOT inside the rw-bound
workspace, so a compromised session cannot rewrite it to widen the next
launch's binds.

The bash applies the conf by exporting ``CLAUDE_SANDBOX_*`` variables, with
any value already in the environment winning. ``parse_config`` keeps that
model: it returns the merged environment, and ``Config.from_env`` reads the
knobs out of it. The argv builder reads the rest of that same environment.

Standard library only: this module is on the launch path (ADR 26).
"""

import glob as _glob
import os
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass

CONFIG_PATH = "/etc/claude-sandbox.conf"

WORKSPACE_ROOT = "CLAUDE_SANDBOX_WORKSPACE_ROOT"
NO_FORGE = "CLAUDE_SANDBOX_NO_FORGE"
EGRESS_JAIL = "CLAUDE_SANDBOX_EGRESS_JAIL"
LOCAL_MODEL_PORT = "CLAUDE_SANDBOX_LOCAL_MODEL_PORT"
LOCAL_PORTS = "CLAUDE_SANDBOX_LOCAL_PORTS"
CALLBACK_PORTS = "CLAUDE_SANDBOX_CALLBACK_PORTS"
GPU = "CLAUDE_SANDBOX_GPU"
ALLOW_DEVICES = "CLAUDE_SANDBOX_ALLOW_DEVICES"
ALLOW_WRITE = "CLAUDE_SANDBOX_ALLOW_WRITE"
ALLOW_IP = "CLAUDE_SANDBOX_ALLOW_IP"
PASS_ENV = "CLAUDE_SANDBOX_PASS_ENV"

# Every variable parse_config can set, in the order the bash lists the keys.
KNOBS = (
    WORKSPACE_ROOT,
    NO_FORGE,
    EGRESS_JAIL,
    LOCAL_MODEL_PORT,
    LOCAL_PORTS,
    CALLBACK_PORTS,
    GPU,
    ALLOW_DEVICES,
    ALLOW_WRITE,
    ALLOW_IP,
    PASS_ENV,
)

# Keys whose value fills the variable only when it is unset or empty, and
# the value a bare key (no `= value`) stands for. `no-forge` takes no value:
# any value, even `0`, means 1, exactly as in the bash.
_DEFAULTS: Mapping[str, tuple[str, str | None]] = {
    "workspace-root": (WORKSPACE_ROOT, None),  # no value: leave unset
    "no-forge": (NO_FORGE, "1"),
    # Egress jail (ADR 0015) is ON by default — see egress_jail_enabled; this
    # key only needs to appear to TURN IT OFF: `egress-jail = 0`.
    "egress-jail": (EGRESS_JAIL, "1"),
    # The port Pi discovers a model on; always part of the relay set (ADR
    # 0020). 0 drops it and Pi's discovery.
    "local-model-port": (LOCAL_MODEL_PORT, "1920"),
    "gpu": (GPU, "1"),
}

# Repeatable keys, accumulated newline-separated onto any value already in
# the environment. An empty value is skipped.
_LISTS: Mapping[str, str] = {
    # Extra outer-loopback TCP ports relayed into the jail (ADR 0020).
    "local-port": LOCAL_PORTS,
    # Ports relayed the other way, so a host browser reaches an OAuth
    # callback server the agent opens on its own loopback (ADR 0021).
    "callback-port": CALLBACK_PORTS,
    "allow-device": ALLOW_DEVICES,
    "allow-write": ALLOW_WRITE,
    # Device IPs the jail keeps reachable past the RFC1918 blackhole.
    "allow-ip": ALLOW_IP,
    "pass-env": PASS_ENV,
}

# bash's [[:space:]] in the C locale. Not str.strip(): that also strips
# Unicode spaces and \x1c-\x1f, which bash keeps.
_SPACE = " \t\n\r\f\v"

Glob = Callable[[str], list[str]]


def host_glob(pattern: str) -> list[str]:
    """Pathname expansion as bash does it: sorted, relative to the cwd."""
    return sorted(_glob.glob(pattern))


def parse_config(path: str, env: Mapping[str, str]) -> dict[str, str]:
    """Apply the conf at ``path`` to a copy of ``env`` and return it.

    Format: ``key = value`` or a bare ``key``; ``#`` starts a comment
    anywhere on a line, and blank lines are ignored. Unknown keys are
    ignored. A missing conf (or anything but a regular file) changes
    nothing; an unreadable one raises OSError.
    """
    merged = dict(env)
    if not os.path.isfile(path):
        return merged
    with open(path, "rb") as conf:
        # bash's `read` drops NUL bytes rather than ending the line.
        text = os.fsdecode(conf.read().replace(b"\0", b""))
    for line in text.split("\n"):
        line = line.partition("#")[0].strip(_SPACE)
        if not line:
            continue
        key, eq, value = line.partition("=")
        key, value = key.rstrip(_SPACE), value.strip(_SPACE) if eq else ""
        if key in _DEFAULTS:
            var, bare = _DEFAULTS[key]
            # no-forge ignores its value: even `no-forge = 0` means 1.
            value = bare if key == "no-forge" else value or bare
            # `: "${VAR:=value}"` — the environment wins unless it is empty.
            if value and not merged.get(var):
                merged[var] = value
        elif key in _LISTS and value:
            var = _LISTS[key]
            merged[var] = f"{merged[var]}\n{value}" if merged.get(var) else value
    return merged


@dataclass(frozen=True, slots=True)
class Config:
    """The conf knobs, read from the environment ``parse_config`` returned.

    Raw strings where the bash compares strings, so that every check below
    behaves exactly as the bash one does.
    """

    workspace_root: str = ""
    no_forge: bool = False
    egress_jail: str = ""
    local_model_port: str = "0"
    local_port_entries: str = ""
    callback_port_entries: str = ""
    gpu: bool = False
    allow_devices: str = ""
    allow_write: str = ""
    allow_ip: str = ""
    pass_env: str = ""

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> "Config":
        return cls(
            workspace_root=env.get(WORKSPACE_ROOT, ""),
            no_forge=env.get(NO_FORGE) == "1",
            egress_jail=env.get(EGRESS_JAIL, ""),
            local_model_port=env.get(LOCAL_MODEL_PORT) or "0",
            local_port_entries=env.get(LOCAL_PORTS, ""),
            callback_port_entries=env.get(CALLBACK_PORTS, ""),
            gpu=env.get(GPU) == "1",
            allow_devices=env.get(ALLOW_DEVICES, ""),
            allow_write=env.get(ALLOW_WRITE, ""),
            allow_ip=env.get(ALLOW_IP, ""),
            pass_env=env.get(PASS_ENV, ""),
        )


def lines(text: str) -> list[str]:
    """Non-empty lines: how the bash walks an accumulated list."""
    return [line for line in text.split("\n") if line]


def words(text: str, glob: Glob = host_glob) -> list[str]:
    """The words of a comma-, space- or newline-separated list.

    The bash splits these lists by leaving the expansion unquoted, which also
    runs pathname expansion on each word: a word holding ``*``, ``?`` or
    ``[`` that matches files in the current directory becomes their names.
    Ported as-is for argv parity (see the phase 1 report on issue #72).
    """
    out: list[str] = []
    for word in re.split(r"[, \t\n]+", text):
        if not word:
            continue
        if any(c in word for c in "*?["):
            out.extend(glob(word) or [word])
        else:
            out.append(word)
    return out


def resolve_workspace_root(config: Config, pwd: str) -> str:
    """The rw bind-mount root: CLAUDE_SANDBOX_WORKSPACE_ROOT, else ``pwd``.

    Set workspace-root to /workspaces to make sibling projects writable.
    """
    return config.workspace_root or pwd


def egress_jail_enabled(config: Config) -> bool:
    """ADR 0015: ON unless explicitly ``0``; any other value means on."""
    return (config.egress_jail or "1") != "0"


def valid_tcp_port(port: str) -> bool:
    """1-65535 in plain decimal, no sign and no leading zero."""
    return re.fullmatch(r"[1-9][0-9]{0,4}", port) is not None and int(port) <= 65535


def _dedup(ports: list[str]) -> list[str]:
    # The bash tracks seen ports as a space-joined string and tests
    # containment, so a port is dropped when " port " occurs anywhere in it.
    seen = " "
    out: list[str] = []
    for port in ports:
        if f" {port} " in seen:
            continue
        seen += f"{port} "
        out.append(port)
    return out


def local_ports(config: Config, glob: Glob = host_glob) -> list[str]:
    """The outbound relay set (ADR 0020), deduplicated, 0 dropped.

    local-model-port plus every local-port entry.
    """
    ports = [config.local_model_port, *words(config.local_port_entries, glob)]
    return _dedup([p for p in ports if p != "0"])


def callback_ports(config: Config, glob: Glob = host_glob) -> list[str]:
    """The inbound callback relay set (ADR 0021), deduplicated."""
    return _dedup(words(config.callback_port_entries, glob))


def local_model_enabled(config: Config, glob: Glob = host_glob) -> bool:
    return bool(local_ports(config, glob))


def callback_enabled(config: Config, glob: Glob = host_glob) -> bool:
    return bool(callback_ports(config, glob))


def validate_local_model_port(config: Config, glob: Glob = host_glob) -> list[str]:
    """Errors in the raw local ports, one message per bad entry.

    Validates the configuration, not the deduplicated set, so a bad entry is
    named by its key. Only local-model-port may be 0.
    """
    errors: list[str] = []
    port = config.local_model_port
    if port != "0" and not valid_tcp_port(port):
        errors.append(
            "claude-sandbox: local-model-port must be 1–65535 (or 0 to disable)."
        )
    for port in words(config.local_port_entries, glob):
        if not valid_tcp_port(port):
            errors.append(
                f"claude-sandbox: local-port entries must be 1–65535, got '{port}'."
            )
    return errors


def validate_callback_ports(config: Config, glob: Glob = host_glob) -> list[str]:
    """Errors in the callback ports, one message per bad entry.

    A port cannot be relayed both ways: the outbound relay's in-jail listener
    would sit on the port the agent needs for its own server, and the inbound
    listener outside would sit on the host service's port.
    """
    errors: list[str] = []
    outbound = local_ports(config, glob)
    for port in words(config.callback_port_entries, glob):
        if not valid_tcp_port(port):
            errors.append(
                f"claude-sandbox: callback-port entries must be 1–65535, got '{port}'."
            )
        elif port in outbound:
            errors.append(
                f"claude-sandbox: port {port} is listed as both callback-port"
                " and local-port/local-model-port."
            )
    return errors
