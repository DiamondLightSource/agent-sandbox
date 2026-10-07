"""``doctor``: check, and with ``--fix`` apply, the recommended user setup.

The container tag in the Claude status line, the Pi footer and the shell
prompts. Every file ``--fix`` changes is backed up first. The paths come
from the environment so the tests stay hermetic.

Where the Python shadow is installed it also checks, from outside the jail,
that the agents' names reach it on PATH (Invariant 1) and whether the PATH
watcher has quarantined anything (ADR 27). Neither is for ``--fix``: what
put a file there needs a person to look at it. Nor are the last two, that
the container has the ``/dev/net/tun`` the egress jail needs (issue #71)
and a passt new enough for it (issue #85): those are the
container's to give.
"""

import json
import os
import shutil
import time
from typing import cast

from .. import config, context, watch
from ..bwrap import SHADOW_DIR
from ..profiles import LIBEXEC
from ..shadow import SHIM, entry_point_problems
from ..tools import write_atomic

# The installed shadow; the Python one is the shim.
SHADOW = f"{SHADOW_DIR}/claude"

SL_CMD = "bash $HOME/.claude/statusline-command.sh"
PROMPT_BEGIN = "# >>> claude-sandbox prompt tag >>>"
PROMPT_END = "# <<< claude-sandbox prompt tag <<<"

ZSH_BLOCK = """\
if [ -r /etc/claude-sandbox-tag ]; then
    __cs_tag="%F{white}$(cat /etc/claude-sandbox-tag)%f "
    # Put the tag at the start of the line above the input line. A theme such
    # as dst opens with a blank line and puts user@host on the next one; the
    # tag belongs on that user@host line, not the blank one.
    case "$PROMPT" in
        *"$__cs_tag"*) ;;
        *$'\\n'*$'\\n'*)
            __cs_head="${PROMPT%$'\\n'*$'\\n'*}"
            PROMPT="$__cs_head"$'\\n'"$__cs_tag${PROMPT#"$__cs_head"$'\\n'}"
            unset __cs_head ;;
        *) PROMPT="$__cs_tag$PROMPT" ;;
    esac
    unset __cs_tag
fi
"""

BASH_BLOCK = """\
if [ -r /etc/claude-sandbox-tag ]; then
    __cs_tag="$(cat /etc/claude-sandbox-tag)"
    case "$PS1" in *"$__cs_tag"*) ;; *) PS1="\\[\\033[0;37m\\]$__cs_tag\\[\\033[0m\\] $PS1" ;; esac
    unset __cs_tag
fi
"""  # noqa: E501


def prompt_block(shell: str) -> str:
    """The rc block that prefixes the container tag to the prompt.

    It does nothing outside a launcher-made container, so a terminal config
    shared with the host stays safe, and a re-sourced rc adds it once.
    """
    body = {"zsh": ZSH_BLOCK, "bash": BASH_BLOCK}[shell]
    return (
        f"{PROMPT_BEGIN}\n"
        "# Added by `claude-sandbox doctor --fix`: shows the tag of the"
        " claude-sandbox\n"
        "# container this shell runs in. Delete this block to remove it.\n"
        f"{body}{PROMPT_END}\n"
    )


def _lines(text: str) -> list[str]:
    """awk's records: newline-separated, a final newline optional."""
    lines = text.split("\n")
    if lines[-1] == "":
        lines.pop()
    return lines


def _read(path: str) -> str:
    with open(path, encoding="utf-8", errors="surrogateescape", newline="") as f:
        return f.read()


def _write(path: str, text: str, mode: str = "w") -> None:
    with open(path, mode, encoding="utf-8", errors="surrogateescape", newline="") as f:
        f.write(text)


