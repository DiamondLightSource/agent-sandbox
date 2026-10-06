#!/usr/bin/env bash
# Launch-body driver for the comparison harness (ADR 26, issue #72 phase 2a).
# Installs a copy of the bash shadow under FIXTURE the way tests/verify.sh
# does: the installed locations it hard-codes are rewritten under FIXTURE and
# nothing else changes. Then runs it as FIXTURE/bin/ARGV0, in the caller's
# environment and working directory, so the whole launch body runs: config,
# git identity, directory creation, warnings and the final exec, which the
# fixture's own `script` (first on PATH) records.
#
#   launch_driver.sh SHADOW FIXTURE ARGV0 [ARG...]
set -euo pipefail
shadow="$1" fixture="$2" argv0="$3"
shift 3
mkdir -p "$fixture/bin"
sed -e "s|/usr/libexec/claude-sandbox|$fixture/libexec|g" \
    -e "s|/etc/claude-gitconfig|$fixture/etc/claude-gitconfig|g" \
    -e "s|/etc/claude-sandbox.conf|$fixture/etc/claude-sandbox.conf|g" \
    "$shadow" > "$fixture/bin/claude-shadow"
chmod +x "$fixture/bin/claude-shadow"
ln -sf claude-shadow "$fixture/bin/$argv0"
exec "$fixture/bin/$argv0" "$@"
