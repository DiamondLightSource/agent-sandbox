"""The conf parser and the knobs read from it (config.py).

Literal expectations for what the bash argv suite (scenarios 8 and 11) and
the comparison harness's conf cases checked against the bash shadow.
"""

from pathlib import Path

import pytest

from claude_sandbox.config import (
    Config,
    callback_ports,
    egress_jail_enabled,
    local_ports,
    parse_config,
    resolve_workspace_root,
    validate_callback_ports,
    validate_local_model_port,
)

P = "CLAUDE_SANDBOX_"


def knobs(conf: str | None, env: dict[str, str], tmp_path: Path) -> dict[str, str]:
    """The knobs ``parse_config`` sets from ``conf`` (None: no file) over
    ``env``."""
    path = tmp_path / "sandbox.conf"
    if conf is not None:
        path.write_bytes(conf.encode())
    merged = parse_config(str(path), env)
    return {k.removeprefix(P): v for k, v in merged.items() if k.startswith(P)}


@pytest.mark.parametrize(
    ("conf", "env", "expected"),
    [
        ("workspace-root = /custom/root\n", {}, {"WORKSPACE_ROOT": "/custom/root"}),
        ("no-forge\n", {}, {"NO_FORGE": "1"}),
        # no-forge takes no value: even `no-forge = 0` means 1.
        ("no-forge = 0\n", {}, {"NO_FORGE": "1"}),
        # ...but the environment can still turn it off.
        ("no-forge\n", {f"{P}NO_FORGE": "0"}, {"NO_FORGE": "0"}),
        # A conf list accumulates onto the environment's.
        (
            "gpu\nallow-device = /dev/null\nallow-device = /dev/zero\n",
            {f"{P}ALLOW_DEVICES": "/dev/full"},
            {"GPU": "1", "ALLOW_DEVICES": "/dev/full\n/dev/null\n/dev/zero"},
        ),
        ("gpu\n", {f"{P}GPU": "0"}, {"GPU": "0"}),  # the environment wins
        ("allow-write = /some/path\n", {}, {"ALLOW_WRITE": "/some/path"}),
        (
            "allow-write = /path/one\nallow-write = /path/two\n",
            {},
            {"ALLOW_WRITE": "/path/one\n/path/two"},
        ),
        ("pass-env = DOCKER_HOST\n", {}, {"PASS_ENV": "DOCKER_HOST"}),
        # A comma-separated value is kept whole, for the builder to split.
        (
            "pass-env = A_VAR, B_VAR\npass-env = C_VAR\n",
            {},
            {"PASS_ENV": "A_VAR, B_VAR\nC_VAR"},
        ),
        ("egress-jail\n", {}, {"EGRESS_JAIL": "1"}),
        ("egress-jail = 0\n", {}, {"EGRESS_JAIL": "0"}),
        ("egress-jail\n", {f"{P}EGRESS_JAIL": "0"}, {"EGRESS_JAIL": "0"}),
        ("allow-ip = 172.23.1.2\n", {}, {"ALLOW_IP": "172.23.1.2"}),
        ("uv-python-store = writable\n", {}, {"UV_PYTHON_STORE": "writable"}),
        ("uv-python-store\n", {}, {}),  # a bare key changes nothing
        (
            "allow-ip = 172.23.1.2\nallow-ip = 10.0.5.6\n",
            {},
            {"ALLOW_IP": "172.23.1.2\n10.0.5.6"},
        ),
        ("allow-ip =\n", {}, {}),  # an empty list entry is skipped
        (
            "# comment\n\nworkspace-root = /from/conf\n# another\n",
            {},
            {"WORKSPACE_ROOT": "/from/conf"},
        ),
        (
            "workspace-root = /from/config\n",
            {f"{P}WORKSPACE_ROOT": "/from/env"},
            {"WORKSPACE_ROOT": "/from/env"},
        ),
        (None, {}, {}),  # no conf file changes nothing
        # Whitespace, CRLF, trailing comments, an empty key, an unknown key,
        # `=` in a value, a comment-only value, a NUL, no final newline.
        (
            "\t workspace-root\t=  /a b # trailing comment\r\n=orphan\n"
            "not-a-key = 1\nallow-write=/x=y\nallow-write = #only a comment\n"
            "allow-ip = 1.2.3.4\x00\ncallback-port = 1455",
            {},
            {
                "WORKSPACE_ROOT": "/a b",
                "CALLBACK_PORTS": "1455",
                "ALLOW_WRITE": "/x=y",
                "ALLOW_IP": "1.2.3.4",
            },
        ),
        # An empty variable counts as unset.
        (
            "workspace-root = /from/conf\nlocal-model-port = 0\n",
            {f"{P}WORKSPACE_ROOT": "", f"{P}LOCAL_MODEL_PORT": ""},
            {"WORKSPACE_ROOT": "/from/conf", "LOCAL_MODEL_PORT": "0"},
        ),
        # An empty value sets nothing.
        (
            "workspace-root =\n",
            {f"{P}WORKSPACE_ROOT": ""},
            {"WORKSPACE_ROOT": ""},
        ),
        ("local-model-port\n", {}, {"LOCAL_MODEL_PORT": "1920"}),
    ],
)
def test_parse_config(
    tmp_path: Path, conf: str | None, env: dict[str, str], expected: dict[str, str]
) -> None:
    assert knobs(conf, env, tmp_path) == expected


