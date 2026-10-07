#!/usr/bin/env bash
# Plan B / Design D — integrated egress-jail probe (issue #56 / ADR 0015), with
# the SURGICAL routing policy (v2). Earlier versions blanket-blackholed RFC1918,
# which broke real use: pasta --config-net mirrors the host's L3 config into the
# netns (address, connected subnet, default gw), and on this site DNS resolvers
# and the gateway are themselves RFC1918 (172.23.x) — so a blanket blackhole
# kills DNS, and the mirrored connected-subnet route (more specific than the
# blackhole) leaves the whole local /20 reachable.
#
# v2 policy (validated here before patching the shadow, now jail.py):
#   - IPv4-only netns: pasta attaches with --ipv4-only, so there is NO IPv6
#     address family in the netns (no GUA/ULA/link-local v6 lateral path)
#   - blackhole RFC1918 (10/8, 172.16/12, 192.168/16), CGNAT (100.64/10), and
#     EVERY connected subnet (not just the first scope-link route)
#   - punch back ONLY: gateway (/32 on-link), DNS resolvers from resolv.conf
#     (/32 via gw), and allow-ip devices (/32 via gw)
# => internet + DNS work; lateral movement to internal hosts (incl. same-subnet,
#    CGNAT/Tailscale, and IPv6) is blocked except the allow-listed devices.
#
# Stub-resolver DNS (issue #60): when /etc/resolv.conf names ONLY loopback
# resolvers (systemd-resolved 127.0.0.53, Tailscale MagicDNS) they're
# unreachable from the netns, so the probe mirrors the jail.py fix —
# pasta --dns-forward listens on JAIL_DNS_FWD (RFC5737 192.0.2.53) inside the
# netns and relays to the host's real resolvers, and the INNER bwrap gets a
# resolv.conf pointed at that forwarder. This box (127.0.0.53 + Tailscale) is
# exactly the affected config, so the DNS+egress test exercises the fix.
#
# Topology (unchanged from v1): parent unshare -rn holder owns a user+net-only ns
# (so /proc stays valid for nested bwrap); pasta attaches from OUTSIDE by PID;
# holder locks routes; holder execs bwrap --cap-drop ALL (NOT capless — nested
# userns — but the netns is ancestor-owned, so routes are immutable from inside).
#
# ► RUN ME UNJAILED ◄  (a normal devcontainer terminal, not a sandboxed `claude`).
#
# Usage: ./probe-network-jail.sh [DEVICE_IP[:PORT]] [HOSTNAME:PORT]
#   DEVICE_IP   optional internal device that must stay reachable via allow-ip
#   HOSTNAME    name:port for the DNS+egress test (default api.anthropic.com:443)
set -uo pipefail
DEV="${1:-}"; NAME="${2:-api.anthropic.com:443}"; export DEV NAME
JAIL_DNS_FWD="192.0.2.53"; export JAIL_DNS_FWD

if [ "${IS_SANDBOX:-}" = 1 ] || \
   [ "$(awk '/CapBnd/{print $2}' /proc/self/status)" = 0000000000000000 ]; then
  echo "Inside the bwrap jail — run me from a normal terminal."; exit 2
fi
command -v pasta   >/dev/null || { echo "pasta not found — sudo apt-get install passt"; exit 2; }
command -v bwrap   >/dev/null || { echo "bwrap not found — sudo apt-get install bubblewrap"; exit 2; }
command -v unshare >/dev/null || { echo "unshare not found — install util-linux"; exit 2; }
[ -e /dev/net/tun ] || { echo "/dev/net/tun missing — add --device=/dev/net/tun to runArgs + rebuild"; exit 2; }

RUNDIR=$(mktemp -d)
READY="$RUNDIR/jail.ready"; export READY
PASTA_ERR="$RUNDIR/jail.pasta.err"
INNER=$(mktemp); HOLD=$(mktemp); export INNER
trap 'rm -f "$INNER" "$HOLD"; rm -rf "$RUNDIR"; [ -n "${HOLDER:-}" ] && kill "$HOLDER" 2>/dev/null || true' EXIT

# Stub-resolver detection (issue #60): if every /etc/resolv.conf nameserver is
# loopback, stage a resolv.conf pointed at the pasta forwarder for INNER.
RESOLV_FWD=""; export RESOLV_FWD
if ! awk '/^[[:space:]]*nameserver/{print $2}' /etc/resolv.conf 2>/dev/null \
     | grep -qvE '^(127\.|::1$)'; then
  RESOLV_FWD="$RUNDIR/resolv.forward"
  printf 'nameserver %s\n' "$JAIL_DNS_FWD" > "$RESOLV_FWD"
  echo "  [probe] loopback-only resolv.conf — INNER will resolve via pasta forwarder $JAIL_DNS_FWD"
fi

