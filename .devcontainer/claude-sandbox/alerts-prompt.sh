# claude-sandbox: warn at the prompt of every outer shell when the PATH
# watcher has quarantined something a session left behind (ADR 27).
#
# Installed root-owned as /etc/profile.d/claude-sandbox-alerts.sh by the
# opt-in Python shadow, and sourced from /etc/bash.bashrc and /etc/zsh/zshrc.
# Acts only in interactive bash and zsh; plain sh parses it and does nothing,
# so nothing below may be bash- or zsh-only syntax outside an eval. With no
# alerts it costs two `test -s` builtins per prompt.
if [ -n "${BASH_VERSION:-}${ZSH_VERSION:-}" ] && [ -z "${__cs_alerts_loaded:-}" ]; then
    case $- in
        *i*)
            __cs_alerts_loaded=1
            __cs_alerts_seen=0
            __cs_alerts() {
                if [ ! -s /run/claude-sandbox/alerts ] && [ ! -s /tmp/claude-sandbox/alerts ]; then
                    __cs_alerts_seen=0
                    return 0
                fi
                # Only what this shell has not shown yet; all of it again if
                # the list was cleared and refilled since.
                __cs_n=0
                __cs_all=""
                __cs_new=""
                for __cs_f in /run/claude-sandbox/alerts /tmp/claude-sandbox/alerts; do
                    [ -r "$__cs_f" ] || continue
                    while IFS= read -r __cs_line || [ -n "$__cs_line" ]; do
                        __cs_n=$((__cs_n + 1))
                        __cs_all="$__cs_all  $__cs_line
"
                        if [ "$__cs_n" -gt "$__cs_alerts_seen" ]; then
                            __cs_new="$__cs_new  $__cs_line
"
                        fi
                    done < "$__cs_f"
                done
                if [ "$__cs_n" -lt "$__cs_alerts_seen" ]; then
                    __cs_new=$__cs_all
                fi
                if [ -n "$__cs_new" ]; then
                    printf '\033[1;31mclaude-sandbox: quarantined what a sandboxed session left:\033[0m\n%s' "$__cs_new" >&2
                    printf '\033[1;31mReview the session that created it; `claude-sandbox alerts --clear` once done.\033[0m\n' >&2
                fi
                __cs_alerts_seen=$__cs_n
                unset __cs_n __cs_all __cs_new __cs_f __cs_line
            }
            if [ -n "${ZSH_VERSION:-}" ]; then
                eval 'precmd_functions+=(__cs_alerts)'
            else
                PROMPT_COMMAND="__cs_alerts${PROMPT_COMMAND:+;$PROMPT_COMMAND}"
            fi
            ;;
    esac
fi
