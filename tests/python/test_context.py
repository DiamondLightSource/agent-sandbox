"""Where the CLI is, and what each command does there."""

from pathlib import Path

import pytest

from claude_sandbox import context
from claude_sandbox.context import CONTAINER, HOST, JAIL, Action, Requirement, Where


@pytest.mark.parametrize(
    ("env", "markers", "where"),
    [
        ({}, False, HOST),
        ({}, True, CONTAINER),
        ({"CLAUDE_SANDBOX_NESTED": "1"}, True, HOST),
        ({"CLAUDE_SANDBOX_CONTEXT": "container"}, False, CONTAINER),
        ({"CLAUDE_SANDBOX_CONTEXT": "host"}, True, HOST),
        ({"CLAUDE_SANDBOX_CONTEXT": "jail"}, False, HOST),  # not a seam value
        # The jail wins over every seam: none of them can leave it.
        ({"IS_SANDBOX": "1", "CLAUDE_SANDBOX_CONTEXT": "host"}, False, JAIL),
        ({"IS_SANDBOX": "1", "CLAUDE_SANDBOX_NESTED": "1"}, True, JAIL),
    ],
)
def test_detect(env: dict[str, str], markers: bool, where: Where) -> None:
    assert context.detect(env, lambda path: markers) is where


def test_install_needs_the_container_files_or_the_override(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(context, "MARKERS", (str(tmp_path),))
    assert context.may_install({})
    monkeypatch.setattr(context, "MARKERS", (str(tmp_path / "absent"),))
    assert not context.may_install({"CLAUDE_SANDBOX_CONTEXT": "container"})
    assert context.may_install({"CLAUDE_SANDBOX_HOST_INSTALL": "1"})


def test_current_is_detected_once() -> None:
    context.current.cache_clear()
    assert context.current() is context.current()


@pytest.mark.parametrize(
    ("where", "action"),
    [(CONTAINER, Action.RUN), (HOST, Action.FORWARD), (JAIL, Action.REFUSE)],
)
def test_action(where: Where, action: Action) -> None:
    assert context.action(Requirement(frozenset({CONTAINER}), HOST), where) is action


def test_requires_records_the_requirement() -> None:
    @context.requires(CONTAINER, JAIL, forward_from=HOST)
    def helper() -> int:
        return 0

    assert context.requirement(helper) == Requirement(
        frozenset({CONTAINER, JAIL}), HOST
    )


@pytest.mark.parametrize(
    ("where", "installed", "text"),
    [
        (JAIL, True, "refusing shell inside a sandboxed agent session"),
        (CONTAINER, True, "already inside an claude-sandbox container"),
        (CONTAINER, False, "install it with: uvx claude-sandbox install"),
        (HOST, False, "shell runs inside a claude-sandbox container, not on the host"),
    ],
)
def test_refusal(
    monkeypatch: pytest.MonkeyPatch, where: Where, installed: bool, text: str
) -> None:
    monkeypatch.setattr(context, "SHADOW", "/bin/sh" if installed else "/nonexistent")
    assert text in context.refusal("shell", where)
