# Configure the network egress jail

The jail is on by default. It blocks private, link-local and connected networks,
and the cloud metadata addresses, while allowing internet access, DNS and
explicitly allowed IPs. Routes your host or VPN adds for internal networks
are not carried into the jail: it rebuilds its route table from that
allowlist alone and checks it before the agent starts. Agents get a
private IPv4-only network namespace; ordinary container shells do not.

With the PyPI launcher, edit `~/.config/claude-sandbox.conf` on the host.
Follow [container configuration](use-the-container-image.md#configure-the-sandbox)
to mount it. In your own devcontainer, edit `/etc/claude-sandbox.conf`
from a terminal outside the agent, and reapply changes after rebuilding or
reinstalling.

## Add the one required container device

The PyPI launcher supplies `/dev/net/tun` automatically. For your own
devcontainer, add this run argument and rebuild:

```json
"runArgs": ["--device=/dev/net/tun"]
```

Missing tun, pasta or namespace support makes agent launch fail closed.
The install, the published image's start and `claude-sandbox doctor` warn
about a missing `/dev/net/tun` earlier, while the jail is on.
Use rootless Podman: rootful Docker cannot host the default jail.

The devcontainer's base image must also have `passt` 0.0~git20230908 or
later. Debian 12's (0.0~git20230309, in `node:22-slim` and other bookworm
images) fails twice. It installs `pasta` as a symlink to `passt`, so on a
host that enforces AppArmor, as Ubuntu hosts do, the profile for `passt`
confines it, and it cannot attach to the agent's namespace. Where it does
attach, it sets up a broadcast route that the jail's route check refuses.
bookworm-backports has no newer `passt`. Ubuntu 24.04 (0.0~git20240220)
and Debian 13 (0.0~git20250503) images work, for example
`node:22-trixie-slim` in place of `node:22-slim`. The install and
`claude-sandbox doctor` warn about an older `passt` while the jail is on,
and the launch refuses and names the cause.

## Keep a lab device or internal forge reachable

Add one bare IP per line:

```ini
allow-ip = 172.23.1.3
```

This grants access to that device, not just one service. Review each addition.

:::{note} DLS: Diamond GitLab
The shipped config includes `allow-ip = 172.23.142.119` for Diamond GitLab.
Retain that line if you replace the config and need forge access.
Authentication alone does not make a blocked internal IP reachable.
:::

## Reach services on the host's loopback

Use a relay port for services on `127.0.0.1`; `allow-ip` does not route
loopback into the jail:

```ini
local-port = 5432
```

The shipped `local-model-port = 1920` relays lllm2's API for every agent
and enables Pi's model discovery. Other ports use repeatable `local-port`
lines. Every relayed port exposes the whole service behind it.

The outer container must share the service's network namespace. The PyPI
launcher uses host networking by default; `--bridge` prevents its loopback
from reaching host-local services. A custom devcontainer needs host networking
when the service runs on the host.

## Let a browser login reach the agent

A callback relay lets a host browser reach a login server inside the agent's
private loopback. Enable only the fixed ports you need:

```ini
callback-port = 53692   # Pi's Claude subscription login
callback-port = 1455    # Codex browser login
```

The shipped examples are commented out. Ports cannot overlap
`local-port` or `local-model-port`. If a host port is already occupied,
the session warns and skips that relay; use the provider's paste-code,
callback-URL or device-login alternative where available.

The browser must reach the outer container's loopback. For a remote machine,
forward the port through your editor or SSH. Claude Code's variable callback
port uses its code-paste flow instead of a fixed relay.

## A note on Channel Access for Claude

:::{warning} DLS: Channel Access needs unicast
LAN broadcast discovery does not cross the agent's private network namespace.
Set `EPICS_CA_ADDR_LIST` to the device IPs, forward it with
[pass-env](pass-environment-variables.md), and allow each IP with `allow-ip`.
Ordinary shells in a host-network container retain broadcast access.
:::

See [Configuration](../reference/configuration.md) for all keys and overrides,
and the [threat model](../explanations/threat-model.md#the-egress-jail-and-the-native-sandbox)
for what the jail does and does not protect.