# INNER — Claude's vantage inside bwrap. Asserts the policy via `ip route get`
# (no traffic) plus real connectivity. NAME/DEV/JAIL_* arrive as env vars.
cat > "$INNER" <<'EOF'
set -u
conn(){ timeout "${3:-8}" bash -c "exec 3<>/dev/tcp/$1/$2" 2>/dev/null; }
rget(){ ip route get "$1" 2>/dev/null | head -n1; }
caps=$(awk '/CapBnd/{print $2}' /proc/self/status)
echo "INFO  CapBnd=$caps (full expected; security = ancestor-userns ownership)"

echo "  --- routing policy (ip route get, no traffic) ---"
echo "$(rget 1.1.1.1)" | grep -q "via ${JAIL_GW:-x}" && echo "PASS  internet routed via gateway $JAIL_GW" || echo "WARN  internet route: $(rget 1.1.1.1)"
# Active resolver: the first routable upstream, else the pasta forwarder
# (issue #60 — stub-resolver hosts resolve through JAIL_DNS_FWD).
fdns=$(printf '%s\n' ${JAIL_DNS:-} | grep -vE '^(127\.|::1$)' | awk 'NF{print;exit}')
[ -n "$fdns" ] || fdns="${JAIL_DNS_FWD:-}"
if [ -n "$fdns" ]; then
  echo "$(rget "$fdns")" | grep -q "via ${JAIL_GW:-x}" && echo "PASS  active resolver $fdns routed via gateway" || echo "WARN  resolver route: $(rget "$fdns")"
