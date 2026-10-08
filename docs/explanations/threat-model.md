# Threat model

The sandbox limits what an agent can access when prompt injection or a mistaken
instruction leads it to run unwanted commands. Claude, Codex and Pi share the
same isolation layer, whether launched in the published image or a custom
devcontainer.

## What it protects

The wrapper hides host credentials and desktop connections, restricts writable
paths, and blocks access to internal networks by default. These controls
address four routes:

- Reading secrets from environment variables, home directories or token stores.
- Driving the host IDE or desktop through IPC, runtime or X11 sockets.
- Escalating privileges through setuid programs, capabilities or terminal input.
- Reaching internal services and lab devices through the host's network access.

[Locked-down defences](../reference/locked-down-defences.md) maps the controls
to their implementation and verification checks.

The developer is trusted. Directly launching a vendor binary or changing the
container configuration can bypass the wrapper. Keep the host kernel patched;
the sandbox does not promise to contain kernel or device-driver exploits.

## What remains accessible

The agent can read and change its workspace, use its own login credentials
and any supplied forge tokens, and reach the internet. Tool caches, shared
skills and explicitly allowed paths, services and devices are also exposed.
See [Deliberately exposed](../reference/deliberately-exposed.md) for the path list.

A compromised session can alter its writable settings and skills. Shared
`~/.agents/skills` extends that persistence to other agents and projects using
the same terminal config. Review those files after an untrusted session.

Custom mounts and tools can introduce credentials outside the masked paths.
In particular, `.local/share` and `.cache` are available to agents; credentials
stored there are exposed. Audit additions to your container.

## What a session leaves behind

The sandbox contains the agent while it runs, not the code it leaves behind:
the venv's packages and scripts, Git hooks, `.git/config`, Makefiles, test
fixtures and editor tasks. None of it escapes on its own; it runs when you, or
a tool outside the sandbox, run it later (`uv run`, `pytest`, `make`,
`git commit`, a VS Code task). Treat it like an unreviewed contribution:
review a session's changes before running project code outside the sandbox,
and look in `.git/hooks`, which never appears in a diff.

Keep the project venv after the system directories on PATH, as the published
image does (`PATH=$PATH:/opt/venv/bin`); a venv first on PATH lets an
executable the session left there shadow a system command in every outer
shell. See {ref}`ADR 28 <adr-review-what-a-session-leaves>`.

## The irreducible workspace-visibility caveat

**Keep secrets outside the workspace.** A `.env` file or credential checked
into the project is readable by the agent. The sandbox cannot hide project
contents while allowing the agent to work on them.

Read-only mounts prevent edits, not disclosure. Enabling `--peers` makes
sibling checkouts readable too, including any secrets they contain.

## PAT hygiene: the soft underbelly

An agent can use a forge token for anything its permissions allow. The sandbox
cannot distinguish an intended push from an unwanted one.

Use a short-lived token restricted to the project, with only the permissions
needed. Omit workflow and administrative permissions unless the task requires
them. The authentication helpers keep tokens out of shell history but do not
enforce their scope.

For sessions that need no forge access, set `no-forge` in the sandbox config.
See [Authenticate with forges](../how-to/authenticate-with-forges.md) for token
permissions and the no-push setup.

## The egress jail and the native sandbox

The default jail uses an IPv4-only network namespace. It blocks RFC1918,
CGNAT (`100.64/10`), connected subnets and link-local addresses, including
the usual cloud metadata addresses: `169.254.0.0/16` and Azure's
`168.63.129.16`. The gateway, DNS resolvers and configured `allow-ip`
destinations remain reachable. The jail does not add these rules on top of
the routes it inherits: pasta copies the outer network's routes into the
namespace, including more specific ones (a DHCP route to a metadata
service, a VPN's internal subnets) that would otherwise win over a
blackhole. The jail empties its route table, builds exactly the allowlist,
and reads every table back before the agent starts; anything else refuses
the launch. Explicit loopback relays expose the
whole service on their selected port.

This limits lateral movement to internal hosts and lab devices. It does not
filter internet domains or prevent uploading readable data to the internet.
If that is required, apply an egress policy at the container boundary.
Claude Code's native domain controls are a separate layer; they do not replace
this sandbox's credential isolation or IP-based lab-device access rules.

The wrapper refuses to launch if `/dev/net/tun`, `pasta`, `unshare` or `ip` is
missing, and if `socat` or `ss` is missing when loopback relays are configured.
Setting `CLAUDE_SANDBOX_EGRESS_JAIL=0` disables the network jail and restores
access through the outer container's network. Ordinary container shells are
also outside this jail. See [network configuration](../how-to/network-egress-jail.md).

## Device access

Device passthrough is off by default. `--device` exposes selected
hardware to every process in the agent session, including downloaded tools
and project scripts. Driver vulnerabilities can cross the sandbox boundary;
Device resource exhaustion and device side effects are not contained. Raw block
devices can bypass filesystem restrictions.

Enable only the devices the workload needs. See
[Devices](../how-to/use-the-container-image.md#devices).
