---
name: claude-sandbox-container
description: Design decisions for the published container image `ghcr.io/diamondlightsource/claude-sandbox` and its host-side launcher. Covers the image build running the same installer (never a parallel install path), entrypoint re-runs of build-time skips, named-container PAT scoping, ro-mounted conf, tag-vs-latest publishing, notify-only launcher versioning (refuse --self-update), and parked issues #79/#80/#81. Surface before edits to the root Dockerfile, container/entrypoint.sh, src/claude_sandbox/host/ (the launcher), or .github/workflows/container.yml. Core shadow/installer invariants live in the claude-sandbox skill.
---

# claude-sandbox-container

The published container image is the **third consumer** of the sandbox
(after the dogfood devcontainer and guest clone+install — see "dogfood ≈
guest" in the `claude-sandbox` skill, whose Invariants 2 and 4 are
mapped onto the image below). Split from that skill so this loads only
on image/launcher topics.

## The image (PR #78)

`ghcr.io/diamondlightsource/claude-sandbox` (built by `.github/workflows/container.yml`
from the root Dockerfile's `claude-sandbox` stage, `FROM` the `developer`
stage) gives non-devcontainer hosts sandboxed Claude via rootless podman + the
`uvx claude-sandbox` host launcher (`src/claude_sandbox/host/`; the bash
`container/claude-container` it replaced is gone since 5.0). Principles
already extended here:

- **Dogfood ≈ guest ≈ image**: the image build runs the same
  `.devcontainer/claude-sandbox/install.sh --image-build` (the bootstrap)
  and so the same Python installer, in the same step order — never a
  parallel install path. Two deliberate build-time skips (both re-run by
  `container/entrypoint.sh` at start, through the installer's own
  `--probe-userns` and `--container-start`, from the root-owned venv with
  `-I`): the userns probe (`system.probe_userns_or_refuse`; a builder probe
  proves nothing about the runtime host) and the shared-config links
  (`steps.plan_shared_links`, once `link_terminal_config`) — the DLS base ships an EMPTY
  `/user-terminal-config` stub, and wiring it at build symlinks
  `~/.claude.json` to a zero-length file the official installer rejects as
  corrupted JSON ("Unexpected EOF"). The entrypoint also seeds `{}` into a
  zero-length `~/.claude.json` (same hazard, first run with a fresh share).
- **Invariant 2 mapping**: the launcher creates one NAMED container per
  project dir so forge PATs are container-scoped without per-launch
  re-paste; `--recreate` ⇒ re-auth. Refuse a "just mount host ~/.config/gh"
  convenience swap.
- **Keeper model (launcher 0.4, 2026-09-11)**: the container's PID 1 is an
  idle bash loop (`KEEPER_CMD`), and every session is `exec -it` into it;
  the launcher `stop`s the keeper when no other session lives after its
  own (below). Why: launcher <= 0.3 baked the agent + args as the container
  command, so `start -ai` replayed them and `--agent`/args were silently
  ignored on reuse, and a `--shell` (unsandboxed bash for `gh-auth`) would
  have baked bash as every later launch. Now only the create-time options
  (`--bridge`, `--peers`, `--gpu`, `--device`, `--mount`, `--mount-rw`)
  are fixed at create, the launcher says so on reuse, and `is_keeper` refuses
  pre-0.4 containers (starting one would run its baked agent detached).
  Refuse: baking the agent back into the create command; making `--shell`
  a sandboxed session (it exists precisely to run the outside-the-jail CLI).
