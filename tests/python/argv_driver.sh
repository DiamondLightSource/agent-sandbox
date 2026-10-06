#!/usr/bin/env bash
# Comparison-harness driver (ADR 26, issue #72 phase 1). Sources the bash
# shadow in source-only mode, runs one of its functions, and prints the
# result NUL-separated for tests/python/test_parity.py to compare with the
# Python port. Runs in the environment the test gives it: everything the
# shadow reads comes from that environment or from the arguments below.
#
#   argv_driver.sh SHADOW argv AGENT SKILLS_DIR CONF VERIFY WORKSPACE REAL [ARG...]
#       bwrap_argv_build's argv for profile AGENT, with SHIPPED_SKILLS_DIR set
#       to SKILLS_DIR, CONF (if non-empty) applied by parse_config first, and
#       SANDBOX_VERIFY=VERIFY.
#   argv_driver.sh SHADOW config CONF PWD
#       parse_config CONF, then every knob it set and what the port, jail
#       and workspace helpers make of them.
#   argv_driver.sh SHADOW call FUNCTION [ARG...]
#       FUNCTION's own stdout, stderr and exit status.
#   argv_driver.sh SHADOW gitconfig OUT
#       render_gitconfig into OUT.
#
# Only bash builtins here: a scenario may set PATH to something useless.
set -euo pipefail

_drv_shadow="$1" _drv_mode="$2"
shift 2

# Sourcing re-exports CLAUDE_SANDBOX_GITCONFIG_PATH to the shadow's constant;
# keep a scenario's own value, as bwrap_argv.sh does by setting it per call.
_drv_gitconfig="${CLAUDE_SANDBOX_GITCONFIG_PATH-}"
# The shadow reads these globals; shellcheck cannot see into a sourced
# file named at run time.
# shellcheck disable=SC2034
CLAUDE_SHADOW_SOURCE_ONLY=1
# shellcheck source=/dev/null
source "$_drv_shadow"
unset CLAUDE_SHADOW_SOURCE_ONLY
if [ -n "$_drv_gitconfig" ]; then
    CLAUDE_SANDBOX_GITCONFIG_PATH="$_drv_gitconfig"
fi

_drv_flag() { if "$@"; then printf 1; else printf 0; fi; }

case "$_drv_mode" in
    argv)
        agent_profile "$1"
        # shellcheck disable=SC2034
        SHIPPED_SKILLS_DIR="$2"
        if [ -n "$3" ]; then parse_config "$3"; fi
        # shellcheck disable=SC2034
        SANDBOX_VERIFY="$4"
        shift 4
        _drv_argv=()
        bwrap_argv_build _drv_argv "$@"
        printf '%s\0' "${_drv_argv[@]}"
        ;;
    config)
        parse_config "$1"
        for _drv_var in CLAUDE_SANDBOX_WORKSPACE_ROOT CLAUDE_SANDBOX_NO_FORGE \
            CLAUDE_SANDBOX_EGRESS_JAIL CLAUDE_SANDBOX_LOCAL_MODEL_PORT \
            CLAUDE_SANDBOX_LOCAL_PORTS CLAUDE_SANDBOX_CALLBACK_PORTS \
            CLAUDE_SANDBOX_GPU CLAUDE_SANDBOX_ALLOW_DEVICES \
            CLAUDE_SANDBOX_ALLOW_WRITE CLAUDE_SANDBOX_ALLOW_IP \
            CLAUDE_SANDBOX_PASS_ENV; do
            if [ -n "${!_drv_var+set}" ]; then
                printf 'env %s=%s\0' "$_drv_var" "${!_drv_var}"
            fi
        done
        printf 'workspace_root=%s\0' "$(resolve_workspace_root "$2")"
        printf 'egress_jail_enabled=%s\0' "$(_drv_flag egress_jail_enabled)"
        printf 'local_ports=%s\0' "$(local_ports)"
        printf 'local_model_enabled=%s\0' "$(_drv_flag local_model_enabled)"
        printf 'callback_ports=%s\0' "$(callback_ports)"
        printf 'callback_enabled=%s\0' "$(_drv_flag callback_enabled)"
        _drv_rc=0
        _drv_err="$(validate_local_model_port 2>&1)" || _drv_rc=$?
        printf 'validate_local_model_port=%s %s\0' "$_drv_rc" "$_drv_err"
        _drv_rc=0
        _drv_err="$(validate_callback_ports 2>&1)" || _drv_rc=$?
        printf 'validate_callback_ports=%s %s\0' "$_drv_rc" "$_drv_err"
        ;;
    call)
        "$@"
        ;;
    gitconfig)
        CLAUDE_SANDBOX_GITCONFIG_PATH="$1"
        render_gitconfig
        ;;
    *)
        echo "argv_driver.sh: unknown mode '$_drv_mode'" >&2
        exit 2
        ;;
esac