@pytest.mark.parametrize(
    ("value", "enabled"),
    [("", True), ("1", True), ("0", False), ("off", True)],  # on unless 0
)
def test_egress_jail_is_on_unless_0(value: str, enabled: bool) -> None:
    assert egress_jail_enabled(Config(egress_jail=value)) is enabled


@pytest.mark.parametrize(
    ("override", "pwd", "expected"),
    [
        # $PWD, never promoted to /workspaces.
        ("", "/workspaces/claude-sandbox2", "/workspaces/claude-sandbox2"),
        ("", "/workspaces/cs2/sub/deeper", "/workspaces/cs2/sub/deeper"),
        ("", "/tmp/myproject", "/tmp/myproject"),
        ("", "/workspaces", "/workspaces"),
        # The override wins wherever $PWD is.
        ("/srv/custom", "/workspaces/foo", "/srv/custom"),
        ("/srv/custom", "/tmp/bar", "/srv/custom"),
        ("/workspaces", "/workspaces/foo", "/workspaces"),
    ],
)  # fmt: skip
def test_resolve_workspace_root(override: str, pwd: str, expected: str) -> None:
    assert resolve_workspace_root(Config(workspace_root=override), pwd) == expected


# The outbound and callback relay sets, and the errors validating each.
Ports = tuple[list[str], list[str], list[str], list[str]]


def ports(env: dict[str, str]) -> Ports:
    config = Config.from_env(env)
    return (
        local_ports(config),
        callback_ports(config),
        validate_local_model_port(config),
        validate_callback_ports(config),
    )


def bad_local(port: str) -> str:
    return f"claude-sandbox: local-port entries must be 1–65535, got '{port}'."


BAD_MODEL = "claude-sandbox: local-model-port must be 1–65535 (or 0 to disable)."
BOTH_WAYS = (
    "claude-sandbox: port 1455 is listed as both callback-port and"
    " local-port/local-model-port."
)


