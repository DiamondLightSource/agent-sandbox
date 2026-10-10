"""Run ``shadow.main`` in a real process against fixture paths.

For the tests that need what an in-process call cannot give: a controlling
terminal, a signal that kills, a real exec of script(1). Run as
``python -I shadow_driver.py SPEC_JSON AGENT [ARG...]``; SPEC names the
package source, the fixture paths (a fake bwrap in ``tools``) and,
optionally, a signal to raise while the git config's temporary file exists.
"""

import json
import os
import sys
from dataclasses import replace
from typing import Any, cast

spec = cast(dict[str, Any], json.loads(sys.argv[1]))
sys.path.insert(0, cast(str, spec["src"]))

from claude_sandbox import shadow  # noqa: E402
from claude_sandbox.profiles import PROFILES  # noqa: E402
from claude_sandbox.tools import TOOL_PATH, find_tool  # noqa: E402

real = cast(str, spec["real"])
host = shadow.Host(
    config_path=cast(str, spec["conf"]),
    gitconfig_path=cast(str, spec["gitconfig"]),
    shipped_skills_dir=cast(str, spec["skills"]),
    # No temp root: a test must never create the real /var/tmp/<agent>-agent.
    profiles={name: replace(p, real=real, tmpdir="") for name, p in PROFILES.items()},
    # The fixture's fake bwrap (and a git that knows no identity) first, then
    # the real script(1).
    find_tool=lambda name: find_tool(
        name, search=(cast(str, spec["tools"]), *TOOL_PATH)
    ),
)

signum = cast(int, spec.get("signal", 0))
if signum:
    replace_file = os.replace

    def interrupted(src: str, dst: str) -> None:
        os.kill(os.getpid(), signum)
        replace_file(src, dst)

    os.replace = interrupted

shadow.main(sys.argv[2], sys.argv[3:], host)
