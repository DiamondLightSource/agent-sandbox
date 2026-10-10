# Authenticate with forges

Give the agent a project-scoped token when it needs to push or use forge APIs.

## Authenticate

From your project on the host, or a devcontainer terminal outside the agent:

```bash
claude-sandbox gh-auth
claude-sandbox glab-auth gitlab.example.com
```

The helpers prompt for a token without placing it in shell history.
The host launcher runs them inside the project's container. Inside an agent
session they refuse, because the agent shares that terminal.

:::{note} DLS: Diamond GitLab
Use `claude-sandbox glab-auth` with no hostname for `gitlab.diamond.ac.uk`.
The shipped network config already allows its IP. Other internal forges need
an [allow-ip entry](network-egress-jail.md#keep-a-lab-device-or-internal-forge-reachable).
:::

The agent can read the resulting token store. Tokens stay in the project
container and must be entered again after recreation.

## Use several GitHub tokens

A fine-grained PAT has one resource owner, and gh keeps one login per
account. To push to repositories in more than one organisation, or to keep
several narrow tokens instead of one broad one, store the extra tokens by
name beside gh's own login:

```bash
claude-sandbox gh-auth                 # gh's own login, as before
claude-sandbox gh-auth --add acme      # a further token, named acme
claude-sandbox gh-auth --add beta-ro --add beta-rw   # several at once
```

Each named token is kept at `~/.config/gh/scoped/NAME.token` (mode 0600),
in the same container-scoped directory as gh's login, so it has the same
lifetime and the agent can read it. A name uses letters, digits, `_`, `.`
and `-`.

You do not say which token belongs to which repository. On the first push
to a repository, git's credential helper asks GitHub, token by token (gh's
own first, then the named ones alphabetically), whether that token may push
there, and remembers the first that may. Nothing is transferred while it
asks. gh's own login set up with the suggested read-only Contents
permission cannot push, so it fails this check and the named tokens are
tried next. When none may push, it answers with gh's own token, so fetches work
as before. If GitHub cannot be reached, the first push to a repository can
wait about 10 seconds per token before falling back.

Inside the agent session, `gh` uses the same choice for the repository it
is working on: the one named by `-R`/`--repo` or `GH_REPO`, else the
checkout's remote marked `gh-resolved = base`, else its `upstream`,
`github` or `origin` remote, in that order. A repository with no choice yet,
such as the upstream of a fork you push to, is asked about in the same way
when `gh` first works on it. The question is whether the token may push;
GitHub cannot be asked whether it may open pull requests, so a token with
push rights but no Pull requests permission is still the one chosen.

With no named token stored, nothing changes: git and gh use gh's own login
and nothing is probed.

To see the tokens, their expiry (when GitHub reported one) and which
repositories each is used for, and to make the sandbox ask again:

```bash
claude-sandbox gh-auth --status
claude-sandbox gh-auth --forget OWNER/REPO   # or no argument, to forget all
```

Storing a new token also forgets the repositories no token could push to.
To remove a token, delete its file under `~/.config/gh/scoped/` from a
container shell. The choice applies inside agent sessions; a container
shell outside the agent keeps using gh's own login. Why it works this way
is in
[ADR 0029](../explanations/decisions/0029-multiple-github-tokens.md).

## Choose permissions

Restrict access to the project and use a short expiry, such as 7–30 days.

For GitHub, select only the required repository. Pushing needs **Contents:
Read and write**; add Issues or Pull requests write permission only for those
tasks. The helper currently suggests read-only Contents, which does not permit
push. Avoid workflow or administrative permissions unless required. See
[GitHub's permission reference](https://docs.github.com/en/rest/authentication/permissions-required-for-fine-grained-personal-access-tokens).

For GitLab, prefer a project access token. Git-over-HTTPS push uses
`write_repository`; broader API operations may require `api`.
The helper's prompt recommends broader scopes; grant only what the intended
workflow needs. See [GitLab's token scopes](https://docs.gitlab.com/security/tokens/access_token_scopes/).
Project tokens can also read Internal-visibility projects in some circumstances;
see [GitLab's project-token documentation](https://docs.gitlab.com/user/project/settings/project_access_tokens/).

The helpers do not enforce token permissions.

## Run without push access

When the agent does not need forge access, omit the token stores and
credential helpers, named GitHub tokens included. With the PyPI launcher,
add this to the [host config](use-the-container-image.md#configure-the-sandbox):

```ini
no-forge
```

Or set the flag when creating the project container:

```bash
CLAUDE_SANDBOX_NO_FORGE=1 claude-sandbox
```

For an existing container, add `--recreate` to apply a changed environment
variable. Recreation removes container-local packages and forge logins.

In your own devcontainer, set `CLAUDE_SANDBOX_NO_FORGE=1` in the launching
terminal or `remoteEnv`, or add `no-forge` to `/etc/claude-sandbox.conf`.

This removes the sandbox's supplied credentials; it cannot prevent pushing
with another token placed in the workspace or explicitly given to the agent.
