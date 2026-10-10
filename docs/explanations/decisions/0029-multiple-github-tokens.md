(adr-multiple-github-tokens)=

# 29. Choose among several GitHub tokens by attempt and cache

Date: 2026-10-10

## Status

Accepted. Extends {ref}`ADR 6 <adr-container-scoped-credentials>` and
{ref}`ADR 7 <adr-curated-gitconfig>`.

## Context

A fine-grained PAT has one resource owner, and gh keeps one token per account
per host. One account that must push to repositories in two organisations
cannot hold both tokens in gh; a second `gh auth login` replaces the first.
Users also want several narrow tokens rather than one broad one (issue #47).

Two ways to pick the token for a repository were considered and rejected:

- **Route by URL prefix in the git config**, one `[credential
  "https://github.com/ORG"]` section per token ahead of the host-wide one.
  Git asks matching helpers in config order, takes the first answer and
  never retries a rejected push. Prefix routing therefore needs each
  repository to map to exactly one token chosen in advance; it cannot say
  "whichever of these works", and a section answering first with an
  under-scoped token fails the push.
- **Ask GitHub what a token may do.** No endpoint lists a fine-grained
  token's repositories or permissions. `GET /repos/{owner}/{repo}` reports
  the *account's* `permissions`, the same for every token the account holds,
  and `x-oauth-scopes` is empty for fine-grained tokens. Tested with three
  tokens of different scope: all reported `admin: true` and all could read
  every repository, while their push and PR rights differed.

The only reliable signal is attempting the write.

## Decision

- `claude-sandbox gh-auth --add NAME` stores a further token at
  `~/.config/gh/scoped/NAME.token`, mode 0600, in the container-scoped gh
  directory (ADR 6 unchanged: same lifetime, same blast radius, never
  `/cache`). Plain `gh-auth` still fills gh's own login.
- The sandbox's git config names one GitHub credential helper, the
  sandbox's own (`python -I -m claude_sandbox _git_credential` from the
  root-owned interpreter), with `useHttpPath = true` so git passes the
  repository path. On a repository it has no entry for, it tries the tokens
  in order, gh's own first, then the named ones by name. Each try is an
  authenticated `GET https://github.com/OWNER/REPO.git/info/refs?service=git-receive-pack`,
  the first request of a push, which transfers nothing. The first token that
  gets a 200 is cached for the repository and returned. A default login with
  read-only Contents, as `gh-auth` suggests, fails this check, so the named
  tokens decide pushes and the default serves reads. When every token is
  refused, gh's own token is returned so fetches still work, and that
  outcome is cached as a fallback. When GitHub does not answer, nothing is
  cached. A redirect to another host is not followed, so a token is sent to
  github.com only.
- With no named token stored the helper hands every request to
  `gh auth git-credential` unchanged and probes nothing.
- The cache is `~/.config/gh/scoped/state.json`, beside the tokens.
  `gh-auth --status` lists the tokens, any expiry GitHub reported when the
  token was added, and the repositories each serves. `gh-auth --forget
  [OWNER/REPO]` clears entries; git's `erase` (a 401) clears that
  repository's entry; adding a token clears the fallbacks so they are tried
  again.
- In the jail, `gh` is a root-owned shim, `/usr/libexec/claude-sandbox/bin/gh`,
  put first on the jail's `PATH` by `bwrap.py`. It finds the repository as gh
  would (`-R`/`--repo`, `GH_REPO`, else the remote marked `gh-resolved =
  base`, else `upstream`, `github`, `origin`). On a repository with no cache
  entry it probes and caches exactly as the git helper does, so the upstream
  of a fork gets its token on the first `gh pr create`. When a named token is
  chosen it sets `GH_TOKEN` in gh's own environment only, then execs the
  real gh, so `gh auth status` reports that token. `gh auth login`,
  `logout`, `refresh` and `switch` (which gh refuses while `GH_TOKEN` is
  set), a caller's own `GH_TOKEN` or
  `GITHUB_TOKEN`, and another `GH_HOST` are left alone. The session's
  environment never holds a GitHub token, so battery check 04 still holds.
- `no-forge` leaves the shim off `PATH` and, as before, omits the gh
  directory and the credential helpers, so named tokens are suppressed too.

## Consequences

- One container can push to several organisations, and narrow same-organisation
  tokens coexist; no token needs wiring to a repository by hand.
- The first push to a repository costs one request per token tried, then
  none. A wrong entry is visible in `--status` and cleared with `--forget`.
- That `info/refs?service=git-receive-pack` answers 200 exactly when a
  fine-grained token may push is observed GitHub behaviour, not a documented
  contract: tested with real tokens (2026-10-10), a Contents read/write token
  got 200 and a Contents read-only token on the same repository got 403.
- The probe measures push rights, not pull-request or issue rights: gh uses
  the first token that may push, and a gh write needing a permission that
  token lacks is not retried with another. Repositories git has
  only fetched are probed once by gh too.
- With GitHub unreachable, a first push or gh command on a repository waits
  up to 10 seconds per token before falling back to gh's own login.
- The named tokens are used inside agent sessions only. A container shell
  outside the agent, and the published image's outer shell
  (`container/git-config.sh`), keep gh's own helper and login.
- Every github.com credential request now starts the root-owned Python
  interpreter, and every `gh` in the jail starts it and one `git config`.
