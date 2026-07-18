# Fabric operator access through the Tailnet

This is the phase-one operator path for the root fabric. It reuses
`sam-desktop`'s existing Headscale identity and isolated `fabric-observer`
Wi-Fi namespace. It does not advertise `10.66.0.0/24`, add a route or veth,
install Tailscale on a consensus root, or copy a K3s administrator kubeconfig
or CA private key off a root.

The path has three independently checked boundaries:

1. a pinned Tailnet SSH connection terminates at `sam-desktop`;
2. its allowlisted sudo command relays only to TCP/22 on a commissioned root
   from inside `fabric-observer`; and
3. a pinned, end-to-end inner SSH connection forwards local TCP/16443 to the
   API VIP from that root.

Kubernetes TLS remains end to end through both SSH transports. The tracked
kubeconfig verifies `api.fabric.internal` against `server-ca.crt`; a process
that races the local port cannot impersonate the API.

## Requirements

- `sam-desktop` is online in Headscale and reachable over OpenSSH;
- its `fabric-observer` namespace is active and can reach the roots;
- the caller has Sam's SSH identity, defaulting to `~/.ssh/id_ed25519`;
- the commissioned public keys in `known_hosts` still match; and
- the client has Bash, curl, jq, OpenSSL, OpenSSH, and a user systemd manager.

OpenSSH user configuration and connection sharing are ignored so an existing
master cannot bypass the pinned path. Set `FABRIC_SSH_IDENTITY` when the
private key is at a different path.

## Use it

The tracked kubeconfig defaults to fabric, so this works from any directory on
a Tailnet machine whose checkout is at `~/src/infra`:

```sh
kubectl --kubeconfig ~/src/infra/kubeconfig get nodes -o wide
kubectl --kubeconfig ~/src/infra/kubeconfig get --raw=/readyz
```

The repository wrappers remain convenient:

```sh
scripts/fk get pods -A
scripts/fk k9s
scripts/ik --context=fabric get nodes
```

`fabric-credential` generates a P-256 key locally. Through the same pinned
SSH/sudo path, a root validates the exact
`O=system:masters,CN=sam-fabric-operator` CSR, constrains its key and
extensions, and signs it directly with the K3s client CA for about 15 minutes
of usable time. A one-minute `notBefore` backdate makes the encoded validity
window at most 16 minutes while tolerating small client/root clock skew.
The CA key stays on the root, and no CSR or RBAC object is created in
Kubernetes. The mode-0600 key and leaf live only in the caller's protected
runtime directory and rotate with at least one minute remaining.

The kubeconfig exec helper also calls `fabric-kube-tunnel ensure`. That command
uses a transient user-systemd service to supervise the fixed loopback forward,
verify the real API serving certificate before returning, and reconnect through
cp2, cp1, then cp3. Native API TLS means watches, exec streams, k9s, and
concurrent clients do not traverse `kubectl proxy`.

Inspect or explicitly stop the transport with:

```sh
scripts/fabric-kube-tunnel status
scripts/fabric-kube-tunnel stop
```

Connect to a root for a version-matched emergency CLI:

```sh
scripts/fabric-ssh cp2 sudo k3s kubectl get nodes -o wide
```

## Boundary and successor

This deliberately depends on `sam-desktop` and the temporary observer radio.
It is not the final independent access plane. Its successor is a service-only
`fabric-access` Headscale peer outside the consensus roots, exposing only the
API and bounded root SSH transports. The pinned API trust and short-lived
operator credential model can remain.
