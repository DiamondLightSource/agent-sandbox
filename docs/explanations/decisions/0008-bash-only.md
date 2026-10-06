(adr-bash-only)=

# 8. Bash-only: no Python package, uv, or pytest

Date: 2026-05-12

## Status

Superseded by {ref}`adr-python-implementation` (ADR 26): the premise of two
short bash files no longer held once the implementation reached about 3,900
lines, and the sandbox is being reimplemented as a Python package that execs
bubblewrap. Until that migration finishes (issue #72), the bash described here
remains the shipped default.

## Context

The tool's implementation drifted: `embedded bash → standalone bash → Python
package + typer CLI → bash-only` (commits `25e67ce`, `a35b8ee`, then `bf65407`;
issue #14 / PR #15). The Python era was roughly 110 KB — `pyproject.toml`, a
`uv` lockfile, a pytest suite, a typer CLI. But the tool is fundamentally *one
bash function building a bwrap argv*, and spreading the security-critical bits
across several Python modules made them **harder** to audit, not easier.

## Decision

Bash-only. The security surface is two short bash files — the shadow and the
installer — that you can read top to bottom. No `pyproject.toml`, `uv.lock`,
`src/claude_sandbox/`, or `test_*.py`. The one allowed Python is the fully
isolated `docs/` toolchain (Sphinx), which touches nothing security-critical.

## Consequences

- The security surface is a couple of files you can read top-to-bottom — the
  same auditability principle that motivated {ref}`adr-standalone-repo`.
- Tests are bash: `tests/bwrap_argv.sh` (pure argv-builder assertions),
  `tests/smoke.sh`, `tests/promote.sh`.
- Root `CLAUDE.md` states the rule ("Bash-only. No Python package, no uv, no
  pytest — don't add them back"); the `claude-sandbox` skill (Reversal 1) lists
  the regressions to refuse ("a small Python CLI for nicer errors," "bring back
  pytest — it's only a little code").
- The v2 "ship as a PyPI package / `uvx` one-liner" idea (issue #26) reopens
  this and must re-justify against auditability before proceeding.
