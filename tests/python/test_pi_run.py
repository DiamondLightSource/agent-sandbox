"""pi-run, the root-owned launch guard Pi is exec'd through (bash, ADR 26)."""

import os
import subprocess
from pathlib import Path

import pytest

PI_RUN = Path(__file__).resolve().parents[2] / ".devcontainer/claude-sandbox/pi-run"


@pytest.mark.parametrize(
    "env",
    [{}, {"IS_SANDBOX": "1", "IS_SANDBOX_AGENT": "claude"}],
    ids=["unwrapped", "another-agents-session"],
)
def test_pi_run_refuses_outside_a_pi_session(env: dict[str, str]) -> None:
    done = subprocess.run(
        ["/bin/bash", str(PI_RUN)],
        env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), **env},
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 2
    assert "Pi must be started through the sandboxed pi command" in done.stderr