PORT_CASES: list[tuple[dict[str, str], Ports]] = [
    # Deduplicated, env and conf merged, the model port first.
    (
        {
            f"{P}LOCAL_MODEL_PORT": "1920",
            f"{P}LOCAL_PORTS": "7000,8080\n8080\n1920, 9000",
            f"{P}CALLBACK_PORTS": "1455",
        },
        (["1920", "7000", "8080", "9000"], ["1455"], [], []),
    ),
    # Every bad entry named by its key.
    (
        {
            f"{P}LOCAL_MODEL_PORT": "01",
            f"{P}LOCAL_PORTS": "0\n65536 abc 65535 +1",
            f"{P}CALLBACK_PORTS": "99999\n1455",
        },
        (
            ["01", "65536", "abc", "65535", "+1"],
            ["99999", "1455"],
            [BAD_MODEL, *map(bad_local, ("0", "65536", "abc", "+1"))],
            ["claude-sandbox: callback-port entries must be 1–65535, got '99999'."],
        ),
    ),
    # A port relayed both ways, reported for each listing.
    (
        {
            f"{P}LOCAL_MODEL_PORT": "0",
            f"{P}LOCAL_PORTS": "8080\n1455",
            f"{P}CALLBACK_PORTS": "1455\n1455",
        },
        (["8080", "1455"], ["1455"], [], [BOTH_WAYS, BOTH_WAYS]),
    ),
    # Early in a long list: the bash could miss this to SIGPIPE.
    (
        {
            f"{P}LOCAL_MODEL_PORT": "1455",
            f"{P}LOCAL_PORTS": ",".join(map(str, range(2000, 2400))),
            f"{P}CALLBACK_PORTS": "1455",
        },
        (
            ["1455", *map(str, range(2000, 2400))],
            ["1455"],
            [],
            [BOTH_WAYS],
        ),
    ),
    ({f"{P}LOCAL_MODEL_PORT": "0"}, ([], [], [], [])),  # no relay at all
    ({}, ([], [], [], [])),
    # Split only, never globbed against the (jail-writable) cwd.
    (
        {f"{P}LOCAL_PORTS": "1?", f"{P}CALLBACK_PORTS": "[x]"},
        (
            ["1?"],
            ["[x]"],
            [bad_local("1?")],
            ["claude-sandbox: callback-port entries must be 1–65535, got '[x]'."],
        ),
    ),
    # Deduplicated by value; the bad model port is refused.
    (
        {f"{P}LOCAL_MODEL_PORT": "1 2", f"{P}LOCAL_PORTS": "1 2 1"},
        (["1 2", "1", "2"], [], [BAD_MODEL], []),
    ),
]


@pytest.mark.parametrize(("env", "expected"), PORT_CASES)
def test_relay_ports(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    env: dict[str, str],
    expected: Ports,
) -> None:
    (tmp_path / "12").touch()  # what `1?` would match as a glob
    monkeypatch.chdir(tmp_path)
    assert ports(env) == expected


SHIPPED_CONF = Path(__file__).resolve().parents[2] / ".devcontainer/claude-sandbox.conf"


def test_the_shipped_conf_relays_the_model_port_and_no_callback() -> None:
    config = Config.from_env(parse_config(str(SHIPPED_CONF), {}))
    assert local_ports(config) == ["1920"]
    # Each callback port binds a host port: one sandbox at a time could hold it.
    assert callback_ports(config) == []
    # An explicit 0 in the environment turns the shipped relay off.
    env = {f"{P}LOCAL_MODEL_PORT": "0"}
    assert local_ports(Config.from_env(parse_config(str(SHIPPED_CONF), env))) == []


@pytest.mark.parametrize(
    "port", ["-1", "65536", "1920,fork", "localhost:1920", "01920", "9" * 20]
)
def test_invalid_relay_ports_are_refused(port: str) -> None:
    assert validate_local_model_port(Config(local_model_port=port))
    assert validate_local_model_port(Config(local_port_entries=f"8082 {port}"))
    assert validate_callback_ports(Config(callback_port_entries=port))


def test_callback_ports_merge_and_may_not_be_relayed_outward() -> None:
    config = Config(callback_port_entries="1456,1455\n53692\n1455\n53692")
    assert callback_ports(config) == ["1456", "1455", "53692"]
    assert validate_callback_ports(config) == []
    for overlap in (
        Config(local_model_port="1920", callback_port_entries="1920"),
        Config(local_port_entries="8082", callback_port_entries="53692 8082"),
    ):
        assert validate_callback_ports(overlap)
