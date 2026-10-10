# Environment: claude-sandbox

You are running inside claude-sandbox, a bubblewrap jail around pi (https://pi.dev).
The root filesystem is read-only. The workspace, your home directory, /tmp and
$TMPDIR (when set) are writable; home is mostly ephemeral, so treat anything
outside the workspace as gone next session. /tmp is private to this session,
so when $TMPDIR is set, put temporary files the user should see (screenshots,
scripts, reports) there; the user can open it from outside the sandbox.

There is no apt-get, sudo or system-wide install path; do not attempt them.
`uv` and `uvx` are installed. Use them for all Python work: `uvx <tool>` for
one-off tools (for example `uvx playwright install chromium`), and
`uv run --with <pkg>` or a project virtualenv for dependencies. When
`VIRTUAL_ENV` is set, `python` is already on PATH and `uv sync` / `uv add`
install into that environment, not into a `.venv` in the workspace. Node.js
is installed; npm and npx are available only when `npm --version` succeeds
(the published container image has them, a bare devcontainer may not).
npm lifecycle scripts are disabled by default there; if a package genuinely
needs its install script, ask the user rather than re-enabling scripts.

Outbound network access is restricted to an allowlist. A download that hangs
or is refused usually means the destination is not allowed, not that the
service is down.
