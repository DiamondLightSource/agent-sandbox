"""The in-container helpers, driven through the CLI in the container context.

tests/doctor.sh runs against the Python CLI too (test_bash_suites.py); the
doctor cases here reach what it does not.
"""

import json
import os
import pty
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

from claude_sandbox import cli, context
from claude_sandbox.context import CONTAINER, JAIL, Where
from claude_sandbox.helpers import auth, commands, doctor, pi_local
from claude_sandbox.tools import find_tool

Main = Callable[..., int]


@pytest.fixture
def main(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Callable[..., int]:
    """``main(*argv, where=CONTAINER)`` with HOME in a temp dir."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("CLAUDE_SANDBOX_LOCAL_MODEL_PORT", raising=False)

    def run(*argv: str, where: Where = CONTAINER) -> int:
        monkeypatch.setattr(context, "current", lambda: where)
        return cli.main(list(argv))

    return run


class Exec(Exception):
    def __init__(self, path: str, argv: list[str]) -> None:
        super().__init__(path)
        self.path, self.argv = path, argv


@pytest.fixture
def execs(monkeypatch: pytest.MonkeyPatch) -> None:
    def execv(path: str, argv: list[str]) -> None:
        raise Exec(path, argv)

    monkeypatch.setattr(os, "execv", execv)


# --- verify, version, update, install, help ---------------------------------


@pytest.mark.usefixtures("execs")
@pytest.mark.parametrize(
    ("argv", "where", "path", "args"),
    [
        (["verify"], CONTAINER, "/usr/local/bin/claude", ["--sandbox-verify"]),
        (["verify", "--agent", "pi"], CONTAINER, "/usr/local/bin/pi",
         ["--sandbox-verify"]),
        (["verify"], JAIL, "/bin/bash",
         ["/usr/libexec/claude-sandbox/verify-sandbox-battery.sh"]),
    ],
)  # fmt: skip
def test_verify(
    main: Main, argv: list[str], where: Where, path: str, args: list[str]
) -> None:
    with pytest.raises(Exec) as exc:
        main(*argv, where=where)
    assert (exc.value.path, exc.value.argv[1:]) == (path, args)


def test_version(
    main: Main, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:  # fmt: skip
    stamp = tmp_path / "version"
    monkeypatch.setenv("CLAUDE_SANDBOX_VERSION_FILE", str(stamp))
    assert main("-v") == 1
    assert "version unknown" in capsys.readouterr().err
    stamp.write_text("4.7.2\n")
    assert main("--version") == 0
    assert capsys.readouterr().out == "claude-sandbox 4.7.2\n"


@pytest.mark.usefixtures("execs")
def test_update(
    main: Main, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:  # fmt: skip
    # Only where the container files are, whatever CLAUDE_SANDBOX_CONTEXT says.
    monkeypatch.setattr(context, "MARKERS", (str(tmp_path / "no-marker"),))
    monkeypatch.delenv("CLAUDE_SANDBOX_HOST_INSTALL", raising=False)
    assert main("update") == 1
    assert "refusing to install outside a container" in capsys.readouterr().err
    monkeypatch.setenv("CLAUDE_SANDBOX_HOST_INSTALL", "1")
    installer = tmp_path / "installer"
    installer.write_text("uvx\n")
    monkeypatch.setenv("CLAUDE_SANDBOX_INSTALLER_FILE", str(installer))
    monkeypatch.setattr(commands, "IMAGE_INSTALL", str(tmp_path))
    monkeypatch.setattr(os, "geteuid", lambda: 1000)
    assert main("update") == 1
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    assert main("update") == 1
    monkeypatch.setattr(commands, "IMAGE_INSTALL", str(tmp_path / "absent"))
    assert main("update") == 1
    err = capsys.readouterr().err
    for text in ("as root", "published container image", "@latest install"):
        assert text in err
    installer.write_text("clone\n")
    on_path = tmp_path / "on_path"
    on_path.mkdir()
    (on_path / "git").write_text("#!/bin/sh\n")
    (on_path / "git").chmod(0o755)
    monkeypatch.setenv("PATH", f"{on_path}:{os.environ['PATH']}")
    clones: list[list[str]] = []

    def git(argv: list[str], check: bool) -> subprocess.CompletedProcess[str]:
        clones.append(argv)
        return subprocess.CompletedProcess(argv, len(clones) - 1)

    monkeypatch.setattr(subprocess, "run", git)
    with pytest.raises(Exec) as exc:
        main("update")
    assert clones[0][:3] == [find_tool("git"), "clone", "--quiet"]
    assert clones[0][0] != str(on_path / "git")
    tmp = clones[0][-1].rsplit("/", 1)[0]
    assert exc.value.argv[0] == "/bin/bash" and "/bin/rm -rf" in exc.value.argv[2]
    assert exc.value.argv[-2:] == ["claude-sandbox-update", tmp]
    assert main("update") == 1  # the second clone fails

    def missing(name: str) -> None:
        return None

    monkeypatch.setattr(commands, "find_tool", missing)
    assert main("update") == 1
    assert "git is not installed" in capsys.readouterr().err


@pytest.mark.parametrize(("installed", "rc"), [(True, 0), (False, 1)])
def test_install(
    main: Main, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
    installed: bool, rc: int,
) -> None:  # fmt: skip
    monkeypatch.setattr(context, "SHADOW", "/bin/sh" if installed else "/nonexistent")
    assert main("install", "--here") == rc
    out = capsys.readouterr()
    assert ("already installed" in out.out) == installed


def test_help_lists_only_what_runs_here(
    main: Main, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main("--help") == 0
    out = capsys.readouterr().out
    assert "    doctor " in out and "    shell " not in out
    assert main(where=JAIL) == 0 and main("help") == 0  # usage, as the bash
    assert "    doctor " in capsys.readouterr().out
    assert main("shell", where=JAIL) == main("gh-auth", where=JAIL) == 1
    assert (
        "refusing gh-auth inside a sandboxed agent session" in capsys.readouterr().err
    )


# --- doctor -------------------------------------------------------------------


@pytest.fixture
def setup(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    lib = tmp_path / "libexec"
    lib.mkdir()
    (lib / "statusline-command.sh").write_text("echo tag\n")
    (lib / "pi-sandbox-tag.ts").write_text("// tag\n")
    rc = tmp_path / "rc"
    rc.mkdir()
    monkeypatch.setenv("CLAUDE_SANDBOX_LIBEXEC", str(lib))
    monkeypatch.setenv("CLAUDE_SANDBOX_TAG_FILE", str(tmp_path / "no-tag"))
    monkeypatch.setenv("USER_TERMINAL_CONFIG", str(rc))
    return tmp_path


def test_doctor_fix_then_ok(
    main: Main, setup: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (setup / "rc" / "bashrc").write_text("# mine")  # no final newline
    (setup / ".claude").mkdir()
    (setup / ".claude" / "settings.json").write_text('{"statusLine": "odd", "x": "é"}')
    assert main("doctor") == 1
    assert main("doctor", "--fix", where=JAIL) == 1
    assert main("doctor", "--fix") == 0
    settings = json.loads((setup / ".claude" / "settings.json").read_text())
    assert settings["statusLine"]["command"] == doctor.SL_CMD and settings["x"] == "é"
    assert (setup / "rc" / "bashrc").read_text() == "# mine\n" + doctor.prompt_block(
        "bash"
    )
    capsys.readouterr()
    assert main("doctor") == 0
    out = capsys.readouterr().out
    assert "  skip     zsh prompt" in out and "  info     container tag" in out


def test_doctor_skips_what_it_cannot_fix(
    main: Main, setup: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (setup / "libexec" / "pi-sandbox-tag.ts").unlink()
    (setup / ".claude").mkdir()
    (setup / ".claude" / "settings.json").write_text("[]")
    main("doctor", "--fix")
    out = capsys.readouterr().out
    assert "is not a JSON object; set statusLine by hand" in out
    assert "pi-sandbox-tag.ts is missing" in out


def test_doctor_leaves_symlinks_alone(
    main: Main, setup: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (setup / "mine.sh").write_text("echo mine\n")
    (setup / ".claude").mkdir()
    (setup / ".claude" / "statusline-command.sh").symlink_to(setup / "mine.sh")
    (setup / ".claude" / "settings.json").symlink_to(setup / "mine.json")
    main("doctor", "--fix")
    out = capsys.readouterr().out
    assert out.count("is a symlink; left as it is") == 2
    assert (setup / "mine.sh").read_text() == "echo mine\n"
    assert not (setup / "mine.json").exists()
    assert not list((setup / ".claude").glob("*.bak-*"))


def test_doctor_replaces_every_old_block(
    main: Main, setup: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    old = f"{doctor.PROMPT_BEGIN}\nold\n{doctor.PROMPT_END}\n"
    zshrc = setup / "rc" / "zshrc"
    zshrc.write_text(f"a\n{old}b\n{doctor.PROMPT_BEGIN}\nunterminated\n")
    main("doctor")
    assert "has an older tag block" in capsys.readouterr().out
    main("doctor", "--fix")
    block = doctor.prompt_block("zsh")
    assert zshrc.read_text() == f"a\n{block}b\n{block}"


# --- pi-local -----------------------------------------------------------------


@pytest.fixture
def server(monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    """What the fake model server answers, by path; absent paths fail."""
    answers: dict[str, object] = {}

    def fetch(url: str) -> object:
        return answers.get(url.split(":", 2)[2].split("/", 1)[1])

    monkeypatch.setattr(pi_local, "fetch", fetch)
    return answers


def models(home: Path) -> dict[str, object]:
    data: dict[str, object] = json.loads(
        (home / ".pi" / "agent" / "models.json").read_text()
    )
    return data


def test_pi_local_manual_then_discovered(
    main: Main, tmp_path: Path, server: dict[str, object]
) -> None:
    assert main("pi-local", "first", "32768") == 0
    path = tmp_path / ".pi" / "agent" / "models.json"
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    first = {
        "id": "first", "name": "Local (lllm2): first",
        "contextWindow": 32768, "maxTokens": 8192, "note": "kept on its own id",
    }  # fmt: skip
    assert models(tmp_path)["providers"] == {"lllm2": {
        "baseUrl": "http://127.0.0.1:1920/v1", "api": "openai-completions",
        "apiKey": "local",
        "compat": {"supportsDeveloperRole": False, "supportsReasoningEffort": False},
        "models": [{k: v for k, v in first.items() if k != "note"}],
    }}  # fmt: skip
    custom = {"providers": {"other": {"apiKey": "keep"}, "lllm2": {
        "apiKey": "", "compat": {"supportsDeveloperRole": True}, "models": [first],
    }}}  # fmt: skip
    path.write_text(json.dumps(custom))
    server["v1/models"] = {"data": [{"id": "found"}]}
    server["props"] = {"default_generation_settings": {"n_ctx": 262144.0}}
    assert main("pi-local", "--port", "8080", where=JAIL) == 0
    lllm2 = models(tmp_path)["providers"]
    assert lllm2 == {
        "other": {"apiKey": "keep"},
        "lllm2": {
            "baseUrl": "http://127.0.0.1:8080/v1", "api": "openai-completions",
            "apiKey": "",
            "compat": {"supportsDeveloperRole": True, "supportsReasoningEffort": False},
            "models": [{"id": "found", "name": "Local (lllm2): found",
                        "contextWindow": 262144, "maxTokens": 32000}],
        },
    }  # fmt: skip


@pytest.mark.parametrize(
    ("argv", "models_answer", "props_answer", "rc"),
    [
        (["a"], None, None, 2),
        (["m", "4096", "0"], None, None, 2),
        (["", "4096"], None, None, 2),
        (["m", "100"], None, None, 2),
        ([], {"data": []}, None, 1),
        ([], {"data": [{"id": ""}]}, None, 1),
        (
            [],
            {"data": [{"id": "m"}]},
            {"default_generation_settings": {"n_ctx": 12.5}},
            1,
        ),
        (
            [],
            {"data": [{"id": "m"}]},
            {"default_generation_settings": {"n_ctx": True}},
            1,
        ),
        ([], {"data": [{"id": "m"}]}, {}, 1),
    ],
)
def test_pi_local_refusals(
    main: Main, server: dict[str, object], argv: list[str],
    models_answer: object, props_answer: object, rc: int,
) -> None:  # fmt: skip
    server["v1/models"], server["props"] = models_answer, props_answer
    assert main("pi-local", *argv) == rc


@pytest.mark.parametrize("text", ["broken", "[]", '{"providers": 0}'])
def test_pi_local_keeps_a_config_it_cannot_read(
    main: Main, tmp_path: Path, text: str
) -> None:
    path = tmp_path / ".pi" / "agent" / "models.json"
    path.parent.mkdir(parents=True)
    path.write_text(text)
    assert main("pi-local", "m", "4096", "1921") == 1
    assert path.read_text() == text


def test_pi_local_fetch_fails_quietly() -> None:
    assert pi_local.fetch("http://127.0.0.1:9/v1/models") is None


# --- gh-auth, glab-auth -----------------------------------------------------------


@pytest.fixture
def forge(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> list[tuple[list[str], str | None]]:
    """gh and glab in a system directory, and same-named executables first on PATH."""
    calls: list[tuple[list[str], str | None]] = []
    for where in ("system", "on_path"):
        (tmp_path / where).mkdir()
        for name in ("gh", "glab"):
            (tmp_path / where / name).write_text("#!/bin/sh\n")
            (tmp_path / where / name).chmod(0o755)
    monkeypatch.setattr(auth, "FORGE_PATH", (str(tmp_path / "system"),))
    monkeypatch.setenv("PATH", f"{tmp_path / 'on_path'}:{os.environ['PATH']}")

    def run(argv: list[str], stdin: str | None = None) -> int:
        calls.append((argv, stdin))
        return 4 if "fail" in argv else 0

    monkeypatch.setattr(auth, "run", run)

    def secret(prompt: str) -> str:
        return "ghp_x"

    monkeypatch.setattr(auth, "read_secret", secret)
    return calls


def test_gh_auth(main: Main, forge: list[tuple[list[str], str | None]]) -> None:
    assert main("gh-auth") == 0
    gh = forge[0][0][0]
    assert gh.endswith("/system/gh")
    assert forge[0] == ([gh, "auth", "login", "--with-token"], "ghp_x\n")
    assert [argv for argv, _ in forge[1:]] == [
        [gh, "auth", "setup-git"],
        [gh, "auth", "status"],
    ]


def test_glab_auth(main: Main, forge: list[tuple[list[str], str | None]]) -> None:
    assert main("glab-auth", "gitlab.example") == 0
    glab = forge[0][0][0]
    assert glab.endswith("/system/glab")
    assert forge[0] == (
        [glab, "auth", "login", "--stdin", "--hostname", "gitlab.example"],
        "ghp_x\n",
    )
    assert forge[2][0] == [
        glab,
        "config",
        "set",
        "--global",
        "host",
        "gitlab.example",
    ]
    forge.clear()
    assert main("glab-auth", "fail") == 4 and len(forge) == 1


def test_a_missing_forge_cli_refuses(
    main: Main, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(auth, "FORGE_PATH", ("/nonexistent",))
    assert main("gh-auth") == main("glab-auth") == 1
    assert "gh is not installed" in capsys.readouterr().err


def test_read_secret_is_unechoed_on_a_terminal(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    master, slave = pty.openpty()
    with os.fdopen(slave) as tty:
        monkeypatch.setattr(sys, "stdin", tty)
        os.write(master, b" tok \n")
        assert auth.read_secret("PAT: ") == "tok"
    os.close(master)
    out = capsys.readouterr()
    assert (out.err, out.out) == ("PAT: ", "\n")
    read, write = os.pipe()
    os.write(write, b"piped")
    os.close(write)
    with os.fdopen(read) as pipe:
        monkeypatch.setattr(sys, "stdin", pipe)
        assert auth.read_secret("PAT: ") == "piped"
    assert auth.run(["true"]) == 0


# --- alerts, and doctor's checks of the PATH guards -----------------------------


@pytest.fixture
def state(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """The PATH watcher's state directories, in a temp dir."""
    from claude_sandbox import watch

    monkeypatch.setattr(watch, "STATE_DIR", str(tmp_path / "run"))
    monkeypatch.setattr(watch, "FALLBACK_STATE_DIR", str(tmp_path / "tmp"))
    (tmp_path / "run").mkdir()
    return tmp_path / "run"


def test_alerts(main: Main, state: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (state / "alerts").write_text("2026-10-06 12:00:00 cleared x (a git hook)\n")
    assert main("alerts") == 0
    assert capsys.readouterr().out == "2026-10-06 12:00:00 cleared x (a git hook)\n"
    assert main("alerts", "--clear") == 0
    assert main("alerts") == 0
    assert capsys.readouterr().out == ""
    # The session must not see or clear them.
    assert main("alerts", where=JAIL) == 1
    assert "alerts" in capsys.readouterr().err
    (state / "alerts").unlink()
    (state / "alerts").mkdir()  # cannot be emptied
    assert main("alerts", "--clear") == 1
    assert "could not empty the alerts" in capsys.readouterr().err


def test_doctor_checks_the_path_guards(
    main: Main,
    state: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from claude_sandbox.shadow import SHIM

    shadow = tmp_path / "shadow"
    monkeypatch.setattr(doctor, "SHADOW", str(shadow))
    venv = tmp_path / "venv"
    venv.mkdir()
    monkeypatch.setenv("PATH", f"{venv}:/usr/local/bin:/usr/bin:/bin")
    monkeypatch.setenv("USER_TERMINAL_CONFIG", str(tmp_path / "none"))

    def report() -> str:
        doctor.Doctor(False).guards()
        return capsys.readouterr().out

    assert "skip     entry points" in report()  # the bash shadow
    shadow.write_text(SHIM)
    out = report()
    assert "ok       entry points" in out and "ok       quarantined" in out
    (venv / "codex").write_text("#!/bin/sh\n")
    (venv / "codex").chmod(0o755)
    (state / "alerts").write_text("one\ntwo\n")
    out = report()
    assert f"warn     entry points           {venv}/codex is ahead of" in out
    assert "warn     quarantined            2 alert(s)" in out

    # A warning alone fails the report; nothing for --fix to do about it.
    def nothing(*args: object) -> None:
        return None

    for check in ("tag", "file", "claude_settings", "prompt"):
        monkeypatch.setattr(doctor.Doctor, check, nothing)
    assert main("doctor") == 1
    assert "doctor --fix" not in capsys.readouterr().out
