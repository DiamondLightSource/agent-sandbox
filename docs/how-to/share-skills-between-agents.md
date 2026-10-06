# Share skills between agents

Put reusable skills in `~/.agents/skills`. Claude, Codex and Pi can all read
and write this directory; keep agent-specific skills in their own directories.

| Directory | Seen by |
|---|---|
| `~/.claude/skills/` | Claude |
| `~/.codex/skills/` | Codex |
| `~/.pi/agent/skills/` | Pi |
| `~/.agents/skills/` | Codex and Pi automatically; Claude when linked |

## Add a shared skill

Put the skill in the shared directory, one level deep:

```bash
mkdir -p ~/.agents/skills/my-skill
$EDITOR ~/.agents/skills/my-skill/SKILL.md
```

Codex and Pi load it in their next session. The files are also visible to
other sessions sharing the same terminal config.

## Give Claude a shared skill

Claude does not read `~/.agents/skills`. Link each skill you want it to
have:

```bash
mkdir -p ~/.claude/skills
ln -s ../../.agents/skills/my-skill ~/.claude/skills/my-skill
```

Always link **from** an agent's directory **to** `~/.agents/skills`. A link
from `~/.agents/skills` into, say, `~/.claude/skills` is dangling in Codex
and Pi sessions, because those sessions cannot see Claude's config.

## Persistence

When your devcontainer mounts `/user-terminal-config`, the installer makes
`~/.agents/skills` a link into it, so shared skills survive rebuilds and
follow you into other devcontainers. Without that mount they last as long
as the container.

## What to watch

Any agent can write to `~/.agents/skills`, and every other agent may then
load what it wrote. Review skills there as you would your own `~/.claude`,
particularly after a session you do not trust. See
{ref}`ADR 25 <adr-shared-agent-skills>`.

Shipped skills (such as `vscode-headless`) are not in this directory. They
are read-only and appear in each agent's own skills directory.
