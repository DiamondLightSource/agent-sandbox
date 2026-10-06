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
credential helpers. With the PyPI launcher, add this to the
[host config](use-the-container-image.md#configure-the-sandbox):

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
