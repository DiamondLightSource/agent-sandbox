(adr-network-egress-jail)=

# 15. Jail Claude's egress in a per-process netns with a routing allowlist

Date: 2026-06-17

## Status

Accepted

Amended 2026-10-07 (5.0): the holder flushes the mirrored main table,
rebuilds exactly the allowlist and verifies it fail-closed; layering
blackholes over pasta's copied routes left more-specific routes reachable.

Layers on top of {ref}`adr-network-egress-open` (ADR 5) — same
mechanism-beneath-bwrap relationship — but **as of 2026-06-18 the jail is the
default**. That overrides ADR 5's *open-egress default* (not its reasoning:
`CLAUDE_SANDBOX_EGRESS_JAIL=0` restores the open path, and filtering still lives
around the tool, not inside it). ADR 5 carries a pointer here.

## Context

ADR 5 left network egress open and said so deliberately: filtering "belongs at
the devcontainer boundary," and a future "add network sandboxing" would be *"a
layered addition on top of credential isolation — record it as its own ADR if
adopted; it does not reverse this decision."* This is that ADR.

The threat is **lateral movement, not exfil** (issue #31, folded into #56). bwrap
already hides credentials and the code is OSS; the asset worth protecting is
*network reach*. A compromised or prompt-injected session sharing the host
network namespace can probe RFC1918, hit internal HTTP and `169.254.169.254`,
and — the incident that motivates this — reach **lab devices with default creds**
(EPICS IOCs, PMAC). A PMAC reached by a hostile session is a *safety* incident,
not just an information one.

Two user cohorts need different controls:

- **Cohort A** — HTTPS to *named* hosts. Claude Code's native sandbox
  (`allowedDomains` / an SNI proxy) fits this; that is issue #33's scope.
- **Cohort B (this repo's users)** — lab devices addressed by **bare RFC1918 IP,
  over UDP, on dynamic ports** (EPICS CA/PVA, PMAC). A hostname allowlist
  *cannot express this*. Cohort B needs IP/CIDR-level control.

The runtime target is **rootless Podman** (pasta is the Podman 5+ default
outbound path). Consequences that rule out Docker-shaped designs: no host
`DOCKER-USER`/iptables knob for the unprivileged user, userspace outbound only,
and no `CAP_NET_ADMIN` on the container. So the control cannot be a host firewall
or a container capability — it has to be built from primitives an unprivileged
user already has.

Feasibility was probed **unjailed on a real rootless host** (the sandboxed
session reports `CapBnd=0`, a false "impossible" reading — see
{ref}`adr-untrusted-workspace`): unprivileged netns create + in-netns routing
with no caps **passes**, `pasta` builds a tap given `/dev/net/tun` **passes**, and
live egress through pasta — internet reachable, non-allowlisted RFC1918
blackholed — **passes**.

## Decision

Add an egress jail **beneath the bwrap wall**, scoped to Claude alone. The
container keeps `--network=host`, so ordinary (non-Claude) shells and EPICS
Channel Access broadcast are untouched. Only the shadow's launch is jailed:

- `claude-shadow` creates a **user + network namespace** with `unshare -rn`
  (a short-lived *holder*) and bwrap **inherits** it — bwrap keeps omitting
  `--unshare-net`; it only nests its own userns inside the holder's. (The holder
  must create the netns, not bwrap and not pasta: the container has no
  `CAP_NET_ADMIN` to make a netns without a userns, and if *pasta* creates the
  namespaces it also makes a pid+mount ns it can't give bwrap a usable `/proc`
  for — bwrap then aborts on `/proc/<pid>` lookups. A user+net-only holder keeps
  `/proc` valid so bwrap nests cleanly.)
- `pasta` **attaches from outside** the holder by PID (`pasta --config-net
  --ipv4-only <holder-pid>`; it backgrounds itself). The egress proxy *must* run
  outside the netns — it needs host connectivity to proxy. No container caps, no
  host-firewall change. Note `--config-net` **mirrors the host's L3 config** into
  the netns: the host address, the **connected-subnet route(s)**, the default
  gateway, and the DNS resolvers from `/etc/resolv.conf`. `--ipv4-only` is
  load-bearing: without it pasta also mirrors the host's **IPv6** connectivity
  (GUA / ULA / link-local) into the netns — an entire address family the IPv4
  routing allowlist does not cover, i.e. a v6 lateral-movement path around every
  v4 blackhole. The jail is therefore **IPv4-only by design**: the netns has no
  IPv6 address at all, so there is nothing to blackhole or punch on the v6 side.
  (The DNS forwarder address `192.0.2.53` is IPv4, so `--dns-forward` is
  unaffected.)
- **Routing-as-allowlist (surgical)** inside the holder's netns. A *blanket*
  RFC1918 blackhole is wrong: on sites where the resolvers and gateway are
  themselves RFC1918 (e.g. an all-`172.23/16` lab network) it kills DNS, and
  pasta's mirrored connected-subnet route is *more specific* than the blackhole,
  so the whole local subnet stays reachable. Instead the holder: `blackhole`
  `10/8`, `172.16/12`, `192.168/16`, the **CGNAT** range `100.64/10` (Tailscale
  and other carrier-grade-NAT internal addresses), **and every connected subnet**
  (pasta mirrors a connected route for *each* on-link subnet, and each is more
  specific than the blackholes, so any one left un-blackholed stays fully
  reachable — the holder enumerates them all, not just the first), `unreachable`
  `169.254/16`; then punches back only — the **gateway** (`/32`, on-link), the
  **pasta DNS forwarder** (`/32` via gw — a non-routable TEST-NET address, not a
  real host), and the **`allow-ip` devices** (`/32` via gw) from
  `/etc/claude-sandbox.conf`. Note what is *not* punched: the resolvers named in
  `/etc/resolv.conf`. Those are real internal hosts, and a `/32` to one is
  reachable on every port, not just 53 — a lateral-movement path for exactly the
  compromised agent this ADR exists to contain. All DNS goes through the
  forwarder instead (see below). The `/32` gateway/forwarder/allow-ip
  re-punches are *more specific* than the `/8`–`/10` blackholes, so longest-prefix
  match keeps them reachable while the surrounding range stays blocked. The holder
  locks these down **before** handing off to bwrap — ordering (netns created →
  pasta attached → routes locked → *then* Claude runs) is load-bearing for the
  boundary. The blackholes are **genuinely fail-closed**: the holder runs in a
  fresh `bash -c`, which the file-level `set -euo pipefail` does *not* reach, so
  the holder re-establishes `set -euo pipefail` as its first statement *and* each
  load-bearing route step is `|| jail_fail`-guarded — a failed blackhole aborts
  the launch so Claude never starts with an internal range reachable. The
  device/DNS punches are fail-soft (a missing one is lost reachability, not an
  open hole).
- **All DNS via the pasta forwarder — one path, always.** pasta attaches with
  `--dns-forward 192.0.2.53` (an RFC5737 TEST-NET address — globally
  non-routable, outside every blackholed range), making it listen on that
  address *inside* the netns and relay DNS to the host's real resolvers (pasta
  runs in the host netns). `claude-shadow` **always** binds a `resolv.conf`
  naming `192.0.2.53` over Claude's, carrying the host's `search` / `domain` /
  `options` lines across so short names still resolve, and the holder routes
  that `/32` via the gateway. Two problems disappear at once:

  - *Stub resolvers* (issue #60). On a personal Ubuntu desktop the sole resolver
    is a **loopback stub** (`127.0.0.53` from systemd-resolved, or Tailscale
    MagicDNS) living in the **host** netns, answering nothing inside the jail —
    every lookup got `ECONNREFUSED` and the API looked down. pasta reaches it.
  - *Resolver-shaped holes* (issue #11). The earlier design punched a `/32` per
    resolver and only staged the forwarder on all-loopback hosts. Those punches
    were the largest remaining hole in the blackhole, aimed at real internal
    infrastructure. Forwarding unconditionally lets them be deleted outright.

  Staging unconditionally is also required for *correctness*, not just posture.
  `--dns-forward` implies pasta's `--dns-host` = the **first** resolver in the
  host's `resolv.conf`, and pasta reverse-translates that host's port-53 replies
  to appear to come from `192.0.2.53`. Any client querying resolver #1 directly
  receives a reply from an unexpected source, which the kernel discards on a
  connected UDP socket — so on *every routable-resolver host* the first resolver
  read as dead from inside the jail while the others worked. glibc absorbed it
  as a multi-second fallback; clients with shorter timeouts (e.g. `glab`) failed
  outright. Enabling the forwarder was never side-effect-free; the only coherent
  choices were "never enable it" or "route everything through it".

  Resolution stays *proxied* and internal IPs stay blackholed, so the boundary is
  intact; if no resolver can be established at all the jail says so rather than
  failing silently.
- **Security rests on userns ownership, not caplessness.** Claude is *not*
  capless here: because bwrap nests its userns inside the holder's unprivileged
  userns, the new userns grants Claude a **full** capability set (`CapBnd` =
  `…1ffffffffff`). That is expected and unavoidable. What contains it: the netns
  and its routes are owned by the **holder's** userns — an *ancestor* of Claude's
  — so Claude's caps confer no authority over them. Verified directly: from inside
  the jail, deleting a blackhole route, punching a route past it, and creating a
  net device all fail `EPERM`, and RFC1918 stays blocked after the attempts.

**On by default, fail-closed, with an escape hatch.** The jail runs unless
`CLAUDE_SANDBOX_EGRESS_JAIL=0` (env, per session) or `egress-jail = 0`
(`/etc/claude-sandbox.conf`, per host) disables it. If the jail is on but a
prerequisite is missing (`/dev/net/tun`, pasta, unshare), the launch **fails
closed** — `claude` refuses to start rather than silently dropping back to open
egress — and the error names both the fix and the `=0` escape hatch. This is the
secure-by-default choice for the lab threat (a misconfigured host can't quietly
lose the control); the escape hatch keeps a non-EPICS or deliberately-open host
unblocked.

Two structural choices fix scope:

- **Bash, inlined in the shadow.** The setup is implemented as inlined functions
  (`netns_launch()` orchestrating, `netns_holder()` running inside `unshare -rn`,
  plus an `egress_jail_enabled` predicate) *inside* `claude-shadow`, not a sourced
  module — preserving the
  single-file, read-top-to-bottom auditability that {ref}`adr-bash-only` and
  {ref}`adr-integrity-surfaces` rest on. netns + routing + pasta *is* shell
  orchestration of CLI tools; the literal `ip route add blackhole …` commands are
  the most auditable representation of the boundary, so no higher-level language
  is warranted. (Trigger to revisit extraction into its own file — its own ADR —
  is if the net code outgrows the shadow's readability.)
- **Allowlist lives in `/etc`, not the workspace.** `allow-ip` entries come from
  `/etc/claude-sandbox.conf`, outside the sandbox's rw set, per
  {ref}`adr-untrusted-workspace`. A per-workspace allowlist would be
  attacker-writable from inside the jail.

## Consequences

- **Defence in depth, not a stronger wall.** This layer sits *beneath* bwrap: a
  bwrap *escape* could re-plumb the netns. It raises the cost of lateral movement
  for a contained session; it is never stronger than the bwrap boundary above it.
- **Channel Access broadcast for Claude is gone.** Claude's private netns has no
  LAN broadcast domain, so CA auto-discovery won't work *for Claude* — it must use
  unicast `EPICS_CA_ADDR_LIST`. Normal shells keep host networking and broadcast.
- **Requires `/dev/net/tun` in the container** (`devcontainer.json` runArgs
  `--device=/dev/net/tun`); pasta and slirp4netns are both TAP-based and neither
  works without it. This is the one hard container-side requirement.
- **Requires `passt` 0.0~git20230908 or later** (issue #85). Debian 12's
  0.0~git20230309 fails in two ways, both verified in rootless podman on an
  Ubuntu host. First, Debian's packages before 0.0~git20230908 install
  `pasta` as a symlink to `passt`, so a host that enforces AppArmor (Ubuntu
  loads profiles for both) confines `pasta` under the `passt` profile. That
  profile denies the holder's `/proc/PID/ns/*` and pasta's own log file, so
  the attach fails. From 0.0~git20230908 `pasta` is a hard link with its
  own profile. Second, with that worked around (`pasta` hard-linked), the
  attach succeeds but the old `--config-net` adds the address with a
  `broadcast` route in table `local` that the route read-back refuses.
  Upstream releases from 2023_06_03 pass; the change is cc9d167 ("With
  --config-net, copy all addresses by default", 2023-05-14).
  bookworm-backports has no passt. The install and `claude-sandbox doctor`
  warn when the jail is on and dpkg reports an older passt. An unreadable
  version gives no warning, only a doctor note. Not `pasta --version`,
  which prints "unknown version" for Debian 12's and Ubuntu 24.04's builds.
- **`HTTPS_PROXY`-style env proxy is explicitly not the mechanism** (issue #31
  Option D): a hostile process unsets the env var or opens a raw socket. The
  control is enforced by ancestor-owned netns + kernel routing, not by environment.
- **Integrity checks still pass in the jail — no jail-aware variant needed.**
  `/verify-sandbox` check 06 (and `sandbox-verify.sh`) assert **`CapEff=0`** (the
  *effective* set), not `CapBnd`. bwrap's `--cap-drop ALL` empties the effective
  set even inside the nested userns, so `CapEff=0` holds and the full 18-check
  battery passes in a jailed session (verified live). What differs from the
  non-jail sandbox is only the **`CapBnd` ceiling** — `…1ffffffffff` in the jail
  vs `0` non-jail, a nested-userns artifact. Effective caps are zero, so nothing
  is active; route-immutability additionally holds via ancestor-userns ownership.
  Cap-ceiling diligence — **verified 2026-06-18** (`probe-network-jail-caps.sh`,
  run unjailed): the higher `CapBnd` ceiling cannot be *re-raised* to weaken
  another bwrap protection. Even after `unshare -rUm` grants a full *effective*
  cap set in a child userns, `mount -o remount,rw /`, a bind-mount over a
  `--ro-bind` path, and `sethostname` all `EPERM` — bwrap's locked mounts are
  immutable from any descendant userns. The full `CapBnd` is therefore inert.
- **Hostname allowlists stay out of scope for Cohort B.** Native `allowedDomains`
  cannot express bare-IP/UDP/dynamic-port device traffic; Cohort A / dual-sandbox
  remains issue #33.
- **On by default shifts the dogfood ≈ guest cost.** With the jail the default
  and fail-closed, a host that hasn't mounted `/dev/net/tun` (a `devcontainer.json`
  runArg an installer can't add) gets a `claude` that refuses to launch until it
  either adds the device or sets `CLAUDE_SANDBOX_EGRESS_JAIL=0`. `install.sh`
  installs pasta so that prerequisite is never the blocker; the dogfood box mounts
  the tun device. A plain `git clone + ./install` guest that wants the default
  jail must add the one runArg — the error says so — and otherwise opts out with
  `=0`. The deliberate trade: a loud stop over a silent downgrade of the default
  control. Since issue #71 the install, the published image's start and
  `claude-sandbox doctor` also warn about a missing tun device, before the
  first launch; the launch itself stays fail-closed.
- **Verification will follow the same three-surface model** as
  {ref}`adr-integrity-surfaces`: a FUTURE, optional jail-aware check (not yet
  implemented) would assert, when the jail is enabled, that the netns exists and
  the RFC1918 blackhole holds with only the configured `allow-ip` routes punched
  through. This is a future item — the existing 18-check battery already passes
  unchanged in a jailed session (check 06 asserts `CapEff=0`; see the bullet
  above), so no jail-aware variant is required today.
- **Proven before adoption:** the full Design-D chain — holder netns → pasta
  attach → route lockdown → nested capful bwrap → Claude — works on a real
  rootless host, *and* the route-immutability security battery passes
  (`probe-network-jail.sh`, run unjailed).
- **Done since adoption:** implemented in `claude-shadow`
  (`netns_launch`/`netns_holder`/`egress_jail_enabled`) plus `install.sh` (installs
  `passt`, which provides pasta) and tests; on by default, fail-closed, and
  validated end-to-end on a rootless `--network=host` host **and in a bridge/NAT
  container** — the gateway-collision and nested-pasta paths are proven (the
  gateway is pinned on-link before the RFC1918 blackhole, so egress works while
  RFC1918 and the same subnet stay blocked). The shadow/`install.sh`/tests
  implementation landed on adoption (2026-06-18).
- **Still optional/open:** only the jail-aware `/verify-sandbox` check (a future
  item, not yet implemented — see the verification bullet above). Core
  internet/RFC1918 behaviour is already proven.

Live design and the feasibility probes: issue **#56** (refines #31, which is
closed; #33 remains open for Cohort A).
