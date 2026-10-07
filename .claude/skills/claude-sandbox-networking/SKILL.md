---
name: claude-sandbox-networking
description: >-
  Network egress, firewall, and lateral-movement design for this repo's bwrap
  Claude sandbox. The per-process egress jail (netns + pasta routing allowlist,
  ADR 0015, issue #56) is ON by default as of 2026-06-18, fail-closed, with a
  CLAUDE_SANDBOX_EGRESS_JAIL=0 escape hatch — overriding ADR 0005's earlier
  open-egress default. Surface
  BEFORE proposing or discussing ANY network change: egress filtering, firewall
  / nftables / iptables / DOCKER-USER, `--unshare-net`, netns / veth, pasta /
  slirp4netns, a CONNECT or SNI proxy, `HTTPS_PROXY` injection, VLAN /
  segmentation, Claude Code's native sandbox `allowedDomains`, or device-access
  networking (EPICS / Channel Access / pvAccess / PMAC).
---

# claude-sandbox-networking

Read before designing or sketching any network-egress change. The live design
is **issue #56**; this skill is the durable context + the guards that stop
agents re-deriving already-rejected options (it has happened).

## Standing decision — the egress jail is ON by default (ADR 0015)

As of **2026-06-18** the per-process egress jail is the **default** posture
(`docs/explanations/decisions/0015-network-egress-jail.md`, **Accepted**). It is
**fail-closed**: if `/dev/net/tun` / pasta / unshare are missing, `claude`
refuses to launch (it does NOT silently fall back to open egress). The escape
hatch is `CLAUDE_SANDBOX_EGRESS_JAIL=0` (env, per session) or `egress-jail = 0`
in `/etc/claude-sandbox.conf` (per host); env wins over conf. Mechanically it's
still a layer *beneath* bwrap (a holder netns), not an in-core firewall — that
part of ADR 0005's reasoning stands.

This **overrides ADR 0005's open-egress *default*** (`0005-network-egress-open.md`,
now "Superseded in part"). 0005's analysis still applies to the `=0` path: with
the jail off the bwrap argv omits `--unshare-net` and shares the host netns, no
per-process firewall — the original open-egress behaviour. So: the default is
jailed; `=0` restores 0005's world. Don't describe egress as "open by default"
anymore, and don't re-add an "opt-in / off by default" framing — that was the
pre-2026-06-18 state.

## ALL DNS goes through pasta `--dns-forward` (issues #60, #11 — both fixed)

`jail.stage_dns()` **always** stages a `resolv.conf` naming `192.0.2.53`, which
`bwrap.py` binds over Claude's (named by `CLAUDE_SANDBOX_JAIL_RESOLV`),
copying the host's `search`/`domain`/`options` lines across — dropping those
breaks short-name resolution at sites with a search list. pasta attaches with
`--dns-forward 192.0.2.53` (RFC5737 TEST-NET — non-routable, outside every
blackhole), listens on that addr *inside* the netns and relays to the host's
real resolvers from the host netns. One DNS path, not two.

**`--dns-forward` is NOT side-effect-free — this is the trap.** It implies
pasta's `--dns-host` = the **first** resolver in the host's `resolv.conf`, and
pasta reverse-translates that host's port-53 replies to come from `192.0.2.53`.
Any client querying resolver #1 *directly* gets a reply from an unexpected
source, which the kernel silently discards on a connected UDP socket. So the old
"stage the forwarder only on all-loopback hosts, leave routable hosts unchanged"
design **poisoned resolver #1 on every routable-resolver host**: glibc absorbed
it as a ~10s fallback, Go clients (`glab`) failed outright, and it read as "that
DNS server is down". The only coherent choices are never enable the forwarder,
or route everything through it. We route everything through it.

Two symptoms that look like a network fault but are this: a resolver that is
`ping`-able and answers from outside the jail but times out inside, and DNS that
"works but is slow" for glibc tools while Go tools fail.