- **Session tracking (issue #69, 2026-10-07)**: closing the host terminal
  kills only the `exec -it` client; conmon (dockerd) holds the pty, so the
  session never sees a hangup and ran on for days with its agent, and the
  launcher, killed by the same SIGHUP, never stopped the keeper. Now every
  session runs under `host/sessions.py start ID` (the image's root-owned
  interpreter, `-I`; the launcher passes the file's source with `-c` so a
  container from an older 5.x image is tracked too), which records its PID
  and start time in `/run/claude-sandbox-sessions/ID` and execs the
  command. On exit, SIGHUP or SIGTERM the launcher (detached into its own
  session on a hangup: script(1) follows SIGHUP with SIGKILL after 2 s)
  kills its engine client and runs `sessions.py end ID`, which SIGHUPs the
  session's process tree (by ppid, so `setsid`/`nohup` children go too),
  SIGKILLs what is left after 3 s, and prints how many other recorded
  sessions are alive; the keeper is stopped at 0. `len .ExecIDs` was
  dropped: exec records outlive clients that died, so idle containers
  never read 0 (it remains only for 4.x containers, which have no
  interpreter to track with). Limits: a launcher killed with SIGKILL runs
  no cleanup; a hand-typed `podman exec` is not counted as a session;
  orphans reparented to the keeper (daemons started from the unsandboxed
  `shell` verb, or background jobs left behind after a session's root
  exits) sit outside the ppid tree and survive until the keeper stops.
  In-jail processes cannot escape that way: under bwrap `--unshare-pid`
  they reparent to the jail's own init, which is in the tree. The
  launcher installs its SIGHUP/SIGTERM handler only where the signal is
  not already ignored (nohup), arms it only once the engine client is
  bound (blocking the signals over the spawn would leak the mask into the
  client), and ignores SIGINT too while it cleans up.
  Refuse: an in-container watchdog that guesses the client is gone (the
  pty stays open, there is nothing to watch); counting processes instead
  of sessions (a leaked daemon would keep the keeper up forever).
- **Invariant 4 mapping**: durable user conf = host file ro-mounted at the
  canonical `/etc/claude-sandbox.conf`; the installer's `--container-start`
  detects the mount (`steps.is_mount`) and skips re-stamping
  (`test_installer_units.test_a_mounted_config_is_left_as_it_is`). Conf stays outside the sandbox rw set.
- A git tag publishes `ghcr.io/...:<tag>` without touching `:latest`
  (`latest` is default-branch-only) — beta images are safe to cut anytime.
- **Launcher versioning (notify-only, by design)**: on a tag build CI
  bakes the tag into the Dockerfile `ARG` → OCI label
  `io.diamondlightsource.claude-sandbox.launcher-version` (off a tag, the
  last release tag the build descends from, `git describe --tags
  --abbrev=0`); the console script passes the wheel's tag in as
  `CLAUDE_SANDBOX_LAUNCHER_VERSION`. Each run the launcher
  (`host/launcher.py` `warn_if_outdated`) compares itself against
  the LOCAL image's label (instant, offline, no container start) and,
  when outdated, prints `uvx claude-sandbox@latest` (run by uvx, which sets
  `UV`) or `pipx upgrade claude-sandbox` (a pipx or pip install), each
  with a pin to the image's version; when newer, a pull+`--recreate`
  hint. **Refuse:** a
  `--self-update` flag (the launcher runs unsandboxed on the host —
  replacing it must stay a deliberate, reviewable act), and hard-failing
  the build on an empty `LAUNCHER_VERSION` ARG (the label is advisory;
  pre-label images exist and must degrade to silence — CodeRabbit asked,
  declined on PR #78). The label key was renamed from
  `io.gilesknap.…` in the DLS-org rebrand (2026-07-24); images built
  before then carry only the old key, so a new launcher reads an empty
  label and degrades to silence — expected, not a bug.
- **PyPI front door (ADR 23, 2026-09-13)**: `uvx claude-sandbox` is the
  launcher and `uvx claude-sandbox install` the guest-devcontainer installer.
  The wheel (root `pyproject.toml` since ADR 26 replaced `packaging/pypi/`;
  hatchling, wheel-only) IS the implementation since 5.0: the console script
  (`claude_sandbox:main`) runs the Python CLI, and `install` runs the bash
  bootstrap the wheel bundles under `claude_sandbox/tree/` (`force-include`:
  the `install` shim, `install.sh`, the shims, in-jail wrappers, battery,
  conf, status line and `skills/` — what the installer reads as its
  `--source`; provisioning copies the package into the venv without it);
  the version is
  the git tag via hatch-vcs (`_dist.yml`/`_pypi.yml`/`_release.yml` copied
  from the DLS python-copier template, wired in `ci.yml`), so wheel == image
  tag (4.0.0 onward; nothing in the tree to bump). The entry point pins `CLAUDE_SANDBOX_IMAGE` to its own version and
  sets `CLAUDE_SANDBOX_LAUNCHER` (`uvx` or `pip`), which picks the outdated
  hint and the name the launcher calls itself. Verbs `claude|codex|pi|shell` replaced the bash launcher's
  `--agent` / `--shell`. There are no aliases and no refusal: an unknown
  first word ends the launcher's options, so `claude-sandbox --agent codex`
  runs claude with `--agent codex` (Claude Code has its own `--agent`
  flag, so refusing the old spelling would break it);
  **host-net is the default**, `--bridge` opts out (create-time). The
  launcher refuses inside a container (`/run/.containerenv` or
  `/.dockerenv`; `CLAUDE_SANDBOX_NESTED=1` overrides — also the test seam:
  `tests/launcher.sh` drives the CLI as a black box against a fake engine,
  run by `tests/python/test_bash_suites.py`, beside `test_host.py`); the
  entry point refuses `install` OUTSIDE one (`CLAUDE_SANDBOX_HOST_INSTALL=1`
  overrides). The installer stamps `/usr/libexec/claude-sandbox/installer`
  = `uvx` (`steps.plan_installer`) so `claude-sandbox update` points back at
  uvx instead of cloning past the pin. **Refuse:** sandbox logic in the
  front door (`src/claude_sandbox/__init__.py`) beyond the image pin, the
  environment and handing over to the CLI or the bootstrap (it lives in
  the ADR 26 modules);
  a second console script or package (reopens `--from` for `@latest`);
  an sdist (a second copy of the tree); a devcontainer *feature* as the
  guest path (considered, slow to start, and useless for the host
  launcher). PyPI trusted publishing needs the `pypi` GitHub environment
  and the project's publisher configured once, by hand.
- **Distribution/conf decisions parked as issues** (re-read before
  re-designing any of these): **#79** ship `/verify-sandbox` as a plugin
  via managed settings — docs-verified that `extraKnownMarketplaces`
  (local path) + `enabledPlugins` is Claude Code's ONLY machine-wide
  command/skill channel (no system commands dir exists; command becomes
  namespaced `/claude-sandbox:verify-sandbox`). **#80** Renovate-pinned
  Claude version (official installer takes `[stable|latest|X.Y.Z]` as
  `$1`; `downloads.claude.ai/claude-code-releases/{latest,stable}`
  return bare version strings). **#81** per-project
  `.claude-sandbox.conf` — see the Invariant 4 carve-out in the
  `claude-sandbox` skill.
- **Image-only Python (2026-09-11)**: the `claude-sandbox` stage bakes a
  uv-managed interpreter at `/opt/uv/python` and an active venv at
  `/cache/venv` (`UV_PROJECT_ENVIRONMENT`, `VIRTUAL_ENV`, `UV_CACHE_DIR`,
  `UV_TOOL_DIR` all under `/cache`, which the shipped conf already
  `allow-write`s). Why `/cache` and not the workspace: the mounted dir has
  the same path on the host, so a workspace `.venv` ping-pongs between the
  host's interpreter and the container's, and its `bin/python` symlink
  dangles on whichever side didn't build it last. Why not the installer:
  dogfood ≈ guest would then push a venv into every clone+install
  devcontainer, when the agent's Python is the guest project's business
  (the sandbox's OWN root-owned interpreter under `/usr/libexec`, ADR 26,
  is a different thing and never the agent's). **Refuse:** moving these
  steps into the installer or the `developer` stage; pointing
  `UV_PROJECT_ENVIRONMENT` back into the workspace; binding `~/.cache`
  back "so Playwright persists" (home is ephemeral on purpose — the fix
  is `pass-env`/`PLAYWRIGHT_BROWSERS_PATH` under `/cache`). The
  pass-through of `UV_PYTHON_INSTALL_DIR`/`UV_TOOL_DIR` (in `bwrap.py`) is
  what stops uv re-downloading the baked interpreter each session;
  `tests/python/test_argv.py` `test_uv_dirs_reach_the_jail` guards it.
- **Image-only Node (2026-09-11)**: node/npm/npx copied from
  `node:22-slim` into `/usr/local` in the `claude-sandbox` stage — NOT
  apt `npm` (npm 9 on EOL Node 18 + ~360 packages, and Playwright's npm
  package refuses < 20). Enables `pi install npm:...` (persists on the
  shared `~/.pi`). The apt `nodejs` 18 the installer installs
  (`installer/system.py` `APT_PACKAGES`) stays: guests may rely on it, and
  it is merely shadowed by PATH order in the image.
  Refuse: swapping `nodejs`→`npm` in `APT_PACKAGES`; moving the COPY into
  the `developer` stage.
  `/usr/local/etc/npmrc` sets `ignore-scripts=true` (image-only, ro
  in-session; overridable by ~/.npmrc so a default not a gate) — pi's own
  npm call passes no `--ignore-scripts`. Don't drop it "because package X
  needs postinstall": that is the case for asking the user.
- **Launcher verbs are forwarded (PR #40, 2026-09-14; one CLI since ADR
  26)**: `claude-sandbox` is one argparse CLI on the host, in the container
  and in the jail (`context.py`, `cli.py`). A helper declared
  `@requires(CONTAINER, ..., forward_from=HOST)` (`gh-auth`, `glab-auth`,
  `verify`, `pi-local`, `version`, `update`, ...) is exec'd inside the
  project container when typed on the host, so the same spelling works on
  either side, and no hand-kept verb list exists. Refuse: renaming the
  in-container command (docs, wheel console script and muscle memory all
  carry the name). Everything else after the launcher's options is still
  agent argv (`uvx claude-sandbox --resume` must keep working;
  `host/options.py` parses by hand for that reason).
- **Testing a branch end to end (2026-09-15, PR #49)** — the two-command
  naming is a recurring foot-gun: on the HOST, `claude-sandbox verify`
  forwards into the *published image*, so it never tests a branch. Always
  say which terminal. The branch path is: devcontainer terminal →
  `./install --here` → start a FRESH agent session from a scratch dir
  (binds are fixed at launch; an existing session never sees new ones) →
  check inside it → `claude-sandbox verify` from the same devcontainer
  terminal (`which claude-sandbox` = `/usr/local/bin/...`, `version`
  shows `-dirty`). To exercise the WHEEL path from a branch without a
  release, in another devcontainer:
  `uvx --from "git+https://github.com/DiamondLightSource/claude-sandbox@<branch>" claude-sandbox install`
  (no `#subdirectory=` since ADR 26 moved `pyproject.toml` to the repo
  root; branches older than that still need `#subdirectory=packaging/pypi`).
- **`clean [--force] [--images]` (PR #42)**: removes the launcher's
  project containers — stopped only by default, running too with
  `--force`, unused `*/diamondlightsource/claude-sandbox` image tags with
  `--images`. Match is name prefix `claude-sandbox-` AND the keeper
  command, never the name alone. Why it exists: reconnecting keeps the
  image a container was created from, so after a pull it is unclear
  which image a session runs; `--recreate` is one project at a time.
- **Devcontainer-like filesystem view (2026-09-14; parent rw 2026-09-15)**:
  the parent mount is OPT-IN with `--peers` (user decision 2026-09-16: off
  by default is more secure, because every sibling and its credentials are
  readable by agents). 4.4.0 had it on by default with `--no-peers` to skip;
  `--no-peers` is still accepted as a no-op so old commands work. On reuse
  without `--peers`, the launcher inspects the container's mounts and warns
  when a 4.4.0 container still binds the parent. Explicit `--mount` and
  `--mount-rw` still apply; existing containers need `--recreate`; the flag
  does not alter mounts in independent devcontainers. With `--peers`,
  the launcher binds the project's PARENT read-write at its host path, as
  the devcontainer's `/workspaces` mount does, and the project rw nested
  over it. First cut made the parent ro; user reversed that 2026-09-15 so
  a shell can start a new sandboxed session in any sibling — not a hazard
  because the shadow binds only `$PWD` rw per session, and the parent is
  NOT added to allow-write. `--mount` is READ-ONLY and `--mount-rw` is
  the old rw + allow-write behaviour. The parent bind is skipped when the parent is
  `/` or contains `$HOME` (a project directly under `~` would hand
  `~/.ssh` and every host token to the container, ro or not — the
  launcher says so). `shell` execs the shell the launcher was run FROM
  (`detect_shell` walks the ancestors; `$SHELL` is only the login shell —
  bash at DLS while terminals run zsh), or `CLAUDE_SANDBOX_SHELL` (per
  run, not create-time; bash fallback inside):
  the DLS base image already installs zsh + oh-my-zsh and writes
  `/root/.zshrc` to source `/user-terminal-config/zshrc`, so the
  terminal-config richness was inherited all along — only the verb
  hard-coded bash. Refuse: hard-coding zsh instead (user declined to force
  it on people). `LANG` is always
  set; `DISPLAY` + `/tmp/.X11-unix` + `~/.Xauthority` (ro) are passed
  only when the host has a DISPLAY, and only the unsandboxed shell sees
  them (the shadow masks `~/.Xauthority`; nothing in the agent path
  changed). Parent and `--mount`/`--mount-rw` binds use `bind-propagation=slave`
  (as builder2ibek's devcontainer does for `/dls_sw`): a rootless
  container may not TRIGGER an autofs mount (`ls /dls_sw/work` → EPERM)
  but host-made mounts propagate in. Refuse: plain `-v` for these
  (loses propagation); mounting the parent when it holds `$HOME`; making
  `--mount` rw again "for convenience"; forwarding DISPLAY into the
  shadow's pass-through list.
- **/cache volume + per-project venv (PR #43, 2026-09-14)**: the launcher
  mounts ONE named volume `claude-sandbox-cache` at `/cache` and sets
  `UV_PROJECT_ENVIRONMENT`/`VIRTUAL_ENV=/cache/venv-for<project path>`,
  `PRE_COMMIT_HOME`, `UV_PYTHON_CACHE_DIR` at create — the DLS
  python-copier devcontainer layout (`/workspaces/podbench/.devcontainer`).
  THE LESSON (user, after my first cut put only `/cache/uv` on a volume):
  venv and uv cache on the SAME filesystem is the point — uv hardlinks
  wheels into the venv, so installs cost no disk and little time; split
  them and you need `UV_LINK_MODE=copy` and pay a copy per install. And
  persistence of packages was never the goal: that template runs
  `uv venv --clear && uv sync` on every create, the cache makes it fast.
  Image side: PATH carries `/opt/venv/bin`, a CONTAINER-LOCAL symlink the
  entrypoint points at `$VIRTUAL_ENV` (a symlink on the shared volume
  would be shared by every project), created fresh once per container
  (`/var/lib/claude-sandbox/venv-created` marker; restarts keep it).
  `clean` ALWAYS prunes `venv-for<path>` dirs whose container name
  (recomputed from the path) no longer exists, via throwaway `run`s on
  the volume with `--entrypoint find|rm` — the image entrypoint would
  run the userns probe + venv setup first and a refusal there read as
  "0 venvs" (seen on the DLS host). `--venvs` existed for one release
  (4.1.0) and was folded in: a removed container gets a fresh venv on
  create anyway, so the flag only saved disk-cleanup you always want. Refuse: a volume on `/cache/uv` alone; `UV_LINK_MODE=copy`;
  a shared symlink inside the volume; `uv sync` in the entrypoint (runs
  before the session with no feedback — the agent or user syncs).
- **Walked back — EPICS client tools in the image (PR #43, 2026-09-14)**:
  built and green (caget & co. + pvxs `pvx*` copied from
  `ghcr.io/epics-containers/epics-base-runtime:7.0.10ec5`, ld.so.conf,
  libevent, amd64-only per-arch stage), then removed the same day: the
  user judged CA too DLS-specific for a generic tool and not that useful
  (agents are behind the egress jail anyway); a user who wants the
  binaries can copy them in. Don't propose it again; the branch history
  has the working recipe if ever needed.
- **Testing a shadow branch in the launcher container**: `uvx
  claude-sandbox shell`, clone the branch, `./install --here`; verify
  with `cat /usr/libexec/claude-sandbox/version` (branch hash, not a
  tag) and a grep for the new code in the package under
  `/usr/libexec/claude-sandbox/venv/lib/python3*/site-packages/claude_sandbox/`
  (`/usr/local/bin/claude` is only the three-line shim). It lives
  only in that named container: `--recreate` or `clean` reverts it. The
  checks must run INSIDE — on the host `uvx claude-sandbox version`
  launched claude with `version` as its prompt until PR #40.
- **Walked back — filtering terminal escape sequences in the pty relay
  (PR #39, closed 2026-09-14)**: RHEL 8/9 desktop terminals DRAW
  unsupported sequences (`␛[>4;2m`, the modifyOtherKeys enable every
  agent emits) instead of dropping them; it reproduces with a native
  host `claude`, so the sandbox is not the cause. A chunk-safe node
  filter on `script`'s output (the bash shadow's `pty_launch`) was built, tested, and
  installed on a DLS box — the junk stayed, and mouse-selection drew
  more of the same, so the user abandoned it as a losing game against
  that terminal. Don't propose stripping sequences again; the real
  answers are a capable terminal or tmux ≥ 3.2 in front of the old one.
  Branch `fix/xtmodkeys-filter` kept. One finding from it (the bash shadow
  ran `script -q -E never` without `-e`, so the agent's exit status never
  reached the caller) is fixed in the Python shadow: `shadow.py` runs
  `script --return`, and `tests/python/test_terminal.py` checks the status
  comes back through it. Refuse dropping `--return`.
