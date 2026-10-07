# claude-sandbox

A Python package (`src/claude_sandbox/`, root `pyproject.toml`) per ADR 26
(`docs/explanations/decisions/0026-python-implementation.md`, supersedes
ADR 8; implemented in 5.0.0, issue #72). The bash shadow, CLI, launcher and
installer steps are gone. What stays bash, by ADR 26: the installer's
bootstrap (`.devcontainer/claude-sandbox/install.sh`, which fetches uv and
provisions the interpreter) and the `install` shim, the three-line shims
(`claude-shim`, `claude-sandbox-shim`), the integrity battery
(`verify-sandbox-battery.sh`), the in-jail wrappers (`codex-launch`,
`pi-run`), `container/entrypoint.sh`, and the end-to-end suites in
`tests/*.sh`. Don't port those to Python, and don't grow sandbox logic in
them.

Development: uv, pytest, ruff, pyright, and a committed dev lockfile
(`uv.lock`). Version comes from the git tag (hatch-vcs).

```bash
uv sync                      # dev environment from uv.lock
uv run pytest --cov          # tests/python/, >=95% branch coverage (e2e suites: tests/*.sh)
uv run ruff check
uv run ruff format --check
uv run pyright               # strict, src/ and tests/python/: fix code, don't relax
uv build --wheel             # the PyPI wheel
```

**Refuse** (the Python guardrails; the why is in the `claude-sandbox` skill,
Reversal 1):

- **No runtime dependencies; the package is stdlib-only.** No third-party
  import anywhere in `src/claude_sandbox/` (the CLI is argparse, not Typer;
  ADR 26 as amended). `__main__.py` still dispatches `_shadow` before the
  CLI is imported, and `tests/python/test_wheel.py` imports every module.
- **The jail's interpreter is fixed.** Anything that runs in or launches
  the jail (the shadow shim and everything it execs) uses the root-owned
  interpreter under `/usr/libexec/claude-sandbox/`, by absolute path, with
  `-I`, never from `~/.cache` (uv's cache, writable from the jail). No
  `#!/usr/bin/env python3`, no interpreter found through `PATH`. The
  host-side `uvx claude-sandbox` launcher/installer (ADR 23) is outside
  this rule.
- **No bind or environment added to the bwrap argv outside `bwrap.py`.**
- **Keep the audit core small** — `bwrap.py`, `jail.py`, `shadow.py` and
  `watch.py` (the PATH watcher, ADR 27), each readable top to bottom. Don't spread the core across many modules: that
  is the `bf65407` / issue #14 failure that ADR 8 reversed.

The documentation toolchain stays isolated to `docs/`:
`docs/requirements.txt` (Sphinx + MyST + pydata theme + mermaid) is its
pinned source of truth and CI installs it with `pip`. Don't merge
docs-toolchain dependencies into the package's `pyproject.toml` or
`uv.lock`, and add no docs command to the shipped `claude-sandbox` CLI.
Contributors may run it with `uvx --with-requirements docs/requirements.txt ...`.

- Docs (Diátaxis, Sphinx): `docs/` → published to GitHub Pages by
  `.github/workflows/docs.yml`. Build locally: `python -m venv .venv-docs
  && .venv-docs/bin/pip install -r docs/requirements.txt && .venv-docs/bin/sphinx-build -b html docs build/html`
- **Never merge a docs layout/CSS change until the user has verified the
  rendering locally in a real browser.** The user verifies via autobuild
  (`sphinx-autobuild`, see docs/how-to/contribute.md) at multiple widths; the sandbox has no
  browser and WeasyPrint is not a faithful proxy for Chromium auto-layout.
  Open the PR if asked, but wait for the user's explicit OK before merging —
  don't merge on a green build alone.
- Threat model + sandbox model: https://diamondlightsource.github.io/claude-sandbox/explanations/threat-model.html
- Sandbox-integrity spec: `skills/verify-sandbox/SKILL.md`
- Network egress jail (lateral-movement isolation, ADR 0015):
  `docs/explanations/decisions/0015-network-egress-jail.md`; operational skill:
  `.claude/skills/claude-sandbox-networking/SKILL.md`