fi
if [ -n "${JAIL_SUBNET:-}" ]; then
  b=${JAIL_SUBNET%/*}; tl=${b%.*}.1; [ "$tl" = "${JAIL_GW:-}" ] && tl=${b%.*}.2
  r=$(rget "$tl" || true)
  if [ -z "$r" ] || printf '%s' "$r" | grep -qE 'blackhole|unreachable|prohibit'; then
    echo "PASS  same-subnet host $tl is blackholed"
  else
    echo "FAIL  SECURITY: same-subnet host $tl reachable: $r"
  fi
fi
# CGNAT (100.64/10, Tailscale et al.) must be blackholed.
cg=$(rget 100.127.255.254 || true)
if [ -z "$cg" ] || printf '%s' "$cg" | grep -qE 'blackhole|unreachable|prohibit'; then
  echo "PASS  CGNAT host 100.127.255.254 is blackholed"
else
  echo "FAIL  SECURITY: CGNAT host 100.127.255.254 reachable: $cg"
fi
# IPv4-only netns (--ipv4-only): no IPv6 address family at all.
v6addr=$(ip -6 addr show scope global 2>/dev/null | awk '/inet6/{print $2}')
if [ -z "$v6addr" ]; then
  echo "PASS  no global IPv6 address in the netns (IPv4-only)"
else
  echo "FAIL  SECURITY: netns has a global IPv6 address: $v6addr"
fi

echo "  --- connectivity ---"
nh=${NAME%:*}; np=${NAME##*:}
conn "$nh" "$np" 10 && echo "PASS  DNS+egress: $NAME reachable (name resolved + connected)" || echo "FAIL  DNS+egress: $NAME unreachable"
conn 1.1.1.1 443    && echo "PASS  internet by IP (1.1.1.1:443)"                              || echo "FAIL  internet by IP unreachable"
conn 10.255.255.254 9 3 && echo "FAIL  RFC1918 LEAK (10/8)"                                  || echo "PASS  RFC1918 (10/8) blocked"
conn 100.127.255.254 9 3 && echo "FAIL  CGNAT LEAK (100.64/10)"                              || echo "PASS  CGNAT (100.64/10) blocked"
# IPv6 must be entirely absent (--ipv4-only). A v6 connect can't even start
# without a v6 source address, so this is unreachable by construction.
conn 2606:4700:4700::1111 443 3 && echo "FAIL  SECURITY: IPv6 egress reachable (2606:4700:4700::1111)" || echo "PASS  IPv6 egress unreachable (IPv4-only netns)"
if [ -n "${DEV:-}" ]; then
  dh=${DEV%:*}; dp=${DEV##*:}; [ "$dh" = "$dp" ] && dp=443
  conn "$dh" "$dp" && echo "PASS  device $dh:$dp reachable via allow-ip" || echo "WARN  device $dh:$dp unreachable"
fi

echo "  --- security: route-immutability from inside the jail ---"
defgw=$(ip route show default 2>/dev/null | awk '{print $3; exit}')
ip route del blackhole 10.0.0.0/8 2>/dev/null && echo "FAIL  SECURITY: deleted a blackhole route" || echo "PASS  cannot delete blackhole route (EPERM)"
{ [ -n "$defgw" ] && ip route add 10.255.255.254/32 via "$defgw" 2>/dev/null; } && echo "FAIL  SECURITY: punched route past blackhole" || echo "PASS  cannot punch a route past the blackhole (EPERM)"
ip link add dummy0 type dummy 2>/dev/null && echo "FAIL  SECURITY: created a net device" || echo "PASS  cannot add net devices (EPERM)"
conn 10.255.255.254 9 3 && echo "FAIL  SECURITY: RFC1918 reachable after manipulation" || echo "PASS  RFC1918 still blocked after manipulation attempts"
EOF

# HOLDER — owns the user+net ns; waits for pasta; applies the v2 surgical policy;
# hands off to a capless-by-ownership bwrap. Runs inside `unshare -rn`.
cat > "$HOLD" <<'EOF'
set -eu
ip link set lo up
for _ in $(seq 1 200); do [ -f "$READY" ] && break; sleep 0.05; done
[ -f "$READY" ] || { echo "  [holder] parent never signalled ready"; exit 3; }

def=$(ip route show default 2>/dev/null | head -n1)
gw=$(awk '{for(i=1;i<NF;i++)if($i=="via")print $(i+1)}' <<<"$def")
dev=$(awk '{for(i=1;i<NF;i++)if($i=="dev")print $(i+1)}' <<<"$def")
if [ -z "$gw" ] || [ -z "$dev" ]; then
  echo "  [holder] no default via/dev (pasta gave: ${def:-none})"; ip route show; exit 4
fi
# ALL connected subnets, not just the first (each is more-specific than the
# blackholes, so any left un-blackholed stays reachable).
mapfile -t subnets < <(ip -o route show dev "$dev" scope link 2>/dev/null | awk '{print $1}')
mapfile -t dns < <(awk '/^[[:space:]]*nameserver/{print $2}' /etc/resolv.conf 2>/dev/null)
echo "  [holder] gw=$gw dev=$dev subnets=${subnets[*]:-none} dns=${dns[*]:-none}"

# Gateway on-link first, then blackhole everything internal, then re-punch.
ip route replace "$gw/32" dev "$dev"
for subnet in "${subnets[@]:-}"; do
  [ -n "$subnet" ] && ip route replace blackhole "$subnet"
done
ip route replace blackhole   10.0.0.0/8
ip route replace blackhole   172.16.0.0/12
ip route replace blackhole   192.168.0.0/16
ip route replace blackhole   100.64.0.0/10
ip route replace unreachable 169.254.0.0/16
ip route replace default via "$gw" dev "$dev"
for ns in "${dns[@]:-}"; do
  [ -n "$ns" ] || continue
  case "$ns" in 127.*|::1|"$gw") continue;; esac
  ip route replace "$ns/32" via "$gw" || echo "  [holder] WARN: failed DNS punch-back for $ns"
done
# Forwarder /32 (issue #60): route the in-netns forwarder address via gw so its
# queries reach pasta's tap. Always safe — the address is globally non-routable.
ip route replace "$JAIL_DNS_FWD/32" via "$gw" || echo "  [holder] WARN: failed DNS forwarder punch-back for $JAIL_DNS_FWD"
if [ -n "${DEV:-}" ]; then
  ip route replace "${DEV%:*}/32" via "$gw" || echo "  [holder] WARN: failed allow-ip punch-back for ${DEV%:*}"
fi

export JAIL_GW="$gw" JAIL_SUBNET="${subnet:-}" JAIL_DNS="${dns[*]:-}"
# On a stub-resolver host, point INNER's resolv.conf at the pasta forwarder
# (mirrors the bwrap.py bind); otherwise INNER keeps the host resolv.conf.
resolv_bind=()
[ -n "${RESOLV_FWD:-}" ] && [ -r "${RESOLV_FWD:-}" ] && resolv_bind=(--ro-bind "$RESOLV_FWD" /etc/resolv.conf)
exec bwrap --ro-bind / / "${resolv_bind[@]}" --dev /dev --unshare-pid --cap-drop ALL -- bash "$INNER"
EOF

unshare -rn bash "$HOLD" &
HOLDER=$!
for _ in $(seq 1 200); do [ -e "/proc/$HOLDER/ns/net" ] && break; sleep 0.05; done
[ -e "/proc/$HOLDER/ns/net" ] || { echo "FAIL  holder netns never appeared"; exit 5; }

if pasta --config-net --ipv4-only --dns-forward "$JAIL_DNS_FWD" "$HOLDER" 2>"$PASTA_ERR"; then
  echo "  [probe] pasta attached to holder netns (pid $HOLDER)"
else
  echo "  [probe] pasta attach FAILED:"; sed 's/^/    /' "$PASTA_ERR"
  pasta --help 2>&1 | sed -n '1,60p'; kill "$HOLDER" 2>/dev/null || true; exit 6
fi

touch "$READY"
wait "$HOLDER"; rc=$?
[ "$rc" -eq 0 ] && echo "Done. v2 surgical policy validated — patch netns_holder to match." \
                || echo "FAIL  holder exited rc=$rc (see [holder] lines above)"
exit "$rc"
