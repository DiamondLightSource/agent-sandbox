"""tests/launcher.sh and tests/doctor.sh, unchanged, against the Python CLI.

Each suite drives a bash file by path; here that file is a two-line
wrapper that runs ``python -I -m claude_sandbox`` (the suites run their
target under ``env -i``, so the wrapper carries what it needs). The
launcher suite reads the launcher's version from a ``VERSION=`` line, so
the wrapper has one, holding the release tag the Python reports.
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import claude_sandbox

TESTS = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    ("suite", "variable", "seam"),
    [
        ("launcher.sh", "CLAUDE_SANDBOX_TEST_LAUNCHER", ""),
        # doctor runs inside the container; this host stands in for it.
        ("doctor.sh", "CLAUDE_SANDBOX_TEST_CLI", "CLAUDE_SANDBOX_CONTEXT=container "),
    ],
)
def test_bash_suite_passes_against_python(
    tmp_path: Path, suite: str, variable: str, seam: str
) -> None:
    for tool in ("jq", "script", "cksum"):
        if shutil.which(tool) is None:
            pytest.skip(f"{suite} needs {tool}")
    wrapper = tmp_path / "claude-sandbox"
    wrapper.write_text(
        "#!/bin/bash\n"
        f'VERSION="{claude_sandbox.release_tag()}"\n'
        f'{seam}exec {sys.executable} -I -m claude_sandbox "$@"\n'
    )
    # The launcher's own seam: run as on a host even inside a container.
    env = os.environ | {variable: str(wrapper), "CLAUDE_SANDBOX_NESTED": "1"}
    env.pop("CLAUDE_SANDBOX_LAUNCHER_VERSION", None)
    done = subprocess.run(
        ["bash", str(TESTS / suite)], env=env, capture_output=True, text=True
    )
    assert done.returncode == 0, done.stdout + done.stderr
    assert " 0 failed" in done.stdout or "/ 0 failed" in done.stdout