Original driver (#60): on a personal Ubuntu desktop the sole resolver is a
**loopback stub** — `127.0.0.53` (systemd-resolved) or Tailscale MagicDNS —
living in the **host** netns, answering nothing inside the jail, so every lookup
was `ECONNREFUSED` ("works at the office, fails at home"). pasta reaches it.
**Don't** "fix" stub DNS by punching loopback
`/32`s (can't route loopback) or by recovering upstreams from
`/run/systemd/resolve/resolv.conf` (the rejected fallback — fails for Tailscale
MagicDNS, whose `100.100.100.100` is served locally by tailscaled, not
routable). `probe-network-jail.sh` mirrors the forwarder path; this dogfood box
(`127.0.0.53` + Tailscale) is exactly the affected config.

## Diagnostic discipline — a connected UDP socket HIDES a source mismatch

When a UDP service (DNS especially) "doesn't answer" inside the jail, do not
conclude the packet was dropped until you have looked with an **unconnected**
socket. `nslookup`, `dig`, glibc, Go and a bash `/dev/udp` probe all *connect*
the socket, so the kernel discards any reply whose source differs from the
address queried — a translated reply and a lost packet are indistinguishable.
That cost real time in #11: the server was answering correctly the whole while.

Reveal it with `recvfrom` printing the peer (perl is present in the sandbox;
`dig` and `python3` are not, and `nslookup` is BusyBox — no `-vc`, and it
predates most flags you'd reach for):

```bash
perl -e 'use Socket; socket(my $s,PF_INET,SOCK_DGRAM,0);
  send($s,$Q,0,sockaddr_in(53,inet_aton($ns)));
  my $f=recv($s,my $b,512,0); my($p,$a)=sockaddr_in($f);
  print inet_ntoa($a)," ",length($b),"\n"'
```

Reply source ≠ address queried → address translation (pasta), not a firewall.
Corollary for triage: compare **all** resolvers, not just the failing one. "Only
the first one is broken" is the signature of `--dns-host`, and a single-resolver
test cannot see it.

## Runtime target — rootless Podman (NOT Docker-bridge)

v1 targets **rootless Podman** + Debian/Ubuntu + `remoteUser=root` (rootless
Docker untested). Consequences that invalidate Docker-shaped designs:
- **No host `DOCKER-USER` / iptables** knob for the unprivileged user.
- Outbound is userspace — **pasta** (modern Podman 5+ default) or legacy
  slirp4netns.
- The container likely has **no `CAP_NET_ADMIN`**.
- **Probe caps UNJAILED.** A sandboxed `claude` session reports
  `CapBnd=0000000000000000` (the shadow's `--cap-drop ALL`) and `unshare -rn`
  fails on RO `/proc/self/uid_map` — that's the *jail*, not the container. Run
  the probe from a normal devcontainer terminal.

## Rootful docker cannot host the jail (found via PR #78 CI, 2026-07)

Under **rootful docker** pasta's attach fails: `Couldn't open user namespace
/proc/<holder>/ns/user: Permission denied` — differing ns/ptrace-access
semantics vs rootless podman — so the fail-closed jail refuses to launch
`claude`. Not fixable by seccomp/apparmor-unconfined or the userns sysctl (all
were lifted when this reproduced). Consequences: rootless podman stays the
supported runtime; the published-image how-to documents the rootful-docker
caveat (escape hatch `CLAUDE_SANDBOX_EGRESS_JAIL=0`, weaker posture); and any
CI that exercises a real jailed launch must run the container under **rootless
podman** (`docker save | podman load`, then `podman run --device /dev/net/tun
--security-opt label=disable ...`) — see the e2e test in
`.github/workflows/container.yml`, which is exactly the skill's throwaway
bridge-container validation, automated. Don't burn time re-trying cap-adds on
rootful docker.

## Threat & cohorts

- **Threat = lateral movement** (issue #31, now folded into #56): a compromised
  session as a network pivot — RFC1918 probing, internal HTTP, `169.254.169.254`,
  **lab devices with default creds** (PMAC = a *safety* incident). Not exfil
  (bwrap hides creds); the asset is *network reach*.
- **Cohort A** — HTTPS to *named* hosts only. Claude Code's native sandbox
  (`allowedDomains`, an SNI proxy) fits; dual-sandbox mode is issue **#33**.
- **Cohort B (this repo's users)** — lab devices by **bare RFC1918 IP, UDP,
  dynamic ports** (EPICS CA/PVA, PMAC). A hostname allowlist **cannot express
  this**, so native `allowedDomains` is a non-fit. Needs IP/CIDR-level control.

## The approach we've landed on — issue #56 (refines #31 Option C)

Keep `--network=host` (normal shells + CA broadcast untouched); jail **only
Claude** via **Design D** (validated 2026-06-18, `probe-network-jail.sh`): the
shadow creates a user+net ns with `unshare -rn` (a *holder*); **pasta attaches
from OUTSIDE** by PID (`pasta --config-net <holder-pid>`, backgrounds); the
holder locks a **surgical routing allowlist** (see below); then
`exec bwrap … --cap-drop ALL -- claude`, which **inherits** the holder's netns
(bwrap keeps OMITTING `--unshare-net`). Ordering is load-bearing: netns → pasta →
routes locked → Claude.

**SURGICAL policy (v2 — a blanket RFC1918 blackhole is WRONG):** `pasta
--config-net` mirrors the host L3 config into the netns (address, connected
subnet, gateway, resolv.conf DNS). On an all-RFC1918 site (Diamond = all
172.23/16) a blanket blackhole kills DNS, and pasta's connected-subnet route is
more-specific than the blackhole so the whole local /20 stays reachable. So the
holder: `blackhole` 10/8 + 172.16/12 + 192.168/16 + the **connected subnet**,
`unreachable` 169.254/16; then punches back ONLY the **gateway** (/32 on-link),
the **pasta DNS forwarder** `192.0.2.53` (/32 via gw — non-routable TEST-NET,
terminates in pasta, NOT a real host), and **`allow-ip`** devices (/32 via gw).
Blackholes fail-closed; forwarder/device punches fail-soft.

**Resolvers get NO /32 — do not re-add them** (issue #11, fixed 2026-08-14).
Until then the holder punched a /32 per `/etc/resolv.conf` resolver, justified
as "resolution ≠ lateral movement". That reasoning was wrong: the punch is a
route, not a protocol filter, so it opened a real internal host on **every**
port, not just 53 — the largest remaining hole in the blackhole, aimed squarely
at infrastructure. All DNS now goes through the forwarder, so the jail needs no
route to any resolver and the holes are gone. Re-adding a resolver punch (or an
`allow-ip`-style entry for a DNS server) reopens it.

**DEAD ALTERNATIVE — do not retry:** pasta-creates-the-ns + bwrap-nested-inside.
pasta spawns a pid+mount ns it can't give bwrap a usable `/proc` for → bwrap
aborts on `/proc/<pid>` lookups; remount is EPERM (kernel proc-mount restriction).
Design D flips ns ownership (holder = user+net only → `/proc` valid).

**SECURITY rests on userns ownership + effective-cap drop.** Distinguish two cap
sets: `CapEff` (effective, active) is **0** in the jail — bwrap's `--cap-drop ALL`
empties it even in the nested userns — while `CapBnd` (the bounding *ceiling*) is
FULL (`…1ffffffffff`, a nested-userns artifact, vs 0 in the non-jail sandbox).
Route-immutability holds because the netns/routes are owned by the holder's
ANCESTOR userns (caps raised inside Claude's own userns don't reach it): verified
route del/punch + device-add all EPERM, RFC1918 stays blocked.
**verify-sandbox needs NO jail-aware variant** — check 06 asserts `CapEff=0` (not
CapBnd), which holds; the full 21-check battery passes live in a jailed session.
Cap-ceiling diligence is **VERIFIED** (`probe-network-jail-caps.sh`, unjailed,
2026-06-18): the full `CapBnd` ceiling can't be re-raised to weaken another bwrap
protection. Even with a full *effective* cap set gained via a child `unshare
-rUm` userns, remount-rw `/`, bind-over a `--ro-bind` path, and `sethostname` all
`EPERM` — bwrap's locked mounts are immutable from a descendant userns. Inert.

**Structure:** the jail is `src/claude_sandbox/jail.py` (`launch` outside,
`holder_main`/`lock_routes` inside the holder, `stage_dns`, the relays), one of
the four audit-core modules of ADR 26 (with `bwrap.py`, `shadow.py`,
`watch.py`), readable top to bottom; the `egress_jail_enabled` predicate and
the `egress-jail`/`allow-ip` keys are in `config.py`. Every side effect goes
through an `Ops` object so the unit tests replace it. Don't spread the jail
across more modules (the `bf65407` failure ADR 26 guards against).

**STATUS — IMPLEMENTED + END-TO-END VALIDATED (2026-06-18).** Probe + real
binary both green on a real rootless host: `CLAUDE_SANDBOX_EGRESS_JAIL=1 claude
-p` reaches the API through the jail; route-immutability battery passes; Cohort B
`allow-ip` device path confirmed reachable; same-subnet host blackholed. (Then
in the bash shadow; since 5.0 `jail.py`, with the `parse_config`
`egress-jail`/`allow-ip` keys and the `egress_jail_enabled` predicate in
`config.py`.) **ON by default** — disable
with `CLAUDE_SANDBOX_EGRESS_JAIL=0` (env) or `egress-jail = 0` in
`/etc/claude-sandbox.conf`. Requires `/dev/net/tun` (`devcontainer.json` runArgs
`--device=/dev/net/tun`) — the one hard container-side dep; **fail-closed** if
pasta/unshare/tun missing (`claude` won't launch — the error names only the
real fix, never the `=0` escape hatch; see the claude-sandbox skill's
weakening-switch policy), never a silent unjailed fallback. Interactive
`claude` + `/verify-sandbox` both confirmed live in a jailed session (18/18 pass
— check 06 asserts `CapEff=0`, which holds). Phase-2 landed on **PR #58** (refs
#56): the installer installs `passt` (now `installer/system.py`
`APT_PACKAGES`); CapEff/CapBnd doc corrections; cap-ceiling
diligence probe written + PASSED unjailed (full `CapBnd` inert). Bridge/NAT now
**VALIDATED** (see [[network-egress-pasta-jail-wip]]): `probe-network-jail.sh`
run in a **bridge/NAT** container proves the gateway-collision + nested-pasta
paths (the RFC1918 gateway is pinned on-link before the blackhole; egress works,
RFC1918 + same-subnet still blocked). Ceiling: a bwrap *escape*
could re-plumb — a layer *beneath* the bwrap wall, never stronger. CA broadcast for Claude is gone → unicast
`EPICS_CA_ADDR_LIST`.

**Network-mode-agnostic + intentional blackholing.** Design D builds Claude's
netns INSIDE the container and pasta mirrors the container's OWN connectivity, so
it works whether the container is `--network=host` OR bridge/NAT — same
requirement (`/dev/net/tun`), one install path, one error message. The jail only
RESTRICTS; it cannot grant more reach than the container already has (an
internal/isolated container can't reach a device regardless of the jail). The
`/dev/net/tun` mount and a possible `--network=host`→bridge switch are BOTH
host-`devcontainer.json` edits, so `install` must detect + error with
instructions either way. **Blackholing must be intentional** — CRITICAL in
non-host containers where the egress gateway is itself RFC1918 (or link-local
`169.254.x.x`): blackholing those ranges can sever the default route and kill ALL
egress. The holder (`jail.lock_routes`) detects the default next-hop and pins a more-specific
route to it FIRST: **protect-gateway → blackhole-the-rest → punch allow-ip** (implemented).
PROVEN in a NON-host (bridge) container (validated 2026-06-18): (a) nested pasta
(inner pasta inside an outer-pasta'd container) works; (b) the gateway-collision
behaviour is handled (gateway pinned on-link first). To reproduce: **a bridge
container = the devcontainer with `--net=host` REMOVED**
(comment `.devcontainer/devcontainer.json` runArgs line `--net=host`; KEEP
`--device=/dev/net/tun`), then rebuild and run `probe-network-jail.sh` from a
normal (unjailed) terminal — revert + rebuild afterwards (the dogfood box needs
host-net for X11 + EPICS CA). Throwaway alternative that leaves the devcontainer
alone: `podman run --rm -it --device=/dev/net/tun -v "$PWD/diagnostics/probe-network-jail.sh:/probe.sh:ro" <devcontainer-image> bash -lc 'apt-get update -qq && apt-get install -y -qq passt bubblewrap iproute2 util-linux && bash /probe.sh'`.
EXPECT: the `[holder]` line shows an RFC1918 gateway (podman `10.88.0.1`, docker
`172.17.0.1`); `PASS internet routed via gateway` + connectivity PASS = proof
that the `$gw/32` on-link route was pinned BEFORE `blackhole 10/8` survived
egress; RFC1918 + same-subnet still blocked. The ONE code-change trigger: holder
logs `no default via/dev` → exits 4 = the dev-only-default edge (a default route
with no `via <gw>`), to be handled then. Both `--network=host` and bridge/NAT are
now validated.

## Loopback relays — the ONLY paths across the netns boundary (ADRs 19, 20, 21)

pasta's port forwarding and gateway mapping are OFF for every agent (`-t none
-u none -T none -U none --no-map-gw`, ADR 19): auto-forwarding exposed
unrelated host-loopback listeners. What crosses instead is one socat pair per
port over a private Unix socket in the jail's `/tmp` relay dir (bwrap masks
it), started by `jail.launch` (outer ends, `_outbound_relays` /
`_callback_relays`) and the holder (inner ends, `_hold_with_relays`, in the
netns before bwrap). Two directions, two conf keys:

- **Outbound** `local-model-port` (shipped 1920, Pi's lllm2 discovery) +
  `local-port` lines / `CLAUDE_SANDBOX_LOCAL_PORTS` (ADR 20): inner socat
  LISTENS on the agent's 127.0.0.1, outer socat CONNECTS to the outer
  loopback. Fatal if a relay can't start.
- **Inbound** `callback-port` lines / `CLAUDE_SANDBOX_CALLBACK_PORTS` (ADR 21,
  2026-09-11): outer socat LISTENS on the outer 127.0.0.1, inner socat
  connects to the agent's loopback per connection. Exists because pi's
  Claude Pro/Max `/login` opens `127.0.0.1:53692` inside the jail and has no
  code-display fallback (dropped upstream in pi 0.59.0); Claude Code has the
  code page + random port so needs nothing; Codex is 1455 with a device-auth
  alternative. Shipped default `53692`. **Fails SOFT** on a taken port (warn,
  launch anyway) because the listening end can collide with a second session
  or an unwrapped agent. A port may not be in both sets
  (`validate_callback_ports`).

Neither adds a route; the host side is always the outer container's IPv4
loopback (host's under `--net=host`, the container's own in bridge mode, where
the published-image launcher would have to publish the port — not done).
Don't reach for pasta `-t/-T` to "simplify" either direction.

## Refuse / don't re-derive

- Re-proposing the **`HTTPS_PROXY` env-var proxy** (#31 Option D) as *security* —
  a hostile process unsets it / uses a raw socket. It's a guardrail, not a boundary.
- **Docker-bridge / `DOCKER-USER` / `--cap-add NET_ADMIN`-on-the-Claude-container**
  designs — they ignore the rootless-Podman target.
- Claiming a **hostname allowlist** (native `allowedDomains`) covers **Cohort B**
  device traffic — it can't (no bare IP / CIDR / UDP).
- Reading caps **from inside the jail** and concluding the container can't do netns.
- **pasta-creates-the-namespaces with bwrap nested inside** (the dead Plan A) —
  pasta's pid+mount ns gives bwrap an unusable `/proc`; use Design D (holder owns
  a user+net-only ns, pasta attaches from outside).
- Asserting Claude is **capless in the jail** — it is NOT (`CapBnd` full, nested
  userns). Security is ancestor-userns ownership of the netns; don't "fix" the
  full caps or gate jail integrity on `CapBnd=0`.
- **Turning off `--network=host`** (per-container egress allowlist instead of
  per-process) as the DEFAULT. It IS simpler — `CapBnd=0` preserved, no
  `/dev/net/tun`, no holder/pasta-attach (root-in-container owns its own netns,
  locks routes once at init) — but it restricts the **whole** container and
  breaks EPICS CA/PVA broadcast for **all** shells, not just Claude. Rejected as
  default for this EPICS org (Design D breaks broadcast for Claude *only*). It's
  a valid **opt-in posture** for Claude-dedicated / non-EPICS containers, not a
  silent flip of the host-net default. (Keeping host-net is an EPICS-workflow
  choice, NOT a mechanism requirement — see portability note: Design D works in
  non-host containers too.)
- **Rewriting ADR 0005** to say the opposite. Its open-egress *default* is now
  overridden (jail-on-by-default), but that was recorded the right way — a
  "Superseded in part" status note on 0005 + the layered ADR 0015 — leaving 0005's
  analysis intact for the `=0` path. Record future posture changes the same way;
  don't gut 0005.
- Mounting **`docker.sock`** into the Claude container.

## Pointers

| Concern | Where |
|---|---|
| Egress-jail decision (chosen approach) | ADR **0015** `0015-network-egress-jail.md` (Accepted) |
| Live design + feasibility probes | issue **#56** |
| Threat + options A–D (superseded by #56) | issue **#31** (closed) |
| Native dual-sandbox / Cohort A | issue **#33** (open) |
| Egress-open decision / scope | ADRs `0005-network-egress-open`, `0002-credential-isolation-tool` |
| Feasibility / route-immutability probes (now tracked under `diagnostics/`) | `diagnostics/probe-network-jail.sh` (full pasta egress + route-immutability battery), `diagnostics/probe-network-jail-caps.sh` (cap-ceiling diligence), `diagnostics/probe-network-layers.sh` (splits tun-INDEPENDENT core from tun-DEPENDENT forwarder) — run UNJAILED |
| Egress-jail code (holder + pasta attach + route lock + relays) — **implemented + on by default** | `src/claude_sandbox/jail.py`: `launch` (outside), `holder_main` / `lock_routes` / `_hold_with_relays` (the holder, re-entered as `python -I -m claude_sandbox _jail_holder`), `stage_dns`; `config.py`: `egress_jail_enabled`, `allow-ip`, the port checks; `bwrap.py`: the staged `resolv.conf` bind (bwrap KEEPS omitting `--unshare-net`; the holder owns the netns) |
| Egress-jail tests | `tests/python/test_jail.py` (unit, `Ops` replaced: argv, fail-closed paths, cleanup, signals); `tests/python/test_jail_netns.py` (real namespaces, pasta, socat — routes locked, DNS through the forwarder, relays, cleanup, Ctrl-C), run by `tests/jail_python.sh` with a skip as a failure, in the image (`container.yml`) and on the runner (`ci.yml` egress-jail job) |
