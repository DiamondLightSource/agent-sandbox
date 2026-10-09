#!/usr/bin/env bash
# Print a Claude Code transcript (.jsonl) as plain conversation text: user
# prompts, Claude's replies, and one line per tool call. Tool output and
# thinking are dropped, which is most of the bulk — this is what a haiku agent
# reads to answer a question about a crashed or cleared session, instead of
# anyone running /resume on it.
#
#     transcript-text.sh <transcript.jsonl> [--since <ISO 8601>] [--tail <N lines>]

set -uo pipefail
f="${1:?usage: transcript-text.sh <transcript.jsonl> [--since ISO] [--tail N]}"
shift
since=""
tail_n=""
while [ $# -gt 0 ]; do
    case "$1" in
        --since) since="$2"; shift 2 ;;
        --tail)  tail_n="$2"; shift 2 ;;
        *) echo "unknown argument: $1" >&2; exit 2 ;;
    esac
done

jq -r --arg since "$since" '
    select(.timestamp != null and .timestamp >= $since)
    | (.timestamp[0:19] + "Z") as $t
    | if .type == "user" and (.isMeta | not) then
          (.message.content
           | if type == "string" then .
             else ([.[] | select(.type == "text") | .text] | join("\n")) end) as $txt
          | select($txt != "")
          | "[\($t)] USER: \($txt)"
      elif .type == "assistant" then
          .message.content[]?
          | if .type == "text" then "[\($t)] CLAUDE: \(.text)"
            elif .type == "tool_use" then
                "[\($t)] TOOL \(.name): \(.input.description // .input.file_path // .input.command // .input.prompt // "" | tostring | gsub("\n"; " ") | .[0:160])"
            else empty end
      else empty end
' "$f" 2>/dev/null | { if [ -n "$tail_n" ]; then tail -n "$tail_n"; else cat; fi; }