class Doctor:
    def __init__(self, fix: bool) -> None:
        env = os.environ
        self.fix = fix
        self.pending = False
        self.warned = False
        self.path = env.get("PATH", "")
        self.home = env.get("HOME") or os.path.expanduser("~")
        self.libexec = env.get("CLAUDE_SANDBOX_LIBEXEC") or LIBEXEC
        self.tag_file = env.get("CLAUDE_SANDBOX_TAG_FILE") or "/etc/claude-sandbox-tag"
        self.terminal_config = (
            env.get("USER_TERMINAL_CONFIG") or "/user-terminal-config"
        )

    def report(self, status: str, subject: str, detail: str) -> None:
        print(f"  {status:<8} {subject:<22} {detail}")

    def todo(self, subject: str, detail: str) -> None:
        """A problem that --fix would repair."""
        self.report("todo", subject, detail)
        self.pending = True

    def backup(self, path: str) -> None:
        copy = f"{path}.bak-{time.strftime('%Y%m%d-%H%M%S')}"
        shutil.copy2(path, copy)
        self.report("backup", os.path.basename(path), f"saved the original as {copy}")

    def linked(self, subject: str, dest: str) -> bool:
        """A symlinked file is the user's own arrangement: leave it alone."""
        if os.path.islink(dest):
            self.report("skip", subject, f"{dest} is a symlink; left as it is")
            return True
        return False

    def tag(self) -> None:
        if os.path.isfile(self.tag_file) and os.path.getsize(self.tag_file) > 0:
            self.report("ok", "container tag", _read(self.tag_file).rstrip("\n"))
        else:
            # Not fixable from inside: the launcher sets the tag at create time.
            self.report(
                "info",
                "container tag",
                "none; containers from the uvx launcher get one"
                " (use --recreate on an older one)",
            )

    def file(self, subject: str, shipped: str, dest: str, mode: int = 0o755) -> None:
        """DEST must be a copy of SHIPPED."""
        if not os.access(shipped, os.R_OK):
            self.report("skip", subject, f"{shipped} is missing; re-run the install")
            return
        if self.linked(subject, dest):
            return
        present = os.path.isfile(dest)
        if present and _read(shipped) == _read(dest):
            self.report("ok", subject, dest)
        elif not self.fix:
            state = "not the recommended one" if present else "absent"
            self.todo(subject, f"{dest} is {state}")
        else:
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            if present:
                self.backup(dest)
            with open(shipped, "rb") as f:
                write_atomic(dest, f.read(), mode)
            self.report("fixed", subject, f"installed {dest}")

    def claude_settings(self) -> None:
        settings = f"{self.home}/.claude/settings.json"
        if self.linked("claude settings", settings):
            return
        present = os.path.isfile(settings)
        data: object = {}
        if present:
            try:
                data = json.loads(_read(settings))
            except ValueError:
                data = None
        if not isinstance(data, dict):
            self.report(
                "skip",
                "claude settings",
                f"{settings} is not a JSON object; set statusLine by hand",
            )
            return
        data = cast(dict[str, object], data)
        line = data.get("statusLine")
        command = (
            cast(dict[str, object], line).get("command")
            if isinstance(line, dict)
            else None
        )
        if present and command == SL_CMD:
            self.report("ok", "claude settings", "statusLine runs the script")
        elif not self.fix:
            self.todo(
                "claude settings", f"statusLine in {settings} does not run the script"
            )
        else:
            if present:
                self.backup(settings)
            os.makedirs(os.path.dirname(settings), exist_ok=True)
            data["statusLine"] = {"type": "command", "command": SL_CMD}
            text = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
            write_atomic(settings, text.encode(), 0o644)
            self.report("fixed", "claude settings", "statusLine now runs the script")

    def prompt(self, shell: str) -> None:
        """The prompt block in the shared rc file for SHELL."""
        rc = f"{self.terminal_config}/{shell}rc"
        subject = f"{shell} prompt"
        if not os.path.isfile(rc):
            self.report(
                "skip", subject, f"{rc} is absent (only DLS base-image shells read it)"
            )
            return
        lines = _lines(_read(rc))
        block = _lines(prompt_block(shell))
        if PROMPT_BEGIN not in lines:
            if not self.fix:
                self.todo(subject, f"{rc} has no container tag in the prompt")
                return
            self.backup(rc)
            _write(rc, "\n" + prompt_block(shell), "a")
            self.report(
                "fixed", subject, f"appended the tag block to {rc} (new shells show it)"
            )
            return
        # Every line from a begin marker to an end marker, markers included.
        inside, found, kept = False, list[str](), list[str]()
        for line in lines:
            if line == PROMPT_BEGIN:
                inside = True
                kept += block
            if inside:
                found.append(line)
            else:
                kept.append(line)
            if line == PROMPT_END:
                inside = False
        if found == block:
            self.report("ok", subject, rc)
        elif not self.fix:
            self.todo(subject, f"{rc} has an older tag block")
        else:
            self.backup(rc)
            # Rewrite in place, not replace: keep the rc's mode and any link.
            _write(rc, "".join(f"{line}\n" for line in kept))
            self.report(
                "fixed", subject, f"updated the tag block in {rc} (new shells show it)"
            )

    def warn(self, subject: str, detail: str) -> None:
        """A problem for a person, not for --fix."""
        self.report("warn", subject, detail)
        self.warned = True

    def guards(self) -> None:
        """The entry points reach the shadow; nothing is quarantined."""
        try:
            python_shadow = _read(SHADOW) == SHIM
        except OSError:
            python_shadow = False
        if not python_shadow:
            self.report("skip", "entry points", "the Python shadow is not installed")
            return
        problems = entry_point_problems(self.path)
        for path, name in problems:
            self.warn(
                "entry points",
                f"{path} is ahead of {SHADOW_DIR}/{name} on PATH; remove it and"
                " review the session that created it",
            )
        if not problems:
            self.report("ok", "entry points", "claude, codex, pi reach the shadow")
        alerts = watch.read_alerts()
        if alerts:
            self.warn(
                "quarantined",
                f"{len(alerts)} alert(s); see `claude-sandbox alerts`",
            )
        else:
            self.report("ok", "quarantined", "nothing")

    def tun(self) -> None:
        """The egress jail's device, judged from the container: an agent
        session has its own /dev."""
        tun = config.TUN
        if context.current() is context.JAIL:
            self.report("skip", "tun device", "run doctor outside the agent")
        elif not config.egress_jail_configured(config.CONFIG_PATH, os.environ):
            self.report("skip", "tun device", "the egress jail is off")
        elif config.tun_missing(config.CONFIG_PATH, os.environ, tun):
            self.warn(
                "tun device",
                f"{tun} is missing; add --device={tun} to the container,"
                " then rebuild or re-create it",
            )
        else:
            self.report("ok", "tun device", f"{tun} present")

    def passt(self) -> None:
        """Whether the installed passt is new enough for the egress jail
        (issue #85). An unreadable version is a note, not an alarm."""
        if context.current() is context.JAIL:
            self.report("skip", "passt", "run doctor outside the agent")
            return
        if not config.egress_jail_configured(config.CONFIG_PATH, os.environ):
            self.report("skip", "passt", "the egress jail is off")
            return
        version = config.passt_version()
        if config.passt_date(version) is None:
            self.report(
                "info",
                "passt",
                f"cannot read its version; the egress jail needs"
                f" {config.PASST_MIN} or later",
            )
        elif config.passt_too_old(config.CONFIG_PATH, os.environ, version):
            self.warn(
                "passt",
                f"{version} is older than {config.PASST_MIN}, too old for the"
                " egress jail; rebuild on a newer base image, such as"
                " Ubuntu 24.04 or Debian 13",
            )
        else:
            self.report("ok", "passt", version)

    def run(self) -> int:
        self.tag()
        self.file(
            "claude status line",
            f"{self.libexec}/statusline-command.sh",
            f"{self.home}/.claude/statusline-command.sh",
        )
        self.claude_settings()
        self.file(
            "pi footer",
            f"{self.libexec}/pi-sandbox-tag.ts",
            f"{self.home}/.pi/agent/extensions/claude-sandbox-tag.ts",
            0o644,
        )
        self.prompt("zsh")
        self.prompt("bash")
        self.guards()
        self.tun()
        self.passt()
        if self.warned and not self.pending:
            return 1
        if self.pending:
            print(
                "Run `claude-sandbox doctor --fix` to apply the recommended setup."
                " It backs up every file it changes."
            )
            return 1
        return 0
