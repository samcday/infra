# Fabric operator access through the Tailnet

This is the minimum pre-gateway operator path. It reuses `sam-desktop`'s
existing Headscale identity and its isolated `fabric-observer` Wi-Fi network
namespace; it does not advertise `10.66.0.0/24`, add a route or veth, install
Tailscale on a consensus root, or copy a K3s administrator credential off a
root.

The two nested SSH hops have separate pinned ED25519 host keys:

1. the outer Tailnet connection terminates at `sam-desktop`; and
2. an end-to-end inner SSH connection terminates at the selected fabric root.

The checked-in raw `ProxyCommand` is allowlisted to TCP/22 on the two
commissioned roots. On `sam-desktop` it enters the existing network namespace
and only relays bytes; it will not select an arbitrary fabric address or port.

## Use it

The relay requires all of the following:

- `sam-desktop` is online in Headscale and reachable over OpenSSH;
- its `fabric-observer` namespace is active and can reach the roots;
- the caller has Sam's SSH identity; and
- the commissioned public host keys in `known_hosts` still match.

The default identity is `~/.ssh/id_ed25519`; set `FABRIC_SSH_IDENTITY` to an
alternate private-key path when needed. OpenSSH user configuration and
connection sharing are deliberately ignored so an existing direct master
cannot bypass the pinned nested path.

From any of Sam's Tailnet machines with this repository checked out and that
identity loaded, run the helpers from the repository root.

Connect to a root:

```sh
scripts/fabric-ssh cp2
scripts/fabric-ssh cp1 sudo k3s kubectl get nodes -o wide
```

Run an ordinary Kubernetes command through the sudo boundary:

```sh
scripts/fk get nodes -o wide
scripts/ik --context=fabric get pods -A
```

Launch k9s locally:

```sh
scripts/fk k9s
```

`fk` starts `k3s kubectl proxy` on a randomly selected root-loopback port under
`sudo`, forwards it to local loopback through the end-to-end SSH connection,
and creates a mode-`0600` runtime kubeconfig containing no credential. A
dedicated stdin watchdog ties the root proxy to the SSH session. Exiting
kubectl or k9s tears down the remote proxy, SSH forward, and kubeconfig.

Use `--node cp1` if cp2 is unavailable. cp3 is intentionally absent from the
allowlist until its installed SSH host key has been captured through the
attended commissioning path.

`fk` uses the local client on `PATH`. Keep local kubectl within Kubernetes'
supported version skew. The desktop's v1.30 client completed these smoke tests
against fabric v1.36.2, but that gap is not a durable promise. The
version-matched emergency CLI is always available on a root:

```sh
scripts/fabric-ssh cp2 sudo k3s kubectl get nodes -o wide
```

## Boundary and successor

This path is immediately useful but deliberately depends on `sam-desktop` and
the temporary observer radio. It is not the final independent access plane.
The successor is a service-only `fabric-access` Headscale peer outside the
consensus roots, exposing only the API and bounded root SSH transports. The
same strict inner host keys and no-export kubeconfig model can remain.
