#!/usr/bin/env bash
# Installer comparison driver (ADR 26, issue #72 phase 4). Sources the bash
# installer without running it and runs one of its steps, for
# tests/python/test_installer_parity.py to compare with the Python port.
# Everything the step reads comes from the environment the test gives it
# (INSTALL_PREFIX, INSTALL_USER_HOME, HOME, CLAUDE_SHARED_CONFIG, ...) and
# from the tree INSTALL_SH sits in.
#
#   install_driver.sh INSTALL_SH STEP
#       STEP is an install.sh function, `shadow` for main()'s own
#       install_file calls (every other step stubbed out), or `main`.
set -euo pipefail

_drv_install="$1" _drv_step="$2"

# shellcheck source=/dev/null
source "$_drv_install"

case "$_drv_step" in
    main)
        main >/dev/null ;;
    shadow)
        for _drv_fn in probe_or_refuse apt_install probe_userns_or_refuse \
                link_terminal_config install_claude_binary install_codex_binary \
                install_pi_binary ensure_cred_dirs install_conf stamp_version \
                stamp_installer install_runtime_scripts install_shipped_skills \
                wire_managed_settings wire_codex_managed wire_user_statusline; do
            eval "$_drv_fn() { :; }"
        done
        # The summary main() prints reads files the stubs did not write.
        main >/dev/null 2>&1 ;;
    *)
        "$_drv_step" ;;
esac
